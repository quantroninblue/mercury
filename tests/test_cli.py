"""The `mercury` admin CLI against a real relay process (python -m mercury_hub relay)."""

import json
import os
import re
import subprocess
import sys
import time

import httpx
import pytest

from conftest import ROOT, NodeRunner, _free_port
from fake_node import FakeNode, pair


@pytest.fixture
def hub(tmp_path):
    port = _free_port()
    env = {**os.environ, "MERCURY_HOME": str(tmp_path), "MERCURY_RELAY_PORT": str(port),
           "MERCURY_ADMIN_TOKEN": "cli-test-token", "MERCURY_PUBLIC_URL": "https://hub.test",
           "PYTHONPATH": str(ROOT)}
    env.pop("HERMES_HOME", None)
    relay = subprocess.Popen([sys.executable, "-m", "mercury_hub", "relay"], env=env, cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url + "/health").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        relay.kill()
        pytest.fail("relay did not start: " + relay.stdout.read())

    def cli(*args, check=True):
        done = subprocess.run([sys.executable, "-m", "mercury_hub", *args], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        if check and done.returncode != 0:
            raise AssertionError(f"mercury {' '.join(args)} failed: {done.stdout}{done.stderr}")
        return done

    yield url, cli, tmp_path
    relay.terminate()
    relay.wait(10)


def test_cli_end_to_end(hub, tmp_path):
    url, cli, home = hub
    assert "relay: up" in cli("status").stdout

    out = cli("pair", "laptop", "--approval", "dangerous").stdout
    code = re.search(r"code for 'laptop': (\w{4}-\w{4})", out).group(1)
    assert "mercury-node pair --hub https://hub.test --code " + code in out

    NodeRunner(FakeNode(url, pair(url, code)["device_token"])).start()
    devices = json.loads(cli("devices", "--json").stdout)
    assert devices[0]["name"] == "laptop" and devices[0]["online"] is True

    run = cli("run", "laptop", "echo verified-output > r.txt && cat r.txt", "--verify", "test -s r.txt",
              "--collect", "r.txt", "--cwd", str(tmp_path), "--json")
    task = json.loads(run.stdout)
    assert task["status"] == "verified" and task["stdout"].strip() == "verified-output"
    assert task["artifacts"][0]["path"] == "r.txt"

    failed = cli("run", "laptop", "exit 7", check=False)
    assert failed.returncode == 1 and "failed (exit 7" in failed.stdout

    log = cli("tasks").stdout
    assert task["id"] in log and "verified" in log

    cli("policy", "laptop", "--approval", "all", "--block", "git push*")
    assert json.loads(cli("devices", "--json").stdout)[0]["approval"] == "all"

    cli("revoke", "laptop")
    assert json.loads(cli("devices", "--json").stdout) == []
    assert (home / "mercury" / "hub.db").exists()


def test_admin_token_write(hub):
    _, cli, home = hub
    env_file = home / ".env"
    env_file.write_text("OTHER=1\n")
    cli("admin-token", "--write")
    text = env_file.read_text()
    assert "OTHER=1" in text and re.search(r"^MERCURY_ADMIN_TOKEN=\S{40,}$", text, re.M)
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    assert cli("admin-token", "--write", check=False).returncode != 0  # refuses to clobber
