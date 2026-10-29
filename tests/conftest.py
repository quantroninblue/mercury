import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mercury_hub.db import Store  # noqa: E402
from mercury_hub.relay import create_app  # noqa: E402

ADMIN = "test-admin-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Relay:
    def __init__(self, db_path: Path):
        self.store = Store(db_path)
        self.app = create_app(self.store, ADMIN)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    ws_ping_interval=None))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            try:
                if httpx.get(self.url + "/health").status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("relay did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)

    def admin(self, method: str, path: str, **kwargs) -> httpx.Response:
        return httpx.request(method, self.url + path, headers={"Authorization": f"Bearer {ADMIN}"},
                             timeout=kwargs.pop("timeout", 30), **kwargs)


@pytest.fixture
def relay(tmp_path):
    r = Relay(tmp_path / "hub.db").start()
    yield r
    r.stop()


class NodeRunner:
    """Run a FakeNode in a background event loop thread."""

    def __init__(self, node):
        self.node = node
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._main, daemon=True)

    def _main(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.node.run())

    def start(self, wait_connected: bool = True):
        self.thread.start()
        if wait_connected:
            deadline = time.time() + 5
            while not self.node.connected.is_set() and time.time() < deadline:
                time.sleep(0.02)
        return self

    def join(self, timeout=5):
        self.thread.join(timeout)
