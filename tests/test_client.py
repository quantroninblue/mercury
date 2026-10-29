"""A full node session against a stand-in hub that speaks the Mercury protocol."""

import asyncio
import json
import os

import pytest
import websockets

from mercury_node import client
from mercury_node.policy import LocalPolicy

pytestmark = pytest.mark.skipif(os.name == "nt", reason="uses sh commands")


class FakeHub:
    def __init__(self, tasks, token="good", close_code=None):
        self.tasks, self.token, self.close_code = tasks, token, close_code
        self.hello, self.messages = None, []
        self.done = asyncio.Event()

    async def handler(self, ws):
        if ws.request.headers.get("Authorization") != f"Bearer {self.token}":
            await ws.close(4401)
            return
        self.hello = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "welcome", "device_name": "laptop", "approval": "dangerous", "protocol": 1}))
        for task in self.tasks:
            await ws.send(json.dumps(task))
        results = 0
        while results < len(self.tasks):
            msg = json.loads(await ws.recv())
            self.messages.append(msg)
            if msg["type"] == "task_result" or (msg["type"] == "task_ack" and not msg["accepted"]):
                results += 1
        if self.close_code:
            await ws.close(self.close_code)
        self.done.set()
        await asyncio.sleep(0.2)

    async def process_request(self, connection, request):
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return connection.respond(403, "forbidden")
        return None


async def _session(hub: FakeHub, token="good", policy=None):
    async with websockets.serve(hub.handler, "127.0.0.1", 0, process_request=hub.process_request) as server:
        port = server.sockets[0].getsockname()[1]
        node = client.Node(f"http://127.0.0.1:{port}", token, policy_loader=lambda: policy or LocalPolicy())
        runner = asyncio.create_task(node.session())
        await asyncio.wait_for(hub.done.wait(), 15)
        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, websockets.ConnectionClosed, client.TokenRejected):
            pass
        return node


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MERCURY_NODE_HOME", str(tmp_path))


def test_hello_task_verify_and_audit(tmp_path):
    hub = FakeHub([{"type": "task", "task_id": "t1", "command": "echo mercury > f.txt && cat f.txt", "shell": "sh",
                    "cwd": str(tmp_path), "timeout_s": 10, "verify_command": "test -s f.txt",
                    "collect_paths": ["f.txt"]}])
    asyncio.run(_session(hub))
    assert hub.hello["type"] == "hello" and hub.hello["protocol"] == 1
    assert "sh" in hub.hello["capabilities"]["shells"]
    ack, result = hub.messages
    assert ack == {"type": "task_ack", "task_id": "t1", "accepted": True}
    assert result["exit_code"] == 0 and result["stdout"].strip() == "mercury" and result["verify_exit_code"] == 0
    assert result["artifacts"][0]["path"] == "f.txt" and result["artifacts"][0]["size"] == 8
    audit = (tmp_path / "tasks.log").read_text().splitlines()
    assert json.loads(audit[-1])["task_id"] == "t1"


def test_local_policy_refusal_is_reported_and_audited(tmp_path):
    hub = FakeHub([{"type": "task", "task_id": "t2", "command": "rm secrets.txt", "shell": "sh", "cwd": "",
                    "timeout_s": 10}])
    asyncio.run(_session(hub, policy=LocalPolicy(mode="read-only")))
    assert hub.messages == [{"type": "task_ack", "task_id": "t2", "accepted": False,
                             "reason": "this device is read-only (policy.json mode: read-only)"}]
    assert "refused" in (tmp_path / "tasks.log").read_text()


def test_timeout_is_capped_by_local_policy():
    hub = FakeHub([{"type": "task", "task_id": "t3", "command": "sleep 20", "shell": "sh", "cwd": "",
                    "timeout_s": 3600}])
    asyncio.run(_session(hub, policy=LocalPolicy(max_timeout_s=1)))
    assert hub.messages[-1]["exit_code"] == 124


def test_rejected_token_stops_instead_of_retrying():
    hub = FakeHub([], token="good")

    async def go():
        async with websockets.serve(hub.handler, "127.0.0.1", 0, process_request=hub.process_request) as server:
            port = server.sockets[0].getsockname()[1]
            node = client.Node(f"http://127.0.0.1:{port}", "bad")
            with pytest.raises(client.TokenRejected):
                await asyncio.wait_for(node.run_forever(), 10)

    asyncio.run(go())


def test_revoked_close_code_raises_token_rejected():
    hub = FakeHub([{"type": "task", "task_id": "t4", "command": "true", "shell": "sh", "cwd": "", "timeout_s": 5}],
                  close_code=4403)

    async def go():
        async with websockets.serve(hub.handler, "127.0.0.1", 0, process_request=hub.process_request) as server:
            port = server.sockets[0].getsockname()[1]
            node = client.Node(f"http://127.0.0.1:{port}", "good", policy_loader=LocalPolicy)
            with pytest.raises(client.TokenRejected):
                await asyncio.wait_for(node.session(), 10)

    asyncio.run(go())


def test_hub_down_means_backoff_not_crash(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            node.stopping = True

    node = client.Node("http://127.0.0.1:9", "tok")
    monkeypatch.setattr(client.asyncio, "sleep", fake_sleep)
    asyncio.run(node.run_forever())
    assert len(sleeps) == 3 and sleeps[1] > sleeps[0] * 1.3  # grows


def test_ws_url():
    assert client.ws_url("https://hub.example") == "wss://hub.example/v1/node"
    assert client.ws_url("http://10.0.0.5:8765") == "ws://10.0.0.5:8765/v1/node"
    with pytest.raises(ValueError):
        client.ws_url("hub.example")
