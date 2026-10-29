"""The device's own rules. The hub cannot override them.

policy.json (created with these defaults on first pairing):

    {
      "mode": "normal",            # normal | read-only | paused
      "blocked_patterns": [],      # globs, e.g. "*\\\\Documents\\\\Finance*"
      "allowed_cwd": [],           # if set, tasks may only start in these folders
      "confirm_on_device": false,  # true: ask y/n in this window before every task
      "max_timeout_s": 900
    }

``paused`` refuses everything; ``read-only`` refuses commands that look like they
change files, settings or processes. A small hard floor (disk wipes, formatting, boot
changes) is always refused, matching the hub's.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import policy_path

DEFAULTS = {"mode": "normal", "blocked_patterns": [], "allowed_cwd": [], "confirm_on_device": False,
            "max_timeout_s": 900}

_I = re.IGNORECASE
HARD_FLOOR = [
    re.compile(r"\brm\s+(-[a-z]*\s+)*-[a-z]*r[a-z]*\s+(-[a-z]+\s+)*(/|/\*|~|\$HOME)(\s|$)", _I),
    re.compile(r"\bmkfs(\.\w+)?\b", _I),
    re.compile(r"\bdd\b.*\bof=/dev/(sd|nvme|hd|mmcblk|disk)", _I),
    re.compile(r"\b(Format-Volume|Clear-Disk|Initialize-Disk|diskpart|bcdedit)\b", _I),
    re.compile(r"(^|[;&|]\s*)format(\.com)?\s+[a-z]:", _I),
    re.compile(r"\bcipher\s+/w|\bvssadmin\s+delete\b", _I),
    re.compile(r"\b(Remove-Item|rm|del|rd|rmdir)\b.*\s['\"]?[a-z]:\\(windows|users)?\\?['\"]?(\s|$)", _I),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
]
# Anything that writes, deletes, moves, installs, kills or reconfigures.
WRITES = re.compile(
    r"(^|[;&|(]\s*)(rm|rmdir|del|erase|rd|mv|move|cp|copy|xcopy|robocopy|mkdir|md|touch|chmod|chown|ln|tee|"
    r"Remove-Item|Move-Item|Copy-Item|Rename-Item|New-Item|Set-Content|Add-Content|Out-File|Clear-Content|"
    r"Set-ItemProperty|New-ItemProperty|Remove-ItemProperty|Stop-Process|Start-Process|Stop-Service|"
    r"Set-Service|Restart-Computer|Stop-Computer|taskkill|kill|pkill|shutdown|reboot|reg|sc|netsh|"
    r"winget|choco|pip|pip3|npm|apt|apt-get|pkg|git\s+(push|commit|reset|clean|checkout)|"
    r"Set-ExecutionPolicy|Install-Module|schtasks|crontab)\b|(^|[^<>2])>{1,2}\s*[^&\s]", _I)


@dataclass
class LocalPolicy:
    mode: str = "normal"
    blocked_patterns: list[str] = field(default_factory=list)
    allowed_cwd: list[str] = field(default_factory=list)
    confirm_on_device: bool = False
    max_timeout_s: int = 900

    @classmethod
    def load(cls, path: Path | None = None) -> "LocalPolicy":
        path = path or policy_path()
        try:
            data = {**DEFAULTS, **json.loads(path.read_text(encoding="utf-8"))}
        except FileNotFoundError:
            data = dict(DEFAULTS)
        except ValueError as exc:
            # A broken policy file must not silently fall back to permissive defaults.
            return cls(mode="paused", blocked_patterns=[f"(policy.json unreadable: {exc})"])
        return cls(mode=str(data["mode"]), blocked_patterns=list(data["blocked_patterns"]),
                   allowed_cwd=list(data["allowed_cwd"]), confirm_on_device=bool(data["confirm_on_device"]),
                   max_timeout_s=int(data["max_timeout_s"]))

    @staticmethod
    def write_defaults(path: Path | None = None) -> Path:
        path = path or policy_path()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
        return path

    def check(self, command: str, cwd: str = "", verify_command: str = "") -> str | None:
        """Return a refusal reason, or None if the task may run."""
        if self.mode == "paused":
            return "this device is paused (policy.json mode: paused)"
        for text in filter(None, (command, verify_command)):
            if any(rx.search(text) for rx in HARD_FLOOR):
                return "refused by the device's safety floor"
            for pattern in self.blocked_patterns:
                if fnmatch.fnmatch(text.lower(), pattern.lower()):
                    return f"matches this device's blocked pattern {pattern!r}"
            if self.mode == "read-only" and WRITES.search(text):
                return "this device is read-only (policy.json mode: read-only)"
        if self.allowed_cwd:
            target = os.path.realpath(os.path.expanduser(cwd or os.getcwd()))
            roots = [os.path.realpath(os.path.expanduser(p)) for p in self.allowed_cwd]
            if not any(os.path.commonpath([target, root]) == root for root in roots if _same_drive(target, root)):
                return f"working folder {cwd or '(default)'} is outside this device's allowed folders"
        return None


def _same_drive(a: str, b: str) -> bool:
    return os.path.splitdrive(a)[0].lower() == os.path.splitdrive(b)[0].lower()
