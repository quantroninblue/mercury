"""SQLite store for devices, pairing codes and the task log.

Secrets (pairing codes, device tokens) are stored only as SHA-256 hashes. The
relay, the CLI and the agent plugin may open the same file, so it runs in WAL
mode and every write is a short transaction.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

APPROVAL_MODES = ("all", "dangerous", "none")
# Unambiguous alphabet for codes a person types: no 0/O, 1/I/L.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MAX_OUTPUT_CHARS = 64_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL DEFAULT '',
    node_version TEXT NOT NULL DEFAULT '',
    capabilities TEXT NOT NULL DEFAULT '{}',
    token_hash TEXT NOT NULL UNIQUE,
    approval TEXT NOT NULL DEFAULT 'dangerous',
    blocked_patterns TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    last_seen REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    approval TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    command TEXT NOT NULL,
    shell TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    verify_command TEXT NOT NULL DEFAULT '',
    collect_paths TEXT NOT NULL DEFAULT '[]',
    timeout_s INTEGER NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    verify_exit_code INTEGER,
    verify_output TEXT NOT NULL DEFAULT '',
    artifacts TEXT NOT NULL DEFAULT '[]',
    duration_ms INTEGER,
    error TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_by_device ON tasks(device_id, created_at);
CREATE TABLE IF NOT EXISTS skill_outcomes (
    skill TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (skill, task_id)
);
"""

SUCCESS_STATUSES = {"verified", "completed"}

