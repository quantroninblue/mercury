"""mercury-devices: the agent's bridge to paired devices.

Tools (toolset ``mercury``):
  mercury_devices      which devices are paired, online, and their OS
  mercury_run          run a command on a device and return a verified result
  mercury_task         look up one task (for long jobs started with wait=false)
  mercury_tasks        recent task log
  mercury_skill_stats  how often each skill's device tasks succeeded

Hooks:
  pre_tool_call   classifies every mercury_run command with mercury_hub.policy and
                  either refuses it or hands it to the engine's human-approval gate
                  (CLI prompt, API approval, or a yes/no reply on WhatsApp), which
                  fails closed. The model cannot skip this.
  post_tool_call  remembers which skills were loaded in a session and, when a device
                  task finishes, records the outcome against those skills.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

try:
    import mercury_hub  # noqa: F401  (installed into the engine's environment)
except ImportError:  # running from a checkout: plugin/mercury-devices -> repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mercury_hub import policy
from mercury_hub.config import Settings
from mercury_hub.db import FINAL_STATUSES, Store

TOOLSET = "mercury"
MODEL_OUTPUT_CHARS = 6000

_store: Store | None = None
_store_lock = threading.Lock()
# session_id -> skills loaded with skill_view in that session
_session_skills: dict[str, set[str]] = {}
_skills_lock = threading.Lock()


def _settings() -> Settings:
    return Settings.load()


def _db() -> Store:
    global _store
    with _store_lock:
        if _store is None:
            _store = Store(_settings().db_path)
        return _store


def _clip(text: str | None, limit: int = MODEL_OUTPUT_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more chars in task log]"


def _relay(method: str, path: str, **kwargs) -> dict:
    import httpx

    settings = _settings()
    if not settings.admin_token:
        raise RuntimeError("MERCURY_ADMIN_TOKEN is not configured on the hub.")
    response = httpx.request(method, settings.relay_url + path, timeout=kwargs.pop("timeout", 30),
                             headers={"Authorization": f"Bearer {settings.admin_token}"}, **kwargs)
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:500]
        raise RuntimeError(f"relay error {response.status_code}: {detail}")
    return response.json()


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def _task_view(task: dict) -> dict:
    view = {k: task.get(k) for k in ("id", "device", "status", "exit_code", "duration_ms", "command",
                                    "verify_command", "verify_exit_code", "artifacts", "error")}
    view["stdout"] = _clip(task.get("stdout"))
    view["stderr"] = _clip(task.get("stderr"), 2000)
    view["verify_output"] = _clip(task.get("verify_output"), 2000)
    view["verified"] = task.get("status") == "verified"
    view["final"] = task.get("status") in FINAL_STATUSES
    return view


# -- tool schemas ---------------------------------------------------------------

DEVICES_SCHEMA = {
    "name": "mercury_devices",
    "description": (
        "List the user's devices paired with Mercury (laptops, PCs, phones), with online status, OS/platform, "
        "hostname, shell capabilities and approval mode. Call this before running anything on a device, and to "
        "pick the right device for a task."),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

RUN_SCHEMA = {
    "name": "mercury_run",
    "description": (
        "Run a shell command on one of the user's paired devices and return its output. Use the device's own "
        "shell syntax (PowerShell on Windows, sh/bash on Linux, macOS and Android/Termux). ALWAYS pass a "
        "verify_command that independently proves the outcome (e.g. Test-Path on a created file, or re-reading "
        "a value); a task only counts as 'verified' if the command exits 0 and the verify_command exits 0. "
        "Destructive or system-changing commands pause for the user's approval automatically."),
    "parameters": {
        "type": "object",
        "properties": {
            "device": {"type": "string", "description": "Device name from mercury_devices."},
            "command": {"type": "string", "description": "Command to run on the device."},
            "verify_command": {"type": "string",
                               "description": "Command that exits 0 only if the task really succeeded."},
            "shell": {"type": "string", "enum": ["", "powershell", "cmd", "bash", "sh"],
                      "description": "Shell to use; empty = device default."},
            "cwd": {"type": "string", "description": "Working directory on the device (optional)."},
            "collect_paths": {"type": "array", "items": {"type": "string"},
                              "description": "Files the task produces; the device reports their size and SHA-256."},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": 3600,
                          "description": "Seconds before the task is cancelled (default 120)."},
            "wait": {"type": "boolean",
                     "description": "Wait for the result (default true). false returns a task id for mercury_task."},
        },
        "required": ["device", "command"],
    },
}

TASK_SCHEMA = {
    "name": "mercury_task",
    "description": "Get the status and verified result of a Mercury device task by id.",
    "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
}

TASKS_SCHEMA = {
    "name": "mercury_tasks",
    "description": "Recent Mercury device tasks (newest first), optionally for one device. Use it to answer "
                   "'what's the status of my tasks' from any device.",
    "parameters": {"type": "object", "properties": {
        "device": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": []},
}

SKILL_STATS_SCHEMA = {
    "name": "mercury_skill_stats",
    "description": (
        "Verified success rate of device tasks run while each skill was loaded, worst first, with recent "
        "failures. Use it to decide which skills need fixing; patch a failing skill with skill_manage."),
    "parameters": {"type": "object", "properties": {"skill": {"type": "string"}}, "required": []},
}


# -- handlers -------------------------------------------------------------------

def handle_devices(params: dict, **_kwargs) -> str:
    try:
        devices = _relay("GET", "/v1/admin/devices")["devices"]
    except Exception as exc:
        return _error(f"Mercury relay unavailable: {exc}")
    return json.dumps({"success": True, "devices": devices})


def handle_run(params: dict, session_id: str | None = None, **_kwargs) -> str:
    device, command = str(params.get("device") or "").strip(), str(params.get("command") or "")
    if not device or not command.strip():
        return _error("device and command are required.")
    timeout_s = int(params.get("timeout_s") or 120)
    body = {"device": device, "command": command, "shell": params.get("shell") or "",
            "cwd": params.get("cwd") or "", "timeout_s": timeout_s,
            "verify_command": params.get("verify_command") or "",
            "collect_paths": list(params.get("collect_paths") or []),
            "wait": params.get("wait", True) is not False, "requested_by": "agent", "session_id": session_id or ""}
    try:
        task = _relay("POST", "/v1/admin/tasks", json=body, timeout=timeout_s + 45)
    except Exception as exc:
        return _error(f"Could not run on {device}: {exc}")
    result = {"success": task["status"] in ("verified", "completed", "sent", "running"), "task": _task_view(task)}
    if task["status"] == "completed":
        result["note"] = "Command succeeded but no verify_command was given, so the result is unverified."
    return json.dumps(result)


def handle_task(params: dict, **_kwargs) -> str:
    try:
        task = _relay("GET", f"/v1/admin/tasks/{params.get('task_id', '')}")
    except Exception as exc:
        return _error(str(exc))
    return json.dumps({"success": True, "task": _task_view(task)})


def handle_tasks(params: dict, **_kwargs) -> str:
    try:
        tasks = _relay("GET", "/v1/admin/tasks", params={k: v for k, v in
                                                       {"device": params.get("device"),
                                                        "limit": params.get("limit") or 10}.items() if v})["tasks"]
    except Exception as exc:
        return _error(str(exc))
    return json.dumps({"success": True, "tasks": [
        {k: t[k] for k in ("id", "device", "status", "command", "exit_code", "error", "created_at")} for t in tasks]})


def handle_skill_stats(params: dict, **_kwargs) -> str:
    return json.dumps({"success": True, "skills": _db().skill_stats(params.get("skill") or None)})


# -- hooks ----------------------------------------------------------------------

def on_pre_tool_call(tool_name: str = "", args: dict | None = None, **_kwargs):
    if tool_name != "mercury_run" or not isinstance(args, dict):
        return None
    name, command = str(args.get("device") or ""), str(args.get("command") or "")
    device = _db().device_by_name(name)
    if not device:
        return None  # the handler reports the unknown device
    checks = [command] + ([args["verify_command"]] if args.get("verify_command") else [])
    decisions = [policy.classify(c, device["approval"], device["blocked_patterns"]) for c in checks]
    blocked = next((d for d in decisions if d.action == "block"), None)
    if blocked:
        return {"action": "block", "message": f"Mercury refused to run this on {name}: {blocked.reason}. "
                                              "Tell the user and suggest a safer alternative."}
    approve = next((d for d in decisions if d.action == "approve"), None)
    if approve:
        return {"action": "approve", "rule_key": f"mercury:{name}:{approve.rule}",
                "message": f"Run on {name} ({device['platform'] or 'device'}): {command[:300]}  [{approve.reason}]"}
    return None


def on_post_tool_call(tool_name: str = "", args: dict | None = None, result=None, session_id: str = "",
                      task_id: str = "", **_kwargs) -> None:
    key = session_id or task_id or "default"
    if tool_name == "skill_view" and isinstance(args, dict) and args.get("name"):
        with _skills_lock:
            _session_skills.setdefault(key, set()).add(str(args["name"]))
        return
    if tool_name != "mercury_run":
        return
    try:
        task = json.loads(result).get("task") if isinstance(result, str) else None
    except ValueError:
        return
    if not task or not task.get("final"):
        return
    with _skills_lock:
        skills = sorted(_session_skills.get(key, ()))
    for skill in skills:
        try:
            _db().record_skill_outcome(skill, task["id"], task["status"], session_id)
        except Exception:
            pass


# -- slash commands (work in the CLI and on WhatsApp) ---------------------------

def cmd_devices(raw_args: str = "", **_kwargs) -> str:
    try:
        devices = _relay("GET", "/v1/admin/devices")["devices"]
    except Exception as exc:
        return f"Mercury relay unavailable: {exc}"
    if not devices:
        return "No paired devices yet. On the hub run: mercury pair <name>"
    return "\n".join(f"{d['name']}: {'online' if d['online'] else 'offline'}, {d['platform'] or '?'} "
                     f"({d['hostname'] or '?'}), approval: {d['approval']}" for d in devices)


def cmd_tasks(raw_args: str = "", **_kwargs) -> str:
    try:
        tasks = _relay("GET", "/v1/admin/tasks", params={"limit": 10})["tasks"]
    except Exception as exc:
        return f"Mercury relay unavailable: {exc}"
    if not tasks:
        return "No device tasks yet."
    return "\n".join(f"{t['id']}, {t['device']}, {t['status']}, {t['command'][:60]}" for t in tasks)


def register(ctx) -> None:
    for schema, handler in (
        (DEVICES_SCHEMA, handle_devices),
        (RUN_SCHEMA, handle_run),
        (TASK_SCHEMA, handle_task),
        (TASKS_SCHEMA, handle_tasks),
        (SKILL_STATS_SCHEMA, handle_skill_stats),
    ):
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_command("devices", cmd_devices, description="List Mercury devices and whether they are online")
    ctx.register_command("device-tasks", cmd_tasks, description="Recent Mercury device tasks")
