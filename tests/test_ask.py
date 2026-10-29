"""/v1/ask: a paired device talks to the agent through the relay (agent API replaced by a stub)."""

import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException

from conftest import Relay, _free_port
from fake_node import pair

API_KEY = "agent-api-key"


class AgentStub:
    """Mimics the engine API server's /v1/runs endpoints, including one approval."""

    def __init__(self):
        self.app = FastAPI()
        self.runs: dict[str, dict] = {}
        self.approvals: list[dict] = []
        app = self.app

        def auth(authorization: str = Header(default="")):
            if authorization != f"Bearer {API_KEY}":
                raise HTTPException(401)

        @app.post("/v1/runs")
        def create(body: dict, authorization: str = Header(default="")):
            auth(authorization)
            run_id = f"run_{len(self.runs) + 1}"
            needs_approval = "delete" in body["input"]
            self.runs[run_id] = {"run_id": run_id, "status": "waiting_for_approval" if needs_approval else "completed",
                                 "output": None if needs_approval else f"echo: {body['input'][-20:]}",
                                 "session_id": body.get("session_id"), "input": body["input"],
                                 "pending_controls": [{"prompt_id": "p1", "execution_generation": 2,
                                                       "command": "Remove-Item old.zip"}] if needs_approval else []}
            return {"run_id": run_id, "status": "started"}

        @app.get("/v1/runs/{run_id}")
        def get(run_id: str, authorization: str = Header(default="")):
            auth(authorization)
            return self.runs[run_id]

        @app.post("/v1/runs/{run_id}/approval")
        def approve(run_id: str, body: dict, authorization: str = Header(default="")):
            auth(authorization)
            self.approvals.append(body)
            run = self.runs[run_id]
            run.update(status="completed", pending_controls=[],
                       output="deleted" if body["choice"] != "deny" else "not deleted")
            return {"ok": True}


