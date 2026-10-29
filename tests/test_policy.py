import json

import pytest

from mercury_node.policy import LocalPolicy


def test_defaults_allow_normal_commands():
    assert LocalPolicy().check("Get-ChildItem $env:USERPROFILE\\Downloads") is None
    assert LocalPolicy().check("rm notes.txt") is None  # hub asks the user; the device allows by default


@pytest.mark.parametrize("command", ["format c:", "rm -rf /", "diskpart", "Remove-Item -Recurse C:\\Windows",
                                     "mkfs.ext4 /dev/sdb1", "bcdedit /deletevalue x"])
def test_floor_always_refuses(command):
    assert "safety floor" in LocalPolicy().check(command)


def test_floor_applies_to_verify_command_too():
    assert LocalPolicy().check("echo ok", verify_command="format d:")


def test_paused_refuses_everything():
    assert "paused" in LocalPolicy(mode="paused").check("Get-Date")


@pytest.mark.parametrize("command", ["Remove-Item a.txt", "echo hi > out.txt", "Set-Content x.txt 1", "mkdir new",
                                     "Stop-Process -Name chrome", "pip install x", "git push"])
def test_read_only_refuses_writes(command):
    assert "read-only" in LocalPolicy(mode="read-only").check(command)


@pytest.mark.parametrize("command", ["Get-CimInstance Win32_Battery", "dir C:\\Users", "Get-Content log.txt",
                                     "ls -la", "termux-battery-status", "cat a.txt 2>&1",
                                     "Get-Process | Sort-Object CPU"])
def test_read_only_allows_reads(command):
    assert LocalPolicy(mode="read-only").check(command) is None


def test_blocked_patterns():
    policy = LocalPolicy(blocked_patterns=["*Finance*"])
    assert "blocked pattern" in policy.check("Get-ChildItem D:\\Finance\\2026")
    assert policy.check("Get-ChildItem D:\\Photos") is None


def test_allowed_cwd(tmp_path):
    inside = tmp_path / "work"
    inside.mkdir()
    policy = LocalPolicy(allowed_cwd=[str(inside)])
    assert policy.check("ls", cwd=str(inside)) is None
    assert policy.check("ls", cwd=str(inside / "sub")) is None
    assert "outside" in policy.check("ls", cwd=str(tmp_path))


def test_load_write_defaults_and_corrupt_file(tmp_path):
    path = tmp_path / "policy.json"
    LocalPolicy.write_defaults(path)
    assert json.loads(path.read_text())["mode"] == "normal"
    path.write_text(json.dumps({"mode": "read-only", "confirm_on_device": True}))
    loaded = LocalPolicy.load(path)
    assert loaded.mode == "read-only" and loaded.confirm_on_device and loaded.max_timeout_s == 900
    path.write_text("{not json")
    assert LocalPolicy.load(path).mode == "paused"  # broken policy fails closed
