"""Decide whether a remote command runs, needs a human "yes", or is refused.

Three outcomes, checked in this order:

* ``block``   hard floor (wiping disks, boot config, fork bombs) and the device's own
              blocked patterns. Nothing overrides these, not even "approval: none".
* ``approve`` destructive or system-changing commands, or every command on devices
              set to ``approval: all``. The agent plugin turns this into the engine's
              human-approval gate, which fails closed when nobody answers.
* ``allow``   everything else.

Patterns cover PowerShell/cmd (Windows nodes) and POSIX shells (Linux, macOS,
Android/Termux). This is a guard rail, not a sandbox: the node enforces its own
local policy as well.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    action: str  # "allow" | "approve" | "block"
    reason: str = ""
    rule: str = ""


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# (rule id, regex, human reason)
HARD_BLOCK: list[tuple[str, re.Pattern, str]] = [
    ("rm-root", _rx(r"\brm\s+(-[a-z]*\s+)*-[a-z]*r[a-z]*\s+(-[a-z]+\s+)*(/|/\*|~|\$HOME)(\s|$)"),
     "recursive delete of a root or home directory"),
    ("mkfs", _rx(r"\bmkfs(\.\w+)?\b"), "formatting a filesystem"),
    ("dd-device", _rx(r"\bdd\b.*\bof=/dev/(sd|nvme|hd|mmcblk|disk)"), "writing raw data to a disk"),
    ("fork-bomb", _rx(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    ("format-volume", _rx(r"\b(Format-Volume|Clear-Disk|Initialize-Disk)\b"), "formatting or wiping a disk"),
    ("format-drive", _rx(r"(^|[;&|]\s*)format(\.com)?\s+[a-z]:"), "formatting a drive"),
    ("diskpart", _rx(r"\bdiskpart\b"), "disk partitioning"),
    ("bcdedit", _rx(r"\bbcdedit\b"), "changing boot configuration"),
    ("cipher-wipe", _rx(r"\bcipher\s+/w"), "wiping free disk space"),
    ("wipe-system-drive", _rx(r"\b(Remove-Item|rm|del|rd|rmdir)\b.*\s['\"]?[a-z]:\\(windows|users)?\\?['\"]?(\s|$)"),
     "deleting a system drive or its Windows/Users folder"),
    ("vssadmin-delete", _rx(r"\bvssadmin\s+delete\b|\bwmic\s+shadowcopy\s+delete\b"), "deleting backups/shadow copies"),
    ("disable-defender", _rx(r"Set-MpPreference\s+.*-Disable\w+\s+\$?true"), "disabling antivirus protection"),
]

NEEDS_APPROVAL: list[tuple[str, re.Pattern, str]] = [
    ("delete", _rx(r"(^|[;&|(]\s*)(rm|rmdir|unlink|shred|del|erase|rd|Remove-Item|ri)\b"), "deletes files"),
    ("move-overwrite", _rx(r"(^|[;&|(]\s*)(mv|Move-Item|move)\b.*(-Force|-f\b)"), "moves files with overwrite"),
    ("power", _rx(r"\b(shutdown|reboot|poweroff|halt|Stop-Computer|Restart-Computer)\b"), "shuts down or restarts"),
    ("kill", _rx(r"\b(kill|pkill|killall|taskkill|Stop-Process|spps)\b"), "stops processes"),
    ("services", _rx(r"\b(systemctl\s+(stop|disable|mask|restart)|sc(\.exe)?\s+(stop|delete|config)|"
                     r"Stop-Service|Set-Service|Remove-Service)\b"), "changes system services"),
    ("registry", _rx(r"\b(reg(\.exe)?\s+(add|delete|import)|Set-ItemProperty\s+.*HK(LM|CU)|"
                     r"Remove-ItemProperty|New-ItemProperty\s+.*HK(LM|CU))"), "changes the registry"),
    ("privilege", _rx(r"\b(sudo|su\s|runas|doas)\b|-Verb\s+RunAs"), "runs with elevated privileges"),
    ("exec-policy", _rx(r"\bSet-ExecutionPolicy\b"), "changes script execution policy"),
    ("users", _rx(r"\b(net\s+user|net\s+localgroup|useradd|userdel|usermod|passwd|New-LocalUser|"
                  r"Remove-LocalUser|Add-LocalGroupMember)\b"), "changes user accounts"),
    ("network-config", _rx(r"\b(netsh|iptables|ufw|New-NetFirewallRule|Set-NetFirewallProfile|route\s+(add|delete))\b"),
     "changes network or firewall settings"),
    ("install", _rx(r"\b(winget|choco|scoop)\s+(install|uninstall|upgrade)|\b(apt|apt-get|dnf|yum|pacman|pkg|brew)\s+"
                    r"(install|remove|purge|upgrade)|\bpip3?\s+install|\bnpm\s+(i|install)\s+-g|Install-Module"),
     "installs or removes software"),
    ("remote-script", _rx(r"(curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b.*\|\s*(sh|bash|zsh|iex|"
                          r"Invoke-Expression)|\b(iex|Invoke-Expression)\s*\(.*(irm|iwr|Invoke-WebRequest|DownloadString)"),
     "runs a script downloaded from the internet"),
    ("permissions", _rx(r"\b(chmod|chown)\s+(-[a-z]*R|--recursive)|\b(icacls|takeown)\b"), "changes file permissions"),
    ("git-destructive", _rx(r"\bgit\s+(push\s+.*(--force|-f\b)|reset\s+--hard|clean\s+-[a-z]*f)"),
     "rewrites or discards git history/changes"),
    ("overwrite-redirect", _rx(r"(^|\s)>\s*(/etc/|[a-z]:\\windows)"), "overwrites a system file"),
    ("scheduled", _rx(r"\b(crontab\s+-r|schtasks\s+/(create|delete)|Register-ScheduledTask|Unregister-ScheduledTask)\b"),
     "changes scheduled tasks"),
    ("clear-content", _rx(r"\b(Clear-Content|Clear-RecycleBin|truncate\s+-s\s*0)\b"), "erases file contents"),
]


def _engine_check(command: str) -> tuple[bool, str]:
    """Reuse the engine's POSIX dangerous-command detector when it is importable."""
    try:
        from tools.approval_detection import detect_dangerous_command  # type: ignore
    except Exception:
        try:
            from tools.approval import detect_dangerous_command  # type: ignore
        except Exception:
            return False, ""
    try:
        dangerous, _key, description = detect_dangerous_command(command)
        return bool(dangerous), str(description or "")
    except Exception:
        return False, ""


def classify(command: str, approval: str = "dangerous", blocked_patterns: list[str] | None = None) -> Decision:
    text = (command or "").strip()
    if not text:
        return Decision("block", "empty command", "empty")
    if "\x00" in text:
        return Decision("block", "command contains a NUL byte", "nul")
    for rule, rx, reason in HARD_BLOCK:
        if rx.search(text):
            return Decision("block", f"refused: {reason}", f"floor:{rule}")
    for pattern in blocked_patterns or []:
        if fnmatch.fnmatch(text.lower(), pattern.lower()):
            return Decision("block", f"refused by this device's blocked pattern {pattern!r}", f"device:{pattern}")
    if approval == "all":
        return Decision("approve", "this device requires approval for every command", "device:all")
    if approval == "none":
        return Decision("allow")
    for rule, rx, reason in NEEDS_APPROVAL:
        if rx.search(text):
            return Decision("approve", reason, rule)
    dangerous, description = _engine_check(text)
    if dangerous:
        return Decision("approve", description or "flagged as dangerous", "engine")
    return Decision("allow")
