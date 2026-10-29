"""The node's connection to the hub: connect out, take tasks, report results.

If the hub is unreachable the node waits and retries (1 s doubling to 5 min, with
jitter). Nothing else on the device is affected. A revoked or unknown token stops
the node instead of retrying forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import random
import time

import websockets

from . import PROTOCOL_VERSION, __version__
from .config import IS_TERMUX, log_path, platform_name
from .policy import LocalPolicy
from .runner import Runner, available_shells, file_artifacts

log = logging.getLogger("mercury.node")

HEARTBEAT_S = 60
MAX_BACKOFF_S = 300
MAX_PARALLEL_TASKS = 4
AUDIT_MAX_BYTES = 5 * 1024 * 1024


class TokenRejected(Exception):
    """The hub refused this device's token (revoked or never paired)."""


def ws_url(hub_url: str) -> str:
    if hub_url.startswith("https://"):
        return "wss://" + hub_url[len("https://"):] + "/v1/node"
    if hub_url.startswith("http://"):
        return "ws://" + hub_url[len("http://"):] + "/v1/node"
    raise ValueError("hub URL must start with https:// or http://")


def capabilities() -> dict:
    import shutil

    return {"shells": available_shells(), "termux_api": IS_TERMUX and bool(shutil.which("termux-battery-status")),
            "python": platform.python_version(), "os_release": platform.platform(terse=True)}


def audit(entry: dict) -> None:
    """Append one line per task to the local audit log, so the user can see everything the hub ran."""
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > AUDIT_MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}) + "\n")
    except OSError as exc:
        log.warning("could not write audit log: %s", exc)


def confirm_on_device(task: dict) -> bool:
    """Ask in this console window. Runs in a worker thread; a missing console means no."""
    try:
        answer = input(f"\n[mercury-node] Hub wants to run:\n  {task['command']}\nAllow? [y/N] ")
    except (EOFError, OSError):
        return False
    return answer.strip().lower() in ("y", "yes")


class Node:
    def __init__(self, hub_url: str, token: str, policy_loader=LocalPolicy.load):
        self.url = ws_url(hub_url)
        self.token = token
        self.policy_loader = policy_loader
        self.runner = Runner()
        self.slots = asyncio.Semaphore(MAX_PARALLEL_TASKS)
        self.device_name = ""
        self.stopping = False

    async def run_forever(self) -> None:
        delay = 1.0
        while not self.stopping:
            try:
                await self.session()
                delay = 1.0
            except TokenRejected:
                raise
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as exc:
                log.info("hub unreachable (%s); retrying in %.0fs", exc.__class__.__name__, delay)
            if self.stopping:
                break
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, MAX_BACKOFF_S)

    async def session(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": f"mercury-node/{__version__}"}
        try:
            ws = await websockets.connect(self.url, additional_headers=headers, open_timeout=20,
                                          ping_interval=30, ping_timeout=30, max_size=2 * 1024 * 1024)
        except websockets.InvalidStatus as exc:
            if exc.response.status_code in (401, 403):
                raise TokenRejected("the hub rejected this device's token (revoked or not paired)") from exc
            raise
        async with ws:
            await ws.send(json.dumps({"type": "hello", "protocol": PROTOCOL_VERSION, "platform": platform_name(),
                                      "hostname": platform.node(), "node_version": __version__,
                                      "capabilities": capabilities()}))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if welcome.get("type") != "welcome":
                raise websockets.WebSocketException(f"unexpected reply {welcome.get('type')!r}")
            self.device_name = welcome.get("device_name", "")
            log.info("connected to hub as %r (approval: %s)", self.device_name, welcome.get("approval"))
            heartbeat = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("type") == "task":
                        asyncio.create_task(self._handle(ws, message))
                    elif message.get("type") == "cancel":
                        if self.runner.cancel(str(message.get("task_id"))):
                            log.info("cancelled task %s at the hub's request", message.get("task_id"))
            except websockets.ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                if code == 4403:
                    raise TokenRejected("this device was revoked on the hub") from exc
                if code == 4409:
                    log.warning("another connection with this device's token replaced this one")
                    self.stopping = True
                raise
            finally:
                heartbeat.cancel()

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await ws.send(json.dumps({"type": "heartbeat"}))

    async def _handle(self, ws, task: dict) -> None:
        task_id = str(task.get("task_id", ""))
        command, cwd = str(task.get("command", "")), str(task.get("cwd", ""))
        verify = str(task.get("verify_command", ""))
        policy = self.policy_loader()
        reason = policy.check(command, cwd, verify)
        if reason is None and policy.confirm_on_device and not await asyncio.to_thread(confirm_on_device, task):
            reason = "declined by the person at this device"
        if reason:
            log.info("refused task %s: %s", task_id, reason)
            audit({"task_id": task_id, "command": command, "refused": reason})
            await ws.send(json.dumps({"type": "task_ack", "task_id": task_id, "accepted": False, "reason": reason}))
            return
        await ws.send(json.dumps({"type": "task_ack", "task_id": task_id, "accepted": True}))
        timeout_s = max(1, min(int(task.get("timeout_s") or 120), policy.max_timeout_s))
        shell = str(task.get("shell", ""))
        log.info("running task %s: %s", task_id, command[:120])
        async with self.slots:
            outcome = await asyncio.to_thread(self.runner.run, task_id, command, shell, cwd, timeout_s)
            result = {"type": "task_result", "task_id": task_id, "exit_code": outcome.exit_code,
                      "stdout": outcome.stdout, "stderr": outcome.stderr, "duration_ms": outcome.duration_ms}
            if verify:
                checked = await asyncio.to_thread(self.runner.run, task_id + ":verify", verify, shell, cwd,
                                                  min(60, timeout_s))
                result |= {"verify_exit_code": checked.exit_code, "verify_output": checked.stdout + checked.stderr}
            result["artifacts"] = file_artifacts(list(task.get("collect_paths") or []), cwd)
        audit({"task_id": task_id, "command": command, "exit_code": outcome.exit_code,
               "verify_exit_code": result.get("verify_exit_code"), "duration_ms": outcome.duration_ms})
        log.info("task %s finished: exit %s%s", task_id, outcome.exit_code,
                 f", verify {result['verify_exit_code']}" if verify else "")
        await ws.send(json.dumps(result))
