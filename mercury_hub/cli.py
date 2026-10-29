"""Hub admin commands: ``mercury <command>`` (or ``python -m mercury_hub <command>``)."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Settings
from .db import APPROVAL_MODES, Store


def _api(settings: Settings, method: str, path: str, **kwargs):
    import httpx

    if not settings.admin_token:
        sys.exit("MERCURY_ADMIN_TOKEN is not set (run `mercury admin-token --write`).")
    try:
        response = httpx.request(method, settings.relay_url + path, timeout=kwargs.pop("timeout", 15),
                                 headers={"Authorization": f"Bearer {settings.admin_token}"}, **kwargs)
    except httpx.HTTPError as exc:
        sys.exit(f"Relay not reachable at {settings.relay_url} ({exc}). Is `mercury relay` running?")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        sys.exit(f"Relay error {response.status_code}: {detail}")
    return response.json()


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"


def cmd_relay(args, settings: Settings) -> None:
    import logging

    import uvicorn

    from .relay import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not settings.admin_token:
        logging.warning("MERCURY_ADMIN_TOKEN is not set; the agent and CLI cannot submit tasks.")
    app = create_app(Store(settings.db_path), settings.admin_token, settings.api_url, settings.api_key,
                     home=settings.home, seed_skills=Path(__file__).resolve().parents[1] / "skills")
    uvicorn.run(app, host=args.host or settings.relay_host, port=args.port or settings.relay_port,
                ws_max_size=2 * 1024 * 1024, ws_ping_interval=20, ws_ping_timeout=20, log_level="info",
                proxy_headers=True, forwarded_allow_ips=args.forwarded_allow_ips)


def cmd_admin_token(args, settings: Settings) -> None:
    token = secrets.token_urlsafe(32)
    if not args.write:
        print(token)
        return
    env = settings.home / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    if any(line.startswith("MERCURY_ADMIN_TOKEN=") for line in lines) and not args.force:
        sys.exit(f"{env} already has MERCURY_ADMIN_TOKEN (use --force to replace it).")
    lines = [line for line in lines if not line.startswith("MERCURY_ADMIN_TOKEN=")] + [f"MERCURY_ADMIN_TOKEN={token}"]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env.chmod(0o600)
    print(f"Wrote MERCURY_ADMIN_TOKEN to {env}. Restart the relay and gateway to pick it up.")


def cmd_pair(args, settings: Settings) -> None:
    try:
        code, expires = Store(settings.db_path).create_pairing_code(args.name, args.approval, args.ttl * 60)
    except ValueError as exc:
        sys.exit(str(exc))
    url = settings.public_url or "<your hub URL>"
    print(f"Pairing code for {args.name!r}: {code[:4]}-{code[4:]}  (valid until {_when(expires)}, single use)")
    print(f"On the device:  mercury-node pair --hub {url} --code {code[:4]}-{code[4:]}")


def cmd_devices(args, settings: Settings) -> None:
    try:
        devices = _api(settings, "GET", "/v1/admin/devices", params={"include_revoked": args.all})["devices"]
    except SystemExit:
        if args.json:
            raise
        print("(relay not reachable: showing stored devices without online status)", file=sys.stderr)
        devices = [d | {"online": None} for d in Store(settings.db_path).devices(args.all)]
    if args.json:
        print(json.dumps(devices, indent=2))
        return
    if not devices:
        print("No paired devices. Create a code with `mercury pair <name>`.")
    for d in devices:
        state = {True: "online", False: "offline", None: "?"}[d["online"]]
        if d.get("revoked_at"):
            state = "revoked"
        print(f"{d['name']:<20} {state:<8} {d['platform'] or '-':<10} {d['hostname'] or '-':<20} "
              f"approval={d['approval']:<9} last seen {_when(d['last_seen'])}")


def cmd_revoke(args, settings: Settings) -> None:
    _api(settings, "POST", f"/v1/admin/devices/{args.name}/revoke")
    print(f"Revoked {args.name}. Its token no longer works; pair again to reconnect it.")


def cmd_policy(args, settings: Settings) -> None:
    body = {}
    if args.approval:
        body["approval"] = args.approval
    if args.block is not None:
        body["blocked_patterns"] = args.block
    print(json.dumps(_api(settings, "POST", f"/v1/admin/devices/{args.name}/policy", json=body), indent=2))


def cmd_tasks(args, settings: Settings) -> None:
    tasks = _api(settings, "GET", "/v1/admin/tasks", params={"device": args.device, "limit": args.limit})["tasks"]
    if args.json:
        print(json.dumps(tasks, indent=2))
        return
    for t in tasks:
        print(f"{t['id']}  {_when(t['created_at'])}  {t['device']:<16} {t['status']:<13} {t['command'][:70]}")


def cmd_run(args, settings: Settings) -> None:
    body = {"device": args.device, "command": args.command, "shell": args.shell, "cwd": args.cwd,
            "timeout_s": args.timeout,
            "verify_command": args.verify or "", "collect_paths": args.collect or [], "requested_by": "cli"}
    started = time.time()
    task = _api(settings, "POST", "/v1/admin/tasks", json=body, timeout=args.timeout + 30)
    if args.json:
        print(json.dumps(task, indent=2))
        return
    print(f"task {task['id']} on {task['device']}: {task['status']} (exit {task['exit_code']}, "
          f"{time.time() - started:.1f}s)")
    if task["error"]:
        print(f"error: {task['error']}")
    if task["stdout"]:
        print(task["stdout"].rstrip())
    if task["stderr"]:
        print("--- stderr ---\n" + task["stderr"].rstrip())
    if task["verify_command"]:
        print(f"--- verify (exit {task['verify_exit_code']}) ---\n{task['verify_output'].rstrip()}")
    sys.exit(0 if task["status"] in ("verified", "completed") else 1)


def cmd_status(args, settings: Settings) -> None:
    import httpx

    try:
        health = httpx.get(settings.relay_url + "/health", timeout=5).json()
        print(f"relay: up at {settings.relay_url} (version {health.get('version')}, protocol {health.get('protocol')})")
    except httpx.HTTPError as exc:
        print(f"relay: DOWN at {settings.relay_url} ({exc})")
    print(f"home:  {settings.home}")
    print(f"db:    {settings.db_path}")
    print(f"admin token: {'set' if settings.admin_token else 'MISSING'}")
    print(f"public URL:  {settings.public_url or 'not set (MERCURY_PUBLIC_URL)'}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mercury", description=f"Mercury hub admin (v{__version__})")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("relay", help="run the device relay")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--forwarded-allow-ips", default="127.0.0.1",
                   help="reverse proxies whose X-Forwarded-For is trusted")
    s.set_defaults(func=cmd_relay)

    s = sub.add_parser("admin-token", help="generate the hub admin token")
    s.add_argument("--write", action="store_true", help="save it to MERCURY_HOME/.env")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_admin_token)

    s = sub.add_parser("pair", help="create a one-time pairing code for a new device")
    s.add_argument("name")
    s.add_argument("--approval", choices=APPROVAL_MODES, default="dangerous",
                   help="which commands need a human yes: all, dangerous (default) or none")
    s.add_argument("--ttl", type=int, default=10, help="minutes the code stays valid (default 10)")
    s.set_defaults(func=cmd_pair)

    s = sub.add_parser("devices", help="list paired devices")
    s.add_argument("--all", action="store_true", help="include revoked devices")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("revoke", help="revoke a device and drop its connection")
    s.add_argument("name")
    s.set_defaults(func=cmd_revoke)

    s = sub.add_parser("policy", help="change a device's approval mode or blocked patterns")
    s.add_argument("name")
    s.add_argument("--approval", choices=APPROVAL_MODES)
    s.add_argument("--block", nargs="*", metavar="GLOB", help="replace the blocked command globs")
    s.set_defaults(func=cmd_policy)

    s = sub.add_parser("tasks", help="show the task log")
    s.add_argument("--device")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_tasks)

    s = sub.add_parser("run", help="run a command on a device (bypasses the agent; hard floor still applies)")
    s.add_argument("device")
    s.add_argument("command")
    s.add_argument("--shell", default="", help="powershell, cmd, bash or sh (default: device's own)")
    s.add_argument("--cwd", default="", help="working directory on the device")
    s.add_argument("--verify", help="command whose exit code 0 proves the result")
    s.add_argument("--collect", nargs="*", help="files to hash and report back")
    s.add_argument("--timeout", type=int, default=120)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="check the relay and settings")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args, Settings.load())


if __name__ == "__main__":
    main()