# Terminal states: the task will not change again.
FINAL_STATUSES = {"verified", "completed", "verify_failed", "failed", "rejected", "timeout", "offline", "blocked"}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clip(text: Any) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + "\n... [truncated by hub]"


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self._run(sql, params).fetchone()
        return dict(row) if row else None

    # -- pairing -------------------------------------------------------------

    def create_pairing_code(self, name: str, approval: str = "dangerous", ttl_s: int = 600) -> tuple[str, float]:
        name = name.strip()
        if not name or len(name) > 64:
            raise ValueError("Device name must be 1-64 characters.")
        if approval not in APPROVAL_MODES:
            raise ValueError(f"Approval must be one of {', '.join(APPROVAL_MODES)}.")
        if self.device_by_name(name, include_revoked=False):
            raise ValueError(f"A device named {name!r} is already paired. Revoke it first.")
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        expires = time.time() + ttl_s
        self._run("INSERT INTO pairing_codes VALUES (?, ?, ?, ?, NULL)", (sha256(code), name, approval, expires))
        return code, expires

    def redeem_pairing_code(self, code: str, platform: str, hostname: str, node_version: str,
                            capabilities: dict | None = None) -> tuple[dict, str]:
        """Exchange a one-time code for a device record and its bearer token (shown once)."""
        code = code.strip().upper().replace("-", "").replace(" ", "")
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pairing_codes WHERE code_hash = ?", (sha256(code),)).fetchone()
            if not row or row["used_at"] is not None or row["expires_at"] < now:
                raise PermissionError("Pairing code is invalid, used or expired.")
            token = secrets.token_urlsafe(32)
            device_id = uuid.uuid4().hex
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("UPDATE pairing_codes SET used_at = ? WHERE code_hash = ?", (now, row["code_hash"]))
                # A revoked device's name can be reused by a fresh pairing.
                self._conn.execute("DELETE FROM devices WHERE name = ? AND revoked_at IS NOT NULL "
                                   "AND id NOT IN (SELECT device_id FROM tasks)", (row["name"],))
                self._conn.execute("UPDATE devices SET name = name || '#' || substr(id, 1, 6) "
                                   "WHERE name = ? AND revoked_at IS NOT NULL", (row["name"],))
                self._conn.execute(
                    "INSERT INTO devices (id, name, platform, hostname, node_version, capabilities, token_hash,"
                    " approval, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (device_id, row["name"], platform[:64], hostname[:128], node_version[:32],
                     json.dumps(capabilities or {})[:4000], sha256(token), row["approval"], now))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.device(device_id), token

    # -- devices -------------------------------------------------------------

    def device(self, device_id: str) -> dict | None:
        return self._decode_device(self._one("SELECT * FROM devices WHERE id = ?", (device_id,)))

    def device_by_name(self, name: str, include_revoked: bool = False) -> dict | None:
        sql = "SELECT * FROM devices WHERE name = ?" + ("" if include_revoked else " AND revoked_at IS NULL")
        return self._decode_device(self._one(sql, (name,)))

    def device_by_token(self, token: str) -> dict | None:
        return self._decode_device(
            self._one("SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL", (sha256(token),)))

    def devices(self, include_revoked: bool = False) -> list[dict]:
        sql = "SELECT * FROM devices" + ("" if include_revoked else " WHERE revoked_at IS NULL") + " ORDER BY name"
        return [self._decode_device(dict(r)) for r in self._run(sql).fetchall()]

    def touch_device(self, device_id: str, **fields: Any) -> None:
        allowed = {"platform", "hostname", "node_version", "capabilities"}
        updates = {k: (json.dumps(v)[:4000] if k == "capabilities" else str(v)[:128])
                   for k, v in fields.items() if k in allowed and v is not None}
        cols = ", ".join([f"{k} = ?" for k in updates] + ["last_seen = ?"])
        self._run(f"UPDATE devices SET {cols} WHERE id = ?", (*updates.values(), time.time(), device_id))

    def set_approval(self, name: str, approval: str) -> dict:
        if approval not in APPROVAL_MODES:
            raise ValueError(f"Approval must be one of {', '.join(APPROVAL_MODES)}.")
        if self._run("UPDATE devices SET approval = ? WHERE name = ? AND revoked_at IS NULL",
                     (approval, name)).rowcount == 0:
            raise KeyError(name)
        return self.device_by_name(name)

    def set_blocked_patterns(self, name: str, patterns: list[str]) -> dict:
        if self._run("UPDATE devices SET blocked_patterns = ? WHERE name = ? AND revoked_at IS NULL",
                     (json.dumps([p for p in patterns if p.strip()]), name)).rowcount == 0:
            raise KeyError(name)
        return self.device_by_name(name)

    def revoke(self, name: str) -> dict:
        device = self.device_by_name(name)
        if not device:
            raise KeyError(name)
        self._run("UPDATE devices SET revoked_at = ? WHERE id = ?", (time.time(), device["id"]))
        return self.device(device["id"])

    @staticmethod
    def _decode_device(row: dict | None) -> dict | None:
        if row is None:
            return None
        row = dict(row)
        row["capabilities"] = json.loads(row.get("capabilities") or "{}")
        row["blocked_patterns"] = json.loads(row.get("blocked_patterns") or "[]")
        row.pop("token_hash", None)
        return row

    # -- tasks ---------------------------------------------------------------

    def create_task(self, device_id: str, command: str, *, shell: str = "", cwd: str = "",
                    verify_command: str = "", collect_paths: list[str] | None = None, timeout_s: int = 120,
                    requested_by: str = "", session_id: str = "", status: str = "queued", error: str = "") -> dict:
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._run(
            "INSERT INTO tasks (id, device_id, command, shell, cwd, verify_command, collect_paths, timeout_s,"
            " status, error, requested_by, session_id, created_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, device_id, command, shell, cwd, verify_command, json.dumps(collect_paths or []), timeout_s,
             status, error, requested_by[:64], session_id[:128], now, now if status in FINAL_STATUSES else None))
        return self.task(task_id)

    def set_task_status(self, task_id: str, status: str, error: str = "") -> None:
        final = time.time() if status in FINAL_STATUSES else None
        self._run("UPDATE tasks SET status = ?, error = CASE WHEN ? != '' THEN ? ELSE error END,"
                  " finished_at = COALESCE(?, finished_at) WHERE id = ? AND finished_at IS NULL",
                  (status, error, error[:2000], final, task_id))

    def record_result(self, task_id: str, result: dict) -> dict | None:
        """Store a node's result and derive the final status. Ignored once a task is final."""
        exit_code = result.get("exit_code")
        verify_exit = result.get("verify_exit_code")
        task = self.task(task_id)
        if not task or task["finished_at"] is not None:
            return task
        if not isinstance(exit_code, int):
            status = "failed"
        elif exit_code != 0:
            status = "failed"
        elif task["verify_command"]:
            status = "verified" if verify_exit == 0 else "verify_failed"
        else:
            status = "completed"
        artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), list) else []
        self._run(
            "UPDATE tasks SET status = ?, exit_code = ?, stdout = ?, stderr = ?, verify_exit_code = ?,"
            " verify_output = ?, artifacts = ?, duration_ms = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, exit_code if isinstance(exit_code, int) else None, _clip(result.get("stdout")),
             _clip(result.get("stderr")), verify_exit if isinstance(verify_exit, int) else None,
             _clip(result.get("verify_output")), json.dumps(artifacts[:50]),
             int(result["duration_ms"]) if isinstance(result.get("duration_ms"), (int, float)) else None,
             str(result.get("error") or "")[:2000], time.time(), task_id))
        return self.task(task_id)

    def task(self, task_id: str) -> dict | None:
        row = self._one("SELECT t.*, d.name AS device FROM tasks t JOIN devices d ON d.id = t.device_id"
                        " WHERE t.id = ?", (task_id,))
        if row:
            row["collect_paths"] = json.loads(row["collect_paths"])
            row["artifacts"] = json.loads(row["artifacts"])
        return row

    def tasks(self, device: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT t.*, d.name AS device FROM tasks t JOIN devices d ON d.id = t.device_id"
        params: tuple = ()
        if device:
            sql += " WHERE d.name = ?"
            params = (device,)
        sql += " ORDER BY t.created_at DESC LIMIT ?"
        rows = [dict(r) for r in self._run(sql, (*params, max(1, min(limit, 200)))).fetchall()]
        for row in rows:
            row["collect_paths"] = json.loads(row["collect_paths"])
            row["artifacts"] = json.loads(row["artifacts"])
        return rows

    # -- skill outcomes --------------------------------------------------------

    def record_skill_outcome(self, skill: str, task_id: str, status: str, session_id: str = "") -> None:
        self._run("INSERT OR REPLACE INTO skill_outcomes VALUES (?, ?, ?, ?, ?)",
                  (skill[:128], task_id, status, session_id[:128], time.time()))

    def skill_stats(self, skill: str | None = None) -> list[dict]:
        """Per-skill verified-outcome counts, worst success rate first."""
        sql = ("SELECT skill, COUNT(*) AS uses,"
               " SUM(CASE WHEN status IN ('verified', 'completed') THEN 1 ELSE 0 END) AS successes,"
               " MAX(created_at) AS last_used FROM skill_outcomes")
        params: tuple = ()
        if skill:
            sql += " WHERE skill = ?"
            params = (skill,)
        rows = [dict(r) for r in self._run(sql + " GROUP BY skill", params).fetchall()]
        for row in rows:
            row["success_rate"] = round(row["successes"] / row["uses"], 2) if row["uses"] else None
            row["recent_failures"] = [dict(r) for r in self._run(
                "SELECT o.task_id, o.status, t.error, t.command FROM skill_outcomes o"
                " LEFT JOIN tasks t ON t.id = o.task_id WHERE o.skill = ?"
                " AND o.status NOT IN ('verified', 'completed') ORDER BY o.created_at DESC LIMIT 3",
                (row["skill"],)).fetchall()]
        return sorted(rows, key=lambda r: (r["success_rate"] if r["success_rate"] is not None else 1, -r["uses"]))

    def fail_unfinished(self, device_id: str | None = None, reason: str = "hub restarted") -> int:
        """Close tasks left open (relay restart or device disconnect)."""
        sql = "UPDATE tasks SET status = 'failed', error = ?, finished_at = ? WHERE finished_at IS NULL"
        params: tuple = (reason, time.time())
        if device_id:
            sql += " AND device_id = ?"
            params += (device_id,)
        return self._run(sql, params).rowcount
