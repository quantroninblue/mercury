import hashlib
import os
import threading
import time

import pytest

from mercury_node.runner import TIMEOUT_EXIT, Runner, argv_for, file_artifacts

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX shell test")


def test_powershell_and_cmd_argv():
    ps = argv_for("powershell", "Get-Date")
    assert "-NonInteractive" in ps and "-NoProfile" in ps and ps[-1].endswith("Get-Date")
    assert "UTF8" in ps[-1]
    cmd = argv_for("cmd", "dir")
    assert cmd[1:] == ["/d", "/s", "/c", "dir"]
    with pytest.raises(ValueError):
        argv_for("fish", "ls")


@posix_only
def test_run_captures_output_and_exit_code(tmp_path):
    out = Runner().run("t1", "echo hello; echo oops >&2; exit 3", "sh", str(tmp_path), 10)
    assert (out.exit_code, out.stdout.strip(), out.stderr.strip()) == (3, "hello", "oops")
    assert out.duration_ms >= 0


@posix_only
def test_timeout_kills_process_tree(tmp_path):
    marker = tmp_path / "child-survived"
    started = time.time()
    out = Runner().run("t2", f"(sleep 3; touch {marker}) & sleep 30", "sh", "", 1)
    assert out.exit_code == TIMEOUT_EXIT and "timeout" in out.stderr
    assert time.time() - started < 10
    time.sleep(3.5)
    assert not marker.exists()  # the background child was killed with its group


@posix_only
def test_cancel_running_task():
    runner = Runner()
    result = {}
    thread = threading.Thread(target=lambda: result.update(o=runner.run("t3", "sleep 30", "sh", "", 60)))
    thread.start()
    for _ in range(50):
        if runner.cancel("t3"):
            break
        time.sleep(0.05)
    thread.join(10)
    assert result["o"].exit_code != 0


@posix_only
def test_output_is_utf8_and_clipped():
    out = Runner().run("t4", "printf 'h\\303\\251llo \\342\\202\\254'; head -c 70000 /dev/zero | tr '\\0' x", "sh", "", 10)
    assert out.stdout.startswith("h\u00e9llo \u20ac") and out.stdout.endswith("[truncated on device]")


def test_missing_shell_or_cwd_reports_error(tmp_path):
    out = Runner().run("t5", "echo hi", "sh" if os.name != "nt" else "cmd", str(tmp_path / "nope"), 5)
    assert out.exit_code == 127 and "could not start" in out.stderr


def test_artifacts(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"mercury")
    arts = file_artifacts(["a.txt", "missing.txt"], str(tmp_path))
    assert arts[0] == {"path": "a.txt", "size": 7, "sha256": hashlib.sha256(b"mercury").hexdigest()}
    assert arts[1] == {"path": "missing.txt", "missing": True}
