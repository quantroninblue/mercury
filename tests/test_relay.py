import time
from pathlib import Path

import httpx

from conftest import NodeRunner
from fake_node import FakeNode, pair


def _paired_node(relay, name="laptop", approval="dangerous", **node_kwargs):
    code = relay.admin("POST", "/v1/admin/pairing", json={"name": name, "approval": approval}).json()["code"]
    creds = pair(relay.url, code)
    runner = NodeRunner(FakeNode(relay.url, creds["device_token"], **node_kwargs)).start()
    return creds, runner


def _run(relay, **body):
    body.setdefault("device", "laptop")
    return relay.admin("POST", "/v1/admin/tasks", json=body).json()


def test_health_is_public_and_admin_requires_token(relay):
    assert httpx.get(relay.url + "/health").json()["ok"] is True
    assert httpx.get(relay.url + "/v1/admin/devices").status_code == 401
    bad = httpx.get(relay.url + "/v1/admin/devices", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


def test_pair_connect_and_list_online(relay):
    creds, runner = _paired_node(relay)
    assert runner.node.welcome["device_name"] == "laptop"
    assert creds["device_name"] == "laptop" and creds["device_token"]
    devices = relay.admin("GET", "/v1/admin/devices").json()["devices"]
    assert devices[0]["online"] is True and devices[0]["platform"] == "linux"


def test_verified_task_round_trip(relay, tmp_path):
    _paired_node(relay)
    task = _run(relay, command="echo mercury > proof.txt && echo done", cwd=str(tmp_path),
                verify_command="grep -q mercury proof.txt", collect_paths=["proof.txt"])
    assert task["status"] == "verified", task
    assert task["exit_code"] == 0 and task["stdout"].strip() == "done" and task["verify_exit_code"] == 0
    assert task["artifacts"][0]["path"] == "proof.txt" and len(task["artifacts"][0]["sha256"]) == 64
    listed = relay.admin("GET", "/v1/admin/tasks").json()["tasks"]
    assert listed[0]["id"] == task["id"]


def test_failed_and_unverified_outcomes(relay):
    _paired_node(relay)
    assert _run(relay, command="echo ok")["status"] == "completed"
    assert _run(relay, command="exit 3")["status"] == "failed"
    assert _run(relay, command="true", verify_command="false")["status"] == "verify_failed"


def test_hard_floor_blocked_at_relay_even_without_agent(relay):
    _, runner = _paired_node(relay, approval="none")
    task = _run(relay, command="rm -rf /")
    assert task["status"] == "blocked" and "refused" in task["error"]
    assert not any(m.get("type") == "task" for m in runner.node.received)


def test_offline_device_and_unknown_device(relay):
    relay.admin("POST", "/v1/admin/pairing", json={"name": "phone"})
    code = relay.admin("POST", "/v1/admin/pairing", json={"name": "pc"}).json()["code"]
    pair(relay.url, code)
    assert _run(relay, device="pc", command="echo hi")["status"] == "offline"
    assert relay.admin("POST", "/v1/admin/tasks", json={"device": "ghost", "command": "x"}).status_code == 404


def test_node_local_policy_rejection(relay):
    _paired_node(relay, reject={"secret"})
    task = _run(relay, command="cat secret.txt")
    assert task["status"] == "rejected" and "local policy" in task["error"]


def test_timeout_sends_cancel(relay, monkeypatch):
    import mercury_hub.relay as relay_module

    monkeypatch.setattr(relay_module, "RESULT_GRACE_S", 0)
    _, runner = _paired_node(relay, hang=True)
    task = _run(relay, command="sleep 100", timeout_s=1)
    assert task["status"] == "timeout"
    time.sleep(0.2)
    assert any(m.get("type") == "cancel" and m["task_id"] == task["id"] for m in runner.node.received)


def test_async_task_then_poll(relay):
    _paired_node(relay)
    task = _run(relay, command="sleep 0.3; echo later", wait=False)
    assert task["status"] in ("sent", "running")
    for _ in range(50):
        polled = relay.admin("GET", f"/v1/admin/tasks/{task['id']}").json()
        if polled["status"] == "completed":
            break
        time.sleep(0.05)
    assert polled["status"] == "completed" and polled["stdout"].strip() == "later"


def test_revoke_disconnects_and_token_stops_working(relay):
    creds, runner = _paired_node(relay)
    assert relay.admin("POST", "/v1/admin/devices/laptop/revoke").status_code == 200
    runner.join()
    assert runner.node.close_code == 4403
    again = NodeRunner(FakeNode(relay.url, creds["device_token"])).start()
    again.join()
    assert again.node.welcome is None and again.node.rejected_status == 403


def test_bad_device_token_and_pairing_rate_limit(relay):
    node = NodeRunner(FakeNode(relay.url, "not-a-token")).start()
    node.join()
    assert node.node.welcome is None and node.node.rejected_status == 403
    for _ in range(5):
        assert httpx.post(relay.url + "/v1/pair", json={"code": "WRONG123"}).status_code == 403
    assert httpx.post(relay.url + "/v1/pair", json={"code": "WRONG123"}).status_code == 429


def test_policy_update(relay):
    _paired_node(relay)
    updated = relay.admin("POST", "/v1/admin/devices/laptop/policy",
                          json={"approval": "all", "blocked_patterns": ["git push*"]}).json()
    assert updated["approval"] == "all" and updated["blocked_patterns"] == ["git push*"]
    assert _run(relay, command="git push origin main")["status"] == "blocked"


def test_newer_connection_replaces_older(relay):
    creds, first = _paired_node(relay)
    second = NodeRunner(FakeNode(relay.url, creds["device_token"])).start()
    first.join()
    assert first.node.close_code == 4409
    assert second.node.welcome is not None
    assert _run(relay, command="echo still works")["status"] == "completed"


def test_skills_listing(tmp_path):
    from conftest import ADMIN, Relay
    from mercury_hub.relay import create_app

    home = tmp_path / "home"
    (home / "skills" / "mercury-learned" / "battery").mkdir(parents=True)
    (home / "skills" / "mercury-learned" / "battery" / "SKILL.md").write_text(
        "---\nname: windows-battery-health\ndescription: Battery report for Windows\n---\n# x\n")
    (home / "pending" / "skills" / "abc123" / "disk").mkdir(parents=True)
    (home / "pending" / "skills" / "abc123" / "disk" / "SKILL.md").write_text("---\nname: disk-report\n---\n")
    relay = Relay(tmp_path / "hub.db")
    relay.app = create_app(relay.store, ADMIN, home=home, seed_skills=Path(__file__).resolve().parents[1] / "skills")
    relay.server.config.app = relay.app
    relay.start()
    try:
        data = relay.admin("GET", "/v1/admin/skills").json()
    finally:
        relay.stop()
    names = {s["name"]: s["source"] for s in data["installed"]}
    assert names["windows-battery-health"] == "local" and names["mercury-device-tasks"] == "seed"
    assert data["pending"] == [{"name": "disk-report", "description": "", "id": "abc123",
                                "staged": data["pending"][0]["staged"]}]
    assert data["stats"] == []
