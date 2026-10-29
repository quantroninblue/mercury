import time

import pytest

from mercury_hub.db import Store, sha256


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "hub.db")


def test_pairing_code_is_single_use_and_token_is_hashed(store):
    code, _ = store.create_pairing_code("laptop")
    device, token = store.redeem_pairing_code(code.lower()[:4] + "-" + code[4:], "windows", "LAPTOP-1", "0.1.0")
    assert device["name"] == "laptop" and device["platform"] == "windows"
    assert "token_hash" not in device
    assert store.device_by_token(token)["id"] == device["id"]
    raw = store._conn.execute("SELECT token_hash FROM devices").fetchone()[0]
    assert raw == sha256(token) and token not in raw
    with pytest.raises(PermissionError):
        store.redeem_pairing_code(code, "windows", "LAPTOP-1", "0.1.0")


def test_expired_and_unknown_codes_rejected(store):
    code, _ = store.create_pairing_code("phone", ttl_s=-1)
    with pytest.raises(PermissionError):
        store.redeem_pairing_code(code, "android", "pixel", "0.1.0")
    with pytest.raises(PermissionError):
        store.redeem_pairing_code("ABCDEFGH", "android", "pixel", "0.1.0")


def test_duplicate_names_and_bad_modes_rejected(store):
    code, _ = store.create_pairing_code("laptop")
    store.redeem_pairing_code(code, "windows", "h", "v")
    with pytest.raises(ValueError):
        store.create_pairing_code("laptop")
    with pytest.raises(ValueError):
        store.create_pairing_code("other", approval="sometimes")


def test_revoke_kills_token_and_frees_name(store):
    code, _ = store.create_pairing_code("laptop")
    device, token = store.redeem_pairing_code(code, "windows", "h", "v")
    store.create_task(device["id"], "echo hi")
    store.revoke("laptop")
    assert store.device_by_token(token) is None
    assert store.devices() == []
    code2, _ = store.create_pairing_code("laptop")
    device2, _ = store.redeem_pairing_code(code2, "windows", "h", "v")
    assert device2["id"] != device["id"]
    assert len(store.devices(include_revoked=True)) == 2  # old record kept for its task history


def _device(store):
    code, _ = store.create_pairing_code("dev")
    return store.redeem_pairing_code(code, "linux", "h", "v")[0]


def test_result_status_derivation(store):
    dev = _device(store)
    cases = [
        ({"exit_code": 0}, "", "completed"),
        ({"exit_code": 0, "verify_exit_code": 0}, "test -f x", "verified"),
        ({"exit_code": 0, "verify_exit_code": 1}, "test -f x", "verify_failed"),
        ({"exit_code": 2, "verify_exit_code": 0}, "test -f x", "failed"),
        ({"exit_code": "0"}, "", "failed"),
    ]
    for result, verify, expected in cases:
        task = store.create_task(dev["id"], "cmd", verify_command=verify, status="sent")
        assert store.record_result(task["id"], result)["status"] == expected, (result, verify)


def test_final_task_is_immutable(store):
    dev = _device(store)
    task = store.create_task(dev["id"], "cmd", status="sent")
    store.set_task_status(task["id"], "timeout", "too slow")
    late = store.record_result(task["id"], {"exit_code": 0})
    assert late["status"] == "timeout" and late["error"] == "too slow"


def test_output_is_clipped(store):
    dev = _device(store)
    task = store.create_task(dev["id"], "cmd", status="sent")
    stored = store.record_result(task["id"], {"exit_code": 0, "stdout": "x" * 100_000})
    assert len(stored["stdout"]) < 70_000 and stored["stdout"].endswith("[truncated by hub]")


def test_skill_stats(store):
    dev = _device(store)
    ok = store.create_task(dev["id"], "a", status="sent")
    bad = store.create_task(dev["id"], "b", status="sent")
    store.record_result(ok["id"], {"exit_code": 0})
    store.record_result(bad["id"], {"exit_code": 1, "error": "boom"})
    store.record_skill_outcome("battery-report", ok["id"], "completed")
    store.record_skill_outcome("battery-report", bad["id"], "failed")
    store.record_skill_outcome("largest-files", ok["id"], "verified")
    stats = store.skill_stats()
    assert [s["skill"] for s in stats] == ["battery-report", "largest-files"]
    assert stats[0]["success_rate"] == 0.5 and stats[0]["recent_failures"][0]["command"] == "b"


def test_fail_unfinished(store):
    dev = _device(store)
    task = store.create_task(dev["id"], "cmd", status="running")
    assert store.fail_unfinished(reason="restart") == 1
    assert store.task(task["id"])["status"] == "failed"
    assert time.time() - store.task(task["id"])["finished_at"] < 5