@pytest.fixture
def setup(tmp_path):
    stub = AgentStub()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(stub.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/v1/runs/x")
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    relay = Relay(tmp_path / "hub.db")
    from mercury_hub.relay import create_app

    relay.app = create_app(relay.store, "test-admin-token", f"http://127.0.0.1:{port}", API_KEY)
    relay.server.config.app = relay.app
    relay.start()
    code = relay.admin("POST", "/v1/admin/pairing", json={"name": "phone"}).json()["code"]
    token = pair(relay.url, code, platform="android")["device_token"]
    yield relay, stub, token
    relay.stop()
    server.should_exit = True


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_device_asks_and_gets_reply(setup):
    relay, stub, token = setup
    run = httpx.post(relay.url + "/v1/ask", json={"message": "battery health of my laptop?"}, headers=_h(token)).json()
    state = httpx.get(f"{relay.url}/v1/ask/{run['run_id']}", headers=_h(token)).json()
    assert state["status"] == "completed" and state["output"].startswith("echo:")
    sent = stub.runs[run["run_id"]]
    assert "Sent from the user's device 'phone' (android)" in sent["input"]
    assert sent["session_id"] == "mercury-main"


def test_devices_share_one_conversation_until_new(setup):
    relay, stub, token = setup
    code = relay.admin("POST", "/v1/admin/pairing", json={"name": "laptop"}).json()["code"]
    laptop = pair(relay.url, code)["device_token"]
    first = httpx.post(relay.url + "/v1/ask", json={"message": "one"}, headers=_h(token)).json()["run_id"]
    second = httpx.post(relay.url + "/v1/ask", json={"message": "two"}, headers=_h(laptop)).json()["run_id"]
    assert stub.runs[first]["session_id"] == stub.runs[second]["session_id"] == "mercury-main"
    fresh = httpx.post(relay.url + "/v1/ask", json={"message": "fresh", "new_conversation": True},
                       headers=_h(token)).json()["run_id"]
    after = httpx.post(relay.url + "/v1/ask", json={"message": "next"}, headers=_h(laptop)).json()["run_id"]
    assert stub.runs[fresh]["session_id"] != "mercury-main"
    assert stub.runs[after]["session_id"] == stub.runs[fresh]["session_id"]  # the new thread is shared too
    assert relay.store.get_setting("conversation") == stub.runs[fresh]["session_id"]  # survives restarts


def test_per_device_conversations(tmp_path):
    from mercury_hub.relay import create_app

    stub = AgentStub()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(stub.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(0.5)
    relay = Relay(tmp_path / "hub.db")
    relay.app = create_app(relay.store, "test-admin-token", f"http://127.0.0.1:{port}", API_KEY,
                           shared_conversation=False)
    relay.server.config.app = relay.app
    relay.start()
    try:
        tokens = []
        for name in ("phone", "laptop"):
            code = relay.admin("POST", "/v1/admin/pairing", json={"name": name}).json()["code"]
            tokens.append(pair(relay.url, code)["device_token"])
        runs = [httpx.post(relay.url + "/v1/ask", json={"message": "hi"}, headers=_h(t)).json()["run_id"]
                for t in tokens]
        assert stub.runs[runs[0]]["session_id"] != stub.runs[runs[1]]["session_id"]
        assert all(stub.runs[r]["session_id"].startswith("mercury-device-") for r in runs)
    finally:
        relay.stop()
        server.should_exit = True


def test_approval_round_trip_from_device(setup):
    relay, stub, token = setup
    run_id = httpx.post(relay.url + "/v1/ask", json={"message": "delete old.zip on laptop"},
                        headers=_h(token)).json()["run_id"]
    state = httpx.get(f"{relay.url}/v1/ask/{run_id}", headers=_h(token)).json()
    assert state["status"] == "waiting_for_approval" and state["pending_controls"][0]["prompt_id"] == "p1"
    answer = httpx.post(f"{relay.url}/v1/ask/{run_id}/approval",
                        json={"request_id": "p1", "choice": "once", "execution_generation": 2}, headers=_h(token))
    assert answer.status_code == 200
    assert stub.approvals == [{"request_id": "p1", "choice": "once", "execution_generation": 2}]
    assert httpx.get(f"{relay.url}/v1/ask/{run_id}", headers=_h(token)).json()["output"] == "deleted"


def test_ask_requires_device_token_and_ownership(setup):
    relay, stub, token = setup
    assert httpx.post(relay.url + "/v1/ask", json={"message": "hi"}).status_code == 401
    assert httpx.post(relay.url + "/v1/ask", json={"message": "hi"}, headers=_h("bogus")).status_code == 401
    run_id = httpx.post(relay.url + "/v1/ask", json={"message": "hi"}, headers=_h(token)).json()["run_id"]
    code = relay.admin("POST", "/v1/admin/pairing", json={"name": "other"}).json()["code"]
    other = pair(relay.url, code)["device_token"]
    assert httpx.get(f"{relay.url}/v1/ask/{run_id}", headers=_h(other)).status_code == 404
    assert httpx.post(f"{relay.url}/v1/ask/{run_id}/approval", json={"request_id": "p1", "choice": "once"},
                      headers=_h(other)).status_code == 404
    relay.admin("POST", "/v1/admin/devices/phone/revoke")
    assert httpx.post(relay.url + "/v1/ask", json={"message": "hi"}, headers=_h(token)).status_code == 401


def test_ask_when_agent_api_not_configured(tmp_path):
    from mercury_hub.relay import create_app

    relay = Relay(tmp_path / "hub.db")
    relay.app = create_app(relay.store, "test-admin-token")
    relay.server.config.app = relay.app
    relay.start()
    try:
        code = relay.admin("POST", "/v1/admin/pairing", json={"name": "phone"}).json()["code"]
        token = pair(relay.url, code)["device_token"]
        reply = httpx.post(relay.url + "/v1/ask", json={"message": "hi"}, headers=_h(token))
        assert reply.status_code == 503 and "API_SERVER_KEY" in reply.json()["detail"]
    finally:
        relay.stop()
