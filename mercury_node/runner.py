"""Run one task in the right shell, with a timeout that kills the whole process tree."""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from .config import IS_WINDOWS

MAX_OUTPUT_CHARS = 60_000
TIMEOUT_EXIT = 124
# PowerShell writes in the console code page by default; force UTF-8 so non-ASCII
# file names and output survive the trip to the hub.
_PS_PREFIX = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "


def available_shells() -> list[str]:
    if IS_WINDOWS:
        found = [name for name, exe in (("powershell", "powershell"), ("pwsh", "pwsh"), ("cmd", "cmd"))
                 if shutil.which(exe)]
        return found or ["cmd"]
    return [name for name in ("bash", "sh") if shutil.which(name)] or ["sh"]


def default_shell() -> str:
    return available_shells()[0]


def argv_for(shell: str, command: str) -> list[str]:
    shell = shell or default_shell()
    if shell in ("powershell", "pwsh"):
        exe = shutil.which(shell) or shell
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _PS_PREFIX + command]
    if shell == "cmd":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    if shell in ("bash", "sh"):
        return [shutil.which(shell) or shell, "-c", command]
    raise ValueError(f"unsupported shell {shell!r}")


def _clip(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + "\n... [truncated on device]"


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()


@dataclass
class Outcome:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class Runner:
    """Tracks running processes so the hub can cancel them."""

    def __init__(self):
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def run(self, task_id: str, command: str, shell: str, cwd: str, timeout_s: int) -> Outcome:
        started = time.monotonic()
        kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "stdin": subprocess.DEVNULL,
                        "cwd": os.path.expanduser(cwd) if cwd else None}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv_for(shell, command), **kwargs)
        except (OSError, ValueError) as exc:
            return Outcome(127, "", f"could not start: {exc}", 0)
        with self._lock:
            self._procs[task_id] = proc
        try:
            out, err = proc.communicate(timeout=timeout_s)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            code = TIMEOUT_EXIT
            err += f"\n[mercury-node] killed after {timeout_s}s timeout".encode()
        finally:
            with self._lock:
                self._procs.pop(task_id, None)
        return Outcome(code, _clip(out), _clip(err), int((time.monotonic() - started) * 1000))

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(task_id)
        if proc:
            _kill_tree(proc)
        return proc is not None


def file_artifacts(paths: list[str], cwd: str) -> list[dict]:
    artifacts = []
    for raw in paths[:20]:
        path = os.path.join(os.path.expanduser(cwd or ""), os.path.expanduser(raw))
        try:
            if os.path.isfile(path):
                digest = hashlib.sha256()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        digest.update(chunk)
                artifacts.append({"path": raw, "size": os.path.getsize(path), "sha256": digest.hexdigest()})
            else:
                artifacts.append({"path": raw, "missing": True})
        except OSError as exc:
            artifacts.append({"path": raw, "error": str(exc)})
    return artifacts
