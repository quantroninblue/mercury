import json
import os
import stat

import pytest

from mercury_node import config


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MERCURY_NODE_HOME", str(tmp_path))
    return tmp_path


def test_save_load_delete(home):
    assert config.load_config() is None
    path = config.save_config("https://hub.example/", "laptop", "dev123", "secret-token")
    cfg = config.load_config()
    assert cfg["hub_url"] == "https://hub.example" and cfg["device_name"] == "laptop"
    assert cfg["device_token"] == "secret-token"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text())["token"] == "secret-token"
    else:
        assert "secret-token" not in path.read_text()  # DPAPI-sealed
    assert config.delete_config() and config.load_config() is None


def test_platform_name_is_known():
    assert config.platform_name() in {"windows", "android", "linux", "darwin"}
