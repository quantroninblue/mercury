# Mercury node

The small client that lets one of your devices take tasks from a Mercury hub. Install it on each Windows laptop or PC, Android phone (Termux), Linux or macOS machine you want Mercury to work on.

| Branch | What it is |
|---|---|
| [`hub`](../../tree/hub) | The agent and device relay, on the office server |
| **`node`** (this one) | This client, on each device |

```bash
git clone -b node --single-branch https://github.com/quantroninblue/mercury.git mercury-node
```

## What it does, and what it doesn't

- **It connects out to the hub over an encrypted WebSocket.** No open ports, no SSH server, no VPN, no admin rights.
- **It runs only tasks that pass this device's own policy.** Every task it runs, and every one it refuses, is written to a local audit log.
- **It's light:** pure Python plus one library (`websockets`). It idles when there's nothing to do.
- **If the hub is down, it waits and reconnects** with increasing delays, from 1 second up to 5 minutes. Nothing else on the device is affected.
- **If the hub revokes this device, it stops** instead of retrying.
- **It never starts by itself** unless you turn on autostart.

## Install

Requires Python 3.10+.

**Windows** (PowerShell, no admin needed):

```powershell
winget install Python.Python.3.12        # skip if Python is installed
cd mercury-node
py -m pip install --user .
```

If `mercury-node` isn't found afterwards, run it as `py -m mercury_node`.

**Android** (install [Termux](https://f-droid.org/packages/com.termux/) from F-Droid; the Play Store build is outdated):

```bash
pkg install python git
git clone -b node --single-branch https://github.com/quantroninblue/mercury.git mercury-node
cd mercury-node && pip install .
# optional, for phone features (battery, notifications): install the Termux:API app, then
pkg install termux-api
```

**Linux / macOS:**

```bash
cd mercury-node && python3 -m pip install --user .
```

## Pair and start

On the hub, an admin runs `mercury pair my-laptop` and gives you a one-time code (valid 10 minutes). Then, on the device:

```bash
mercury-node pair --hub https://mercury.example.com --code K7QM-2XPA
mercury-node start            # keep this window open; Ctrl+C to stop
```

Optional autostart when you sign in:

```bash
mercury-node startup          # Windows: Startup folder. Android: Termux:Boot.
mercury-node startup --remove
```

## Talk to Mercury from this device

```bash
mercury-node ask "what are the 5 biggest files in Downloads on my laptop?"
mercury-node ask --new "start a fresh conversation"
```

If Mercury needs approval for something risky, you're asked right there: `y` approves once, `s` approves for the session, anything else denies. This is how a phone can send tasks to a laptop without WhatsApp.

## Your device, your rules

`mercury-node policy` shows the policy file. Edit it to change what the hub may run here. Changes apply from the next task.

```json
{
  "mode": "normal",
  "blocked_patterns": ["*\\Documents\\Finance*"],
  "allowed_cwd": [],
  "confirm_on_device": false,
  "max_timeout_s": 900
}
```

| Setting | Effect |
|---|---|
| `mode: "paused"` | refuse everything |
| `mode: "read-only"` | refuse anything that looks like it changes files, settings or processes |
| `blocked_patterns` | refuse commands matching these globs |
| `allowed_cwd` | tasks may only start in these folders |
| `confirm_on_device: true` | ask in the node window before every task |
| `max_timeout_s` | upper limit on how long any task may run |

Disk wipes, formatting and boot changes are always refused here, whatever the hub says.

Other commands:

```bash
mercury-node status           # pairing, policy, hub reachable?
mercury-node log              # what the hub ran here
mercury-node unpair           # forget the pairing (also revoke it on the hub)
```

## Where things are stored

| | Windows | Android / Linux / macOS |
|---|---|---|
| Settings and device token | `%APPDATA%\Mercury\node\node.json` (token encrypted with Windows DPAPI) | `~/.config/mercury-node/node.json` (mode 600) |
| Policy | `...\policy.json` | `.../policy.json` |
| Audit log | `...\tasks.log` | `.../tasks.log` |

## Mercury Desktop (optional app)

`desktop/` is a desktop app for Windows, macOS and Linux:
- **Chat with Mercury**, with approval prompts as buttons.
- **On the hub, or with the hub admin token:** your devices (pair, change approval mode, revoke), the task log with outputs and verification, and skills with their success rates and pending approvals.
- **Five themes:** Quicksilver, Caduceus, Daylight, Retrograde and Terminal.

Requires Node.js 20+.

```bash
cd desktop
npm install
npm start                     # builds the UI and opens the app
```

Pair it like any device. On the hub, run `mercury pair desktop`, then enter the hub URL and code under **Connections**. The app's token is stored encrypted with the OS keychain. When the app runs on the hub machine itself, it picks up the admin token from `~/.mercury/.env` automatically.

On Ubuntu 24.04 and other systems that restrict user namespaces, a development copy needs Electron's sandbox helper owned by root, once:

```bash
sudo chown root:root node_modules/electron/dist/chrome-sandbox && sudo chmod 4755 node_modules/electron/dist/chrome-sandbox
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install websockets pytest
.venv/bin/python -m pytest        # 42 tests: policy, runner, config, full protocol session
cd desktop && npm install && npm run build   # desktop app
```

The wire protocol is specified in the hub branch: [docs/protocol.md](../../blob/hub/docs/protocol.md). Setting up a Windows laptop or Android phone: [docs/device-setup.md](../../blob/hub/docs/device-setup.md).

Mercury is built on [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research. This client is Mercury's own code, MIT licensed.
