"""mercury-node: pair this device with a Mercury hub, take tasks, and talk to Mercury."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import (IS_WINDOWS, config_path, delete_config, load_config, log_path, node_dir, platform_name,
                     policy_path, save_config)
from .policy import LocalPolicy


def _post(url: str, body: dict, token: str = "", timeout: float = 30) -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": f"mercury-node/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    return _send(req, timeout)


def _get(url: str, token: str, timeout: float = 30) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "User-Agent": f"mercury-node/{__version__}"})
    return _send(req, timeout)


def _send(req: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("detail")
        except ValueError:
            detail = exc.reason
        sys.exit(f"Hub said {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"Could not reach the hub: {exc.reason}")


def _require_config() -> dict:
    cfg = load_config()
    if not cfg or not cfg.get("device_token"):
        sys.exit("This device is not paired. Run: mercury-node pair --hub <url> --code <code>")
    return cfg


def _warn_plain_http(url: str) -> None:
    if url.startswith("http://"):
        print("Warning: this hub URL is not encrypted (http://). Use https:// outside a trusted local network.",
              file=sys.stderr)


def cmd_pair(args) -> None:
    hub = args.hub.rstrip("/")
    if not hub.startswith(("https://", "http://")):
        sys.exit("--hub must start with https:// (or http:// on a trusted local network)")
    _warn_plain_http(hub)
    if load_config() and not args.force:
        sys.exit(f"Already paired ({config_path()}). Use --force to pair again.")
    from .client import capabilities

    reply = _post(hub + "/v1/pair", {"code": args.code, "platform": platform_name(), "hostname": platform.node(),
                                     "node_version": __version__, "capabilities": capabilities()})
    path = save_config(hub, reply["device_name"], reply["device_id"], reply["device_token"])
    LocalPolicy.write_defaults()
    print(f"Paired as {reply['device_name']!r} with {hub}.")
    print(f"Settings: {path}\nLocal policy: {policy_path()}")
    print("Start taking tasks with: mercury-node start")


def cmd_start(args) -> None:
    from .client import Node, TokenRejected

    cfg = _require_config()
    _warn_plain_http(cfg["hub_url"])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    print(f"mercury-node {__version__}: {cfg['device_name']} -> {cfg['hub_url']}  (Ctrl+C to stop)")
    node = Node(cfg["hub_url"], cfg["device_token"])
    try:
        asyncio.run(node.run_forever())
    except TokenRejected as exc:
        sys.exit(f"Stopped: {exc}. Pair again with a new code.")
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_status(args) -> None:
    cfg = load_config()
    if not cfg:
        print("Not paired.")
        return
    print(f"Device:  {cfg['device_name']} ({platform_name()}, {platform.node()})")
    print(f"Hub:     {cfg['hub_url']}")
    policy = LocalPolicy.load()
    print(f"Policy:  mode={policy.mode}, confirm_on_device={policy.confirm_on_device}, "
          f"blocked={len(policy.blocked_patterns)}, allowed_cwd={policy.allowed_cwd or 'any'}")
    try:
        with urllib.request.urlopen(cfg["hub_url"] + "/health", timeout=8) as resp:
            health = json.loads(resp.read().decode())
        print(f"Hub:     reachable (version {health.get('version')})")
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"Hub:     NOT reachable ({exc})")


def cmd_unpair(args) -> None:
    print("Removed this device's pairing." if delete_config() else "This device was not paired.")
    print("Also revoke it on the hub: mercury revoke <name>")


def cmd_policy(args) -> None:
    path = LocalPolicy.write_defaults()
    print(f"{path}\n")
    print(path.read_text(encoding="utf-8"))
    print("Edit this file to change what the hub may run here. Changes apply to the next task.")


def cmd_log(args) -> None:
    path = log_path()
    if not path.exists():
        print("No tasks yet.")
        return
    for line in path.read_text(encoding="utf-8").splitlines()[-args.lines:]:
        entry = json.loads(line)
        status = f"refused: {entry['refused']}" if "refused" in entry else f"exit {entry.get('exit_code')}"
        print(f"{entry['time']}  {entry.get('task_id', ''):<12} {status:<28} {entry.get('command', '')[:80]}")


def _approval_prompt(control: dict) -> str:
    text = control.get("command") or control.get("description") or control.get("title") or json.dumps(control)
    try:
        answer = input(f"\nMercury needs your approval:\n  {text}\nApprove? [y = once / s = session / N] ")
    except EOFError:
        return "deny"
    return {"y": "once", "yes": "once", "s": "session"}.get(answer.strip().lower(), "deny")


def cmd_ask(args) -> None:
    cfg = _require_config()
    hub, token = cfg["hub_url"], cfg["device_token"]
    message = " ".join(args.message).strip() or sys.stdin.read().strip()
    if not message:
        sys.exit("Nothing to ask.")
    run = _post(hub + "/v1/ask", {"message": message, "new_conversation": args.new}, token)
    run_id = run["run_id"]
    answered: set[str] = set()
    started, last_status = time.time(), ""
    while True:
        state = _get(f"{hub}/v1/ask/{run_id}", token)
        status = state.get("status", "")
        if status != last_status and status not in ("completed",):
            print(f"... {status}", file=sys.stderr)
            last_status = status
        for control in state.get("pending_controls") or []:
            ident = str(control.get("prompt_id") or control.get("request_id") or "")
            if ident and ident not in answered:
                choice = _approval_prompt(control)
                _post(f"{hub}/v1/ask/{run_id}/approval", {"request_id": ident, "choice": choice,
                                                         "execution_generation": control.get("execution_generation")},
                      token)
                answered.add(ident)
        if status in ("completed", "failed", "cancelled", "interrupted"):
            break
        if time.time() - started > args.timeout:
            sys.exit(f"Still running after {args.timeout}s (request {run_id}). Ask Mercury about it later.")
        time.sleep(1.5)
    if status != "completed":
        sys.exit(f"Mercury could not finish: {state.get('error') or status}")
    print(state.get("output") or "(no reply)")


def cmd_startup(args) -> None:
    """Opt-in autostart. Windows: a Startup-folder launcher (no admin). Termux: Termux:Boot script."""
    launcher = f'"{sys.executable}" -m mercury_node start'
    if IS_WINDOWS:
        folder = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        target = folder / "mercury-node.cmd"
        content = f"@echo off\r\nstart \"mercury-node\" /min {launcher}\r\n"
    elif platform_name() == "android":
        target = Path.home() / ".termux" / "boot" / "mercury-node"
        content = f"#!/data/data/com.termux/files/usr/bin/sh\ntermux-wake-lock\n{launcher} >> {node_dir()}/node.out 2>&1\n"
    else:
        sys.exit("On Linux/macOS use your service manager (systemd --user, launchd) to run: mercury-node start")
    if args.remove:
        if target.exists():
            target.unlink()
            print(f"Removed {target}.")
        else:
            print("Autostart was not set up.")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    if not IS_WINDOWS:
        target.chmod(0o700)
    print(f"Mercury node will start when you sign in: {target}\nUndo with: mercury-node startup --remove")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mercury-node", description=f"Mercury node {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("pair", help="pair this device with a hub using a one-time code")
    s.add_argument("--hub", required=True, help="hub URL, e.g. https://mercury.example.com")
    s.add_argument("--code", required=True, help="code from `mercury pair <name>` on the hub")
    s.add_argument("--force", action="store_true", help="replace an existing pairing")
    s.set_defaults(func=cmd_pair)

    s = sub.add_parser("start", help="connect to the hub and take tasks (Ctrl+C to stop)")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("ask", help="send a message to Mercury from this device")
    s.add_argument("message", nargs="*", help="what to ask (or pipe it in)")
    s.add_argument("--new", action="store_true", help="start a new conversation")
    s.add_argument("--timeout", type=int, default=600)
    s.set_defaults(func=cmd_ask)

    sub.add_parser("status", help="show pairing, policy and hub reachability").set_defaults(func=cmd_status)
    sub.add_parser("policy", help="show the local policy file").set_defaults(func=cmd_policy)

    s = sub.add_parser("log", help="tasks the hub ran on this device")
    s.add_argument("-n", "--lines", type=int, default=20)
    s.set_defaults(func=cmd_log)

    sub.add_parser("unpair", help="forget this device's pairing").set_defaults(func=cmd_unpair)

    s = sub.add_parser("startup", help="start automatically at sign-in (opt-in)")
    s.add_argument("--remove", action="store_true")
    s.set_defaults(func=cmd_startup)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
