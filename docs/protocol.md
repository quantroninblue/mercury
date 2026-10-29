# Mercury device protocol (v1)

How an end-user node (the `node` branch) talks to the hub relay. Nodes always connect **out** to the hub, so devices need no open ports, SSH server or VPN.

All messages are JSON objects with a `type` field. Text frames only. Maximum frame size is 2 MB.

## 1. Pairing (once per device)

On the hub, an admin creates a one-time code:

```
$ mercury pair laptop
Pairing code for 'laptop': K7QM-2XPA  (valid until 2026-10-29 17:05:00, single use)
On the device:  mercury-node pair --hub https://mercury.example.com --code K7QM-2XPA
```

The node exchanges it for a device token:

```
POST /v1/pair
{"code": "K7QM-2XPA", "platform": "windows", "hostname": "LAPTOP-01",
 "node_version": "0.1.0", "capabilities": {"shells": ["powershell", "cmd"]}}

200 {"device_id": "...", "device_name": "laptop", "device_token": "<secret>",
     "approval": "dangerous", "protocol": 1}
```

- Codes are 8 characters, single use, expire after 10 minutes by default, and are stored hashed. Dashes, spaces and case are ignored.
- The token is returned once. The hub stores only its SHA-256. The node must keep it in the OS credential store (Windows Credential Manager, Android Keystore or Termux file with mode 600).
- 5 failed attempts from one address in 15 minutes give `429` until the window passes.
- `platform` is one of `windows`, `linux`, `darwin`, `android`.

## 2. Connecting

```
WS /v1/node
Authorization: Bearer <device_token>
```

An unknown or revoked token is refused at the handshake (HTTP 403). After the socket opens, the node must send `hello` within 10 seconds:

```json
{"type": "hello", "protocol": 1, "platform": "windows", "hostname": "LAPTOP-01",
 "node_version": "0.1.0", "capabilities": {"shells": ["powershell", "cmd"], "termux_api": false}}
```

The hub answers:

```json
{"type": "welcome", "device_id": "...", "device_name": "laptop", "approval": "dangerous", "protocol": 1}
```

A second connection with the same token replaces the first (the old socket is closed with code `4409`). Revoking a device closes its socket with `4403`. A wrong protocol version or a missing hello closes with `4400`.

The hub sends WebSocket pings every 20 seconds. Nodes may also send `{"type": "heartbeat"}` to refresh `last_seen`.

**Reconnecting:** if the hub is unreachable, the node retries with exponential backoff (1 s doubling to a 5 minute cap, with jitter) and otherwise stays idle. A hub outage must never affect the device.

## 3. Tasks

Hub to node:

```json
{"type": "task", "task_id": "3f9c2a1b7d4e", "command": "Get-CimInstance Win32_Battery | ConvertTo-Json",
 "shell": "powershell", "cwd": "", "timeout_s": 120,
 "verify_command": "if ((Get-CimInstance Win32_Battery)) { exit 0 } else { exit 1 }",
 "collect_paths": []}
```

- `shell` empty means the device default (PowerShell on Windows, `sh` elsewhere).
- The node checks the command against its **own local policy** first, then acknowledges:

```json
{"type": "task_ack", "task_id": "3f9c2a1b7d4e", "accepted": true}
{"type": "task_ack", "task_id": "3f9c2a1b7d4e", "accepted": false, "reason": "outside allowed folders"}
```

- After running it (and `verify_command`, if any, in the same `cwd`), the node reports:

```json
{"type": "task_result", "task_id": "3f9c2a1b7d4e", "exit_code": 0,
 "stdout": "...", "stderr": "", "duration_ms": 412,
 "verify_exit_code": 0, "verify_output": "",
 "artifacts": [{"path": "report.html", "size": 18233, "sha256": "..."}]}
```

- `artifacts` lists each `collect_paths` entry that exists, with its size and SHA-256.
- Output over 64,000 characters per stream is truncated by the hub. Nodes should truncate earlier if needed.
- On timeout the node must kill the process tree and still send a `task_result` with `exit_code` 124. The hub also sends `{"type": "cancel", "task_id": "..."}` if it gave up waiting. The node should kill the task if it is still running.

### Task statuses (hub side)

| Status | Meaning |
|---|---|
| `sent`, `running` | in flight |
| `verified` | exit code 0 **and** verify command exit code 0 |
| `completed` | exit code 0, no verify command (unverified) |
| `verify_failed` | exit code 0, verify command failed |
| `failed` | non-zero exit, bad result, or device disconnected |
| `rejected` | refused by the device's local policy |
| `blocked` | refused by the hub's policy floor (never sent) |
| `offline` | device not connected |
| `timeout` | no result within `timeout_s` + 15 s |

## 4. Talking to Mercury from a device

A paired device can send messages to the agent with its own token. The agent's API key never leaves the hub.

```
POST /v1/ask                 {"message": "battery health of my laptop?", "new_conversation": false}
                             -> {"run_id": "run_abc", "status": "started"}
GET  /v1/ask/{run_id}        -> {"status": "running" | "waiting_for_approval" | "completed" | "failed",
                                 "output": "...", "error": null, "pending_controls": [...]}
POST /v1/ask/{run_id}/approval  {"request_id": "<prompt_id>", "execution_generation": 3, "choice": "once"}
```

- All three need `Authorization: Bearer <device_token>`. A device can only see and answer its own requests.
- All paired devices continue one shared conversation, so a follow-up can come from any device. `new_conversation: true` starts a fresh thread for everyone. With `MERCURY_CONVERSATION=per-device` on the hub, each device keeps its own thread instead.
- Approval prompts for risky commands appear in `pending_controls`. The asking device answers with `once`, `session` or `deny`. Unanswered prompts are denied after the hub's timeout.
- `mercury-node ask "..."` implements this on the device.

## 5. Security summary

- Transport: WSS through a TLS reverse proxy (deploy/Caddyfile). Only `/health`, `/v1/pair`, `/v1/node` and `/v1/ask*` are public.
- Authentication: per-device random 256-bit tokens, hashed at rest, revocable.
- Authorisation, three layers:
  1. The hub's hard floor (`mercury_hub/policy.py`) refuses disk wipes, boot changes and similar, on every path.
  2. The agent's approval gate asks the user before destructive or system-changing commands (per-device `approval`: `all`, `dangerous`, `none`).
  3. The node's local policy can refuse anything, whatever the hub says.
