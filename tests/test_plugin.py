"""The agent plugin, loaded the way the engine loads it (register(ctx)), against a live relay."""

import importlib.util
import json

import pytest

from conftest import ADMIN, ROOT, NodeRunner
from fake_node import FakeNode, pair


class FakeCtx:
    def __init__(self):
        self.tools, self.hooks, self.commands = {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        assert schema["name"] == name and toolset == "mercury"
        self.tools[name] = handler

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler


@pytest.fixture
def plugin(relay, monkeypatch):
    monkeypatch.setenv("MERCURY_HOME", str(relay.store.path.parent))
    monkeypatch.setenv("MERCURY_DB", str(relay.store.path))
    monkeypatch.setenv("MERCURY_RELAY_URL", relay.url)
    monkeypatch.setenv("MERCURY_ADMIN_TOKEN", ADMIN)
    spec = importlib.util.spec_from_file_location("mercury_devices_plugin",
                                                  ROOT / "plugin" / "mercury-devices" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = FakeCtx()
    module.register(ctx)
    return module, ctx


def _connect(relay, name="laptop", approval="dangerous"):
    code = relay.admin("POST", "/v1/admin/pairing", json={"name": name, "approval": approval}).json()["code"]
    return NodeRunner(FakeNode(relay.url, pair(relay.url, code)["device_token"])).start()


def test_registers_tools_hooks_and_commands(plugin):
    _, ctx = plugin
    assert set(ctx.tools) == {"mercury_devices", "mercury_run", "mercury_task", "mercury_tasks", "mercury_skill_stats"}
    assert set(ctx.hooks) == {"pre_tool_call", "post_tool_call"}
    assert set(ctx.commands) == {"devices", "device-tasks"}


def test_devices_and_run_tools(plugin, relay, tmp_path):
    _, ctx = plugin
    _connect(relay)
    devices = json.loads(ctx.tools["mercury_devices"]({}))
    assert devices["success"] and devices["devices"][0]["online"]
    out = json.loads(ctx.tools["mercury_run"]({"device": "laptop", "command": "echo hi > f && cat f",
                                               "verify_command": "test -s f", "cwd": str(tmp_path)},
                                              session_id="s1"))
    assert out["success"] and out["task"]["verified"] and out["task"]["stdout"].strip() == "hi"
    task = json.loads(ctx.tools["mercury_task"]({"task_id": out["task"]["id"]}))
    assert task["task"]["status"] == "verified"
    assert "laptop" in ctx.commands["devices"]("")
    assert out["task"]["id"] in ctx.commands["device-tasks"]("")


def test_unverified_result_is_flagged(plugin, relay):
    _, ctx = plugin
    _connect(relay)
    out = json.loads(ctx.tools["mercury_run"]({"device": "laptop", "command": "echo hi"}))
    assert out["task"]["status"] == "completed" and "unverified" in out["note"]


def test_pre_tool_call_directives(plugin, relay):
    _, ctx = plugin
    _connect(relay)
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="terminal", args={"command": "rm -rf /"}) is None  # not ours
    assert hook(tool_name="mercury_run", args={"device": "laptop", "command": "Get-Date"}) is None
    approve = hook(tool_name="mercury_run", args={"device": "laptop", "command": "Remove-Item old.zip"})
    assert approve["action"] == "approve" and approve["rule_key"] == "mercury:laptop:delete"
    assert "laptop" in approve["message"]
    block = hook(tool_name="mercury_run", args={"device": "laptop", "command": "format c:"})
    assert block["action"] == "block" and "refused" in block["message"]
    sneaky = hook(tool_name="mercury_run", args={"device": "laptop", "command": "echo ok",
                                                 "verify_command": "del important.txt"})
    assert sneaky["action"] == "approve"  # verify commands are policed too


def test_skill_outcomes_recorded_from_session(plugin, relay):
    module, ctx = plugin
    _connect(relay)
    post = ctx.hooks["post_tool_call"]
    post(tool_name="skill_view", args={"name": "battery-health"}, result="{}", session_id="s9")
    for command in ("echo ok", "exit 1"):
        args = {"device": "laptop", "command": command}
        result = ctx.tools["mercury_run"](args, session_id="s9")
        post(tool_name="mercury_run", args=args, result=result, session_id="s9")
    stats = json.loads(ctx.tools["mercury_skill_stats"]({}))["skills"]
    assert stats[0]["skill"] == "battery-health" and stats[0]["uses"] == 2 and stats[0]["success_rate"] == 0.5


def test_relay_down_is_reported_not_raised(plugin, monkeypatch):
    _, ctx = plugin
    monkeypatch.setenv("MERCURY_RELAY_URL", "http://127.0.0.1:9")
    out = json.loads(ctx.tools["mercury_devices"]({}))
    assert out["success"] is False and "unavailable" in out["error"]
