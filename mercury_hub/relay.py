"""Mercury device relay.

Nodes (end-user devices) connect *out* to the hub over a WebSocket, so devices
need no open ports, SSH server or VPN. The agent never talks to devices
directly: it calls the admin API on this relay, which looks up the device,
applies the hard policy floor, sends the task over the device's socket and
waits for a verified result.

Public routes (put these, and only these, behind TLS):
    GET  /health
    POST /v1/pair          one-time code -> device token
    WS   /v1/node          authenticated device connection (bad token: handshake refused, HTTP 403)
    POST /v1/ask           a paired device talks to Mercury (bearer device token)
    GET  /v1/ask/{run_id}  progress, reply and pending approvals for that message
    POST /v1/ask/{run_id}/approval

Admin routes (hub-local; bearer MERCURY_ADMIN_TOKEN):
    GET  /v1/admin/devices
    POST /v1/admin/pairing
    POST /v1/admin/devices/{name}/revoke
    POST /v1/admin/devices/{name}/policy
    POST /v1/admin/tasks
    GET  /v1/admin/tasks
    GET  /v1/admin/tasks/{task_id}
    GET  /v1/admin/skills      installed and pending skills with verified success rates

The wire protocol is documented in docs/protocol.md.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from . import PROTOCOL_VERSION, __version__, policy
from .db import FINAL_STATUSES, Store

log = logging.getLogger("mercury.relay")

HELLO_TIMEOUT_S = 10
RESULT_GRACE_S = 15
MAX_TIMEOUT_S = 3600
PAIR_FAILURE_LIMIT = 5
PAIR_FAILURE_WINDOW_S = 15 * 60


class PairRequest(BaseModel):
    code: str = Field(min_length=6, max_length=16)
    platform: str = Field(default="", max_length=64)
    hostname: str = Field(default="", max_length=128)
    node_version: str = Field(default="", max_length=32)
    capabilities: dict = Field(default_factory=dict)


class PairingCodeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    approval: str = "dangerous"
    ttl_s: int = Field(default=600, ge=60, le=86400)


class PolicyRequest(BaseModel):
    approval: str | None = None
    blocked_patterns: list[str] | None = None


class TaskRequest(BaseModel):
    device: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=20000)
    shell: str = Field(default="", max_length=32)
    cwd: str = Field(default="", max_length=1024)
    timeout_s: int = Field(default=120, ge=1, le=MAX_TIMEOUT_S)
    verify_command: str = Field(default="", max_length=20000)
    collect_paths: list[str] = Field(default_factory=list, max_length=20)
    wait: bool = True
    requested_by: str = Field(default="", max_length=64)
    session_id: str = Field(default="", max_length=128)


class AskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    new_conversation: bool = False


class AskApproval(BaseModel):
    request_id: str = Field(min_length=1, max_length=256)
    choice: str = Field(pattern="^(once|session|always|deny)$")
    execution_generation: int | None = None


@dataclass
class Connection:
    device_id: str
    websocket: WebSocket
    connected_at: float = field(default_factory=time.time)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict) -> None:
        async with self.send_lock:
            await self.websocket.send_json(message)


class Hub:
    def __init__(self, store: Store, admin_token: str):
        self.store = store
        self.admin_token = admin_token
        self.connections: dict[str, Connection] = {}
        self.waiters: dict[str, asyncio.Future] = {}
        self.pair_failures: dict[str, list[float]] = {}
        # /v1/ask: which device owns each agent run.
        self.ask_runs: dict[str, str] = {}

    # -- task lifecycle --------------------------------------------------------

    def _resolve(self, task_id: str) -> None:
        waiter = self.waiters.pop(task_id, None)
        if waiter and not waiter.done():
            waiter.set_result(None)

    async def submit(self, req: TaskRequest) -> dict:
        device = self.store.device_by_name(req.device)
        if not device:
            raise HTTPException(404, f"No paired device named {req.device!r}.")
        common = dict(shell=req.shell, cwd=req.cwd, verify_command=req.verify_command,
                      collect_paths=req.collect_paths, timeout_s=req.timeout_s,
                      requested_by=req.requested_by, session_id=req.session_id)
        # Hard floor is enforced here too, whatever the caller decided.
        decision = policy.classify(req.command, device["approval"], device["blocked_patterns"])
        if decision.action == "block":
            return self.store.create_task(device["id"], req.command, status="blocked", error=decision.reason, **common)
        conn = self.connections.get(device["id"])
        if not conn:
            return self.store.create_task(device["id"], req.command, status="offline",
                                          error=f"{req.device} is not connected.", **common)
        task = self.store.create_task(device["id"], req.command, status="sent", **common)
        waiter = asyncio.get_running_loop().create_future()
        self.waiters[task["id"]] = waiter
        try:
            await conn.send({"type": "task", "task_id": task["id"], "command": req.command, "shell": req.shell,
                             "cwd": req.cwd, "timeout_s": req.timeout_s, "verify_command": req.verify_command,
                             "collect_paths": req.collect_paths})
        except Exception as exc:
            self.waiters.pop(task["id"], None)
            self.store.set_task_status(task["id"], "failed", f"could not reach device: {exc}")
            return self.store.task(task["id"])
        if req.wait:
            await self._await_result(task["id"], req.timeout_s, conn)
        else:
            asyncio.create_task(self._await_result(task["id"], req.timeout_s, conn))
        return self.store.task(task["id"])

    async def _await_result(self, task_id: str, timeout_s: int, conn: Connection) -> None:
        waiter = self.waiters.get(task_id)
        if waiter is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout_s + RESULT_GRACE_S)
        except asyncio.TimeoutError:
            self.waiters.pop(task_id, None)
            self.store.set_task_status(task_id, "timeout", f"no result within {timeout_s + RESULT_GRACE_S}s")
            try:
                await conn.send({"type": "cancel", "task_id": task_id})
            except Exception:
                pass

    # -- node messages ---------------------------------------------------------

    def handle_node_message(self, device_id: str, message: dict) -> None:
        kind = message.get("type")
        if kind == "heartbeat":
            self.store.touch_device(device_id)
            return
        task_id = str(message.get("task_id") or "")
        task = self.store.task(task_id) if task_id else None
        if not task or task["device_id"] != device_id:
            log.warning("device %s sent %s for unknown task %r", device_id[:8], kind, task_id)
            return
        if kind == "task_ack":
            if message.get("accepted"):
                self.store.set_task_status(task_id, "running")
            else:
                self.store.set_task_status(task_id, "rejected",
                                           str(message.get("reason") or "refused by the device's local policy"))
                self._resolve(task_id)
        elif kind == "task_result":
            self.store.record_result(task_id, message)
            self._resolve(task_id)

    def disconnected(self, conn: Connection) -> None:
        if self.connections.get(conn.device_id) is conn:
            del self.connections[conn.device_id]
        for task_id in [t for t, _ in list(self.waiters.items())]:
            task = self.store.task(task_id)
            if task and task["device_id"] == conn.device_id and task["status"] not in FINAL_STATUSES:
                self.store.set_task_status(task_id, "failed", "device disconnected before returning a result")
                self._resolve(task_id)

    # -- pairing rate limit ----------------------------------------------------

    def pair_allowed(self, client: str) -> bool:
        now = time.time()
        recent = [t for t in self.pair_failures.get(client, []) if now - t < PAIR_FAILURE_WINDOW_S]
        self.pair_failures[client] = recent
        return len(recent) < PAIR_FAILURE_LIMIT

    def pair_failed(self, client: str) -> None:
        self.pair_failures.setdefault(client, []).append(time.time())


def public_device(device: dict, hub: Hub) -> dict:
    conn = hub.connections.get(device["id"])
    return {k: device[k] for k in ("name", "platform", "hostname", "node_version", "capabilities", "approval",
                                   "blocked_patterns", "created_at", "last_seen", "revoked_at")} | {
        "online": conn is not None, "connected_since": conn.connected_at if conn else None}


def public_task(task: dict) -> dict:
    keep = ("id", "device", "command", "shell", "cwd", "verify_command", "collect_paths", "timeout_s", "status",
            "exit_code", "stdout", "stderr", "verify_exit_code", "verify_output", "artifacts", "duration_ms",
            "error", "requested_by", "created_at", "finished_at")
    return {k: task.get(k) for k in keep}


def _skill_meta(skill_md: Path) -> dict:
    """name/description from a SKILL.md front matter, without a YAML dependency."""
    meta = {"name": skill_md.parent.name, "description": ""}
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return meta
    if text.startswith("---"):
        for line in text.split("---", 2)[1].splitlines():
            key, _, value = line.partition(":")
            if key.strip() in ("name", "description") and value.strip():
                meta[key.strip()] = value.strip().strip("'\"")
    return meta


def list_skills(home: Path | None, extra_dirs: list[Path]) -> dict:
    installed, pending = [], []
    roots = ([home / "skills"] if home else []) + extra_dirs
    for root in roots:
        if root.is_dir():
            for skill_md in sorted(root.rglob("SKILL.md")):
                if ".hub" in skill_md.parts or "pending" in skill_md.parts:
                    continue
                installed.append(_skill_meta(skill_md) | {"source": "seed" if root in extra_dirs else "local",
                                                          "updated": skill_md.stat().st_mtime})
    staged = home / "pending" / "skills" if home else None
    if staged and staged.is_dir():
        for entry in sorted(staged.iterdir()):
            skill_md = next(iter(entry.rglob("SKILL.md")), None) if entry.is_dir() else None
            meta = _skill_meta(skill_md) if skill_md else {"name": entry.stem, "description": ""}
            pending.append(meta | {"id": entry.name, "staged": entry.stat().st_mtime})
    return {"installed": installed, "pending": pending}


def create_app(store: Store, admin_token: str, api_url: str = "", api_key: str = "",
               home: Path | None = None, seed_skills: Path | None = None,
               shared_conversation: bool = True) -> FastAPI:
    """``shared_conversation``: every paired device continues one conversation with Mercury (the
    default, for a personal hub). False gives each device its own thread."""
    app = FastAPI(title="Mercury relay", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    hub = Hub(store, admin_token)
    app.state.hub = hub
    store.fail_unfinished(reason="hub restarted before the device returned a result")

    def require_admin(authorization: str = Header(default="")) -> None:
        if not hub.admin_token:
            raise HTTPException(503, "MERCURY_ADMIN_TOKEN is not configured on the hub.")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), hub.admin_token.encode()):
            raise HTTPException(401, "Invalid admin token.")

    def require_device(authorization: str = Header(default="")) -> dict:
        scheme, _, token = authorization.partition(" ")
        device = store.device_by_token(token) if scheme.lower() == "bearer" and token else None
        if not device:
            raise HTTPException(401, "Unknown or revoked device token.")
        return device

    async def agent_api(method: str, path: str, body: dict | None = None) -> dict:
        import httpx

        if not api_url or not api_key:
            raise HTTPException(503, "The agent API is not configured on the hub (API_SERVER_KEY).")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(method, api_url + path, json=body,
                                                headers={"Authorization": f"Bearer {api_key}"})
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Mercury's agent is not reachable on the hub ({exc.__class__.__name__}).")
        if response.status_code >= 400:
            raise HTTPException(502 if response.status_code >= 500 else response.status_code,
                                f"agent API: {response.text[:300]}")
        return response.json()

    def owned_run(run_id: str, device: dict) -> None:
        if hub.ask_runs.get(run_id) != device["id"]:
            raise HTTPException(404, "No such request from this device.")

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "service": "mercury-relay", "version": __version__, "protocol": PROTOCOL_VERSION}

    @app.post("/v1/pair")
    def pair(body: PairRequest, request: Request) -> dict:
        client = request.client.host if request.client else "unknown"
        if not hub.pair_allowed(client):
            raise HTTPException(429, "Too many failed pairing attempts. Try again later.")
        try:
            device, token = store.redeem_pairing_code(body.code, body.platform, body.hostname, body.node_version,
                                                      body.capabilities)
        except PermissionError as exc:
            hub.pair_failed(client)
            raise HTTPException(403, str(exc))
        log.info("paired device %s (%s, %s)", device["name"], body.platform, body.hostname)
        return {"device_id": device["id"], "device_name": device["name"], "device_token": token,
                "approval": device["approval"], "protocol": PROTOCOL_VERSION}

    @app.websocket("/v1/node")
    async def node(websocket: WebSocket) -> None:
        scheme, _, token = websocket.headers.get("authorization", "").partition(" ")
        device = store.device_by_token(token) if scheme.lower() == "bearer" and token else None
        if not device:
            await websocket.close(code=4401, reason="unknown or revoked device token")
            return
        await websocket.accept()
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), HELLO_TIMEOUT_S)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            await websocket.close(code=4400, reason="expected hello")
            return
        if hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL_VERSION:
            await websocket.close(code=4400, reason=f"expected hello with protocol {PROTOCOL_VERSION}")
            return
        store.touch_device(device["id"], platform=hello.get("platform"), hostname=hello.get("hostname"),
                           node_version=hello.get("node_version"),
                           capabilities=hello.get("capabilities") if isinstance(hello.get("capabilities"), dict) else None)
        previous = hub.connections.get(device["id"])
        conn = Connection(device["id"], websocket)
        hub.connections[device["id"]] = conn
        if previous:
            try:
                await previous.websocket.close(code=4409, reason="replaced by a newer connection")
            except Exception:
                pass
        await conn.send({"type": "welcome", "device_id": device["id"], "device_name": device["name"],
                         "approval": device["approval"], "protocol": PROTOCOL_VERSION})
        log.info("device %s connected", device["name"])
        try:
            while True:
                message = await websocket.receive_json()
                if isinstance(message, dict):
                    hub.handle_node_message(device["id"], message)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            hub.disconnected(conn)
            log.info("device %s disconnected", device["name"])

    @app.post("/v1/ask")
    async def ask(body: AskRequest, device: dict = Depends(require_device)) -> dict:
        key, base = (("conversation", "mercury-main") if shared_conversation
                     else (f"conversation:{device['id']}", f"mercury-device-{device['id'][:12]}"))
        if body.new_conversation:
            store.set_setting(key, f"{base}-{int(time.time())}")
        session_id = store.get_setting(key, base)
        message = (f"[Sent from the user's device '{device['name']}' ({device['platform'] or 'unknown OS'}). "
                   f"Reply in plain text suitable for a small screen.]\n\n{body.message}")
        run = await agent_api("POST", "/v1/runs", {"input": message, "session_id": session_id})
        run_id = str(run.get("run_id") or "")
        if not run_id:
            raise HTTPException(502, "agent API did not return a run id")
        hub.ask_runs[run_id] = device["id"]
        store.touch_device(device["id"])
        log.info("device %s asked Mercury (run %s)", device["name"], run_id)
        return {"run_id": run_id, "status": run.get("status", "started")}

    @app.get("/v1/ask/{run_id}")
    async def ask_status(run_id: str, device: dict = Depends(require_device)) -> dict:
        owned_run(run_id, device)
        run = await agent_api("GET", f"/v1/runs/{run_id}")
        return {k: run.get(k) for k in ("run_id", "status", "output", "error", "pending_controls")}

    @app.post("/v1/ask/{run_id}/approval")
    async def ask_approval(run_id: str, body: AskApproval, device: dict = Depends(require_device)) -> dict:
        owned_run(run_id, device)
        payload = {"request_id": body.request_id, "choice": body.choice}
        if body.execution_generation is not None:
            payload["execution_generation"] = body.execution_generation
        log.info("device %s answered approval %s on run %s: %s", device["name"], body.request_id, run_id, body.choice)
        return await agent_api("POST", f"/v1/runs/{run_id}/approval", payload)

    @app.get("/v1/admin/devices", dependencies=[Depends(require_admin)])
    def devices(include_revoked: bool = False) -> dict:
        return {"devices": [public_device(d, hub) for d in store.devices(include_revoked)]}

    @app.post("/v1/admin/pairing", dependencies=[Depends(require_admin)])
    def pairing(body: PairingCodeRequest) -> dict:
        try:
            code, expires = store.create_pairing_code(body.name, body.approval, body.ttl_s)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"code": code, "name": body.name, "approval": body.approval, "expires_at": expires}

    @app.post("/v1/admin/devices/{name}/revoke", dependencies=[Depends(require_admin)])
    async def revoke(name: str) -> dict:
        try:
            device = store.revoke(name)
        except KeyError:
            raise HTTPException(404, f"No paired device named {name!r}.")
        conn = hub.connections.get(device["id"])
        if conn:
            try:
                await conn.websocket.close(code=4403, reason="device revoked")
            except Exception:
                pass
        return {"revoked": name}

    @app.post("/v1/admin/devices/{name}/policy", dependencies=[Depends(require_admin)])
    def set_policy(name: str, body: PolicyRequest) -> dict:
        try:
            if body.approval is not None:
                store.set_approval(name, body.approval)
            if body.blocked_patterns is not None:
                store.set_blocked_patterns(name, body.blocked_patterns)
        except KeyError:
            raise HTTPException(404, f"No paired device named {name!r}.")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return public_device(store.device_by_name(name), hub)

    @app.post("/v1/admin/tasks", dependencies=[Depends(require_admin)])
    async def submit(body: TaskRequest) -> dict:
        return public_task(await hub.submit(body))

    @app.get("/v1/admin/tasks", dependencies=[Depends(require_admin)])
    def list_tasks(device: str | None = None, limit: int = 20) -> dict:
        return {"tasks": [public_task(t) for t in store.tasks(device, limit)]}

    @app.get("/v1/admin/skills", dependencies=[Depends(require_admin)])
    def skills() -> dict:
        listed = list_skills(home, [seed_skills] if seed_skills else [])
        return listed | {"stats": store.skill_stats()}

    @app.get("/v1/admin/tasks/{task_id}", dependencies=[Depends(require_admin)])
    def get_task(task_id: str) -> dict:
        task = store.task(task_id)
        if not task:
            raise HTTPException(404, f"No task {task_id!r}.")
        return public_task(task)

    return app
