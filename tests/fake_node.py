"""A minimal node that speaks the Mercury device protocol, for hub tests.

It pairs, connects, and runs tasks with ``sh -c`` (or answers from a script of
canned behaviours). The real end-user client lives on the ``node`` branch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time

import httpx
import websockets

PROTOCOL = 1


def pair(base_url: str, code: str, platform: str = "linux", hostname: str = "test-host") -> dict:
    response = httpx.post(f"{base_url}/v1/pair", json={"code": code, "platform": platform, "hostname": hostname,
                                                       "node_version": "test", "capabilities": {"shells": ["sh"]}})
    response.raise_for_status()
    return response.json()


def _run(command: str, cwd: str, timeout: int) -> tuple[int, str, str]:
    try:
        done = subprocess.run(["sh", "-c", command], cwd=cwd or None, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


class FakeNode:
    """Runs in its own thread's event loop; ``reject`` / ``hang`` make it misbehave on purpose."""

    def __init__(self, base_url: str, token: str, reject: set[str] | None = None, hang: bool = False):
        self.ws_url = base_url.replace("http", "ws", 1) + "/v1/node"
        self.token = token
        self.reject = reject or set()
        self.hang = hang
        self.welcome: dict | None = None
        self.close_code: int | None = None
        self.rejected_status: int | None = None  # HTTP status when the handshake itself is refused
        self.received: list[dict] = []
        self.connected = asyncio.Event()

    async def run(self) -> None:
        try:
            async with websockets.connect(self.ws_url, additional_headers={"Authorization": f"Bearer {self.token}"}) as ws:
                await ws.send(json.dumps({"type": "hello", "protocol": PROTOCOL, "platform": "linux",
                                          "hostname": "test-host", "node_version": "test",
                                          "capabilities": {"shells": ["sh"]}}))
                self.welcome = json.loads(await ws.recv())
                self.connected.set()
                async for raw in ws:
                    message = json.loads(raw)
                    self.received.append(message)
                    if message.get("type") == "task":
                        asyncio.create_task(self._task(ws, message))
        except websockets.InvalidStatus as exc:
            self.rejected_status = exc.response.status_code
        except websockets.ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else None
        finally:
            self.connected.set()

    async def _task(self, ws, task: dict) -> None:
        if any(word in task["command"] for word in self.reject):
            await ws.send(json.dumps({"type": "task_ack", "task_id": task["task_id"], "accepted": False,
                                      "reason": "blocked by local policy"}))
            return
        await ws.send(json.dumps({"type": "task_ack", "task_id": task["task_id"], "accepted": True}))
        if self.hang:
            return
        started = time.time()
        code, out, err = await asyncio.to_thread(_run, task["command"], task.get("cwd", ""), task["timeout_s"])
        result = {"type": "task_result", "task_id": task["task_id"], "exit_code": code, "stdout": out,
                  "stderr": err, "duration_ms": int((time.time() - started) * 1000)}
        if task.get("verify_command"):
            vcode, vout, verr = await asyncio.to_thread(_run, task["verify_command"], task.get("cwd", ""), 30)
            result |= {"verify_exit_code": vcode, "verify_output": vout + verr}
        artifacts = []
        for path in task.get("collect_paths") or []:
            full = os.path.join(task.get("cwd") or "", path)
            if os.path.isfile(full):
                with open(full, "rb") as fh:
                    artifacts.append({"path": path, "size": os.path.getsize(full),
                                      "sha256": hashlib.sha256(fh.read()).hexdigest()})
        result["artifacts"] = artifacts
        await ws.send(json.dumps(result))
