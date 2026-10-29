# Setting up Mercury on real devices

How to choose a model, add a Windows laptop and an Android phone, and walk through what Mercury can do with them. Each step shows what you should see.

You need:
- The hub running (an admin has done the hub README's *Install*, *Configure* and *Run* sections). For a demo on one office network, the hub URL can be `http://<hub-ip>:8765` (start the relay with `mercury relay --host 0.0.0.0`). Use `https://` with Caddy anywhere else.
- A Windows 10/11 laptop with internet access. No admin rights are needed.
- Part C only: an Android phone, and either Mercury's WhatsApp number (once its SIM is set up) or Termux.

---

## Part 0: Choose a model (hub)

1. To choose a model, run `mercury model` on the hub and pick a provider (see the hub README, *Configure*). An API key is the quickest.
2. To check it answers: `mercury chat -q "Reply with the word ready"`

Expect: `ready`. If it complains about the context window, the model or server offers less than 64K tokens. Pick a larger-context model, or raise the local server's context setting.

3. Start the gateway, which includes the API server the phone and desktop app use: `mercury gateway run`. Keep it running next to `mercury relay`.

---

## Part A: Add a Windows laptop

**A1. Install the node** (PowerShell, normal user):

```powershell
winget install Python.Python.3.12          # skip if `py --version` already works
git clone -b node --single-branch https://github.com/quantroninblue/mercury.git mercury-node
cd mercury-node
py -m pip install --user .
py -m mercury_node --help
```

Expect: the help text lists `pair`, `start`, `ask`, `status`, `policy`, `log`, `unpair`, `startup`.

**A2. Pair.** On the hub: `mercury pair laptop`. It prints a code like `K7QM-2XPA`. On the laptop:

```powershell
py -m mercury_node pair --hub http://<hub-ip>:8765 --code K7QM-2XPA
```

Expect: `Paired as 'laptop'`. The file `%APPDATA%\Mercury\node\node.json` exists and does **not** contain the token in plain text (it's encrypted with DPAPI).
If it says the code is invalid: codes are single-use and expire after 10 minutes. Make a new one.

**A3. Start.** Leave this window open:

```powershell
py -m mercury_node start
```

Expect: `connected to hub as 'laptop'`. On the hub, `mercury devices` shows `laptop  online  windows`.

**A4. A verified task from the hub CLI** (tests the relay on its own, without the agent):

```bash
mercury run laptop "Get-ChildItem $env:USERPROFILE\Downloads | Sort-Object Length -Descending | Select-Object -First 5 Name,Length | Format-Table | Out-String -Width 200" --verify "exit 0"
```

Expect: `verified`, then the five largest files in Downloads. The laptop window logs `running task ...` and `finished: exit 0, verify 0`.

**A5. Safety floor:**

```bash
mercury run laptop "Format-Volume -DriveLetter D"
```

Expect: `blocked ... refused: formatting or wiping a disk`. Nothing appears in the laptop window, because the hub never sent it.

**A6. Local policy on the laptop.** Run `py -m mercury_node policy`, open the file it shows, and set `"mode": "read-only"`. Then on the hub:

```bash
mercury run laptop "New-Item $env:TEMP\mercury-test.txt"
```

Expect: `rejected ... this device is read-only`. Set `"mode"` back to `"normal"` afterwards.

**A7. The laptop's own audit log:** `py -m mercury_node log`

Expect: every task from A4–A6, including the refused one.

**A8. Mercury picks the device and verifies.** On the hub, run `mercury` (chat) and type:

> What are the five largest files in Downloads on my laptop?

Expect: Mercury calls `mercury_devices`, then `mercury_run` on `laptop` with a verify command, and answers with the files plus a task id and "verified".

**A9. Approval for risky commands.** In the same chat:

> Delete the file mercury-test.txt in my temp folder on the laptop.

Expect: an approval prompt naming the laptop and the `Remove-Item ...` command. Answer `deny` first: Mercury reports it was not approved and does not retry another way. Ask again and approve once: the file is deleted and the result is verified (for example with `Test-Path` returning false).

**A10. Learn a new skill, validate it, reuse it.**

> What's my laptop's battery health? I want design capacity, full charge capacity and cycle count.

Expect, in order:
1. Mercury says it has no skill for this.
2. It researches `powercfg /batteryreport`.
3. It writes a skill with a parser script, a test and a sample.
4. It runs the test on the hub (sandbox).
5. It runs once on the laptop, with a verified result.
6. It asks you to approve the skill. Type `/skills pending`, then `/skills approve <id>`.

Then type `/new` to start a fresh conversation and ask:

> Check my laptop's battery health again.

Expect: Mercury loads the approved skill (`skill_view`) and answers **without** researching again.

**A11. Revoke.** On the hub: `mercury revoke laptop`.

Expect: the laptop window prints `Stopped: this device was revoked on the hub` and exits. `py -m mercury_node start` now refuses to connect.

**A12. Hub outage doesn't hurt the laptop.** Pair again (A2), start (A3), then stop the hub relay for a minute.

Expect: the laptop window shows `hub unreachable ... retrying in ...s`, the waits grow, the laptop works normally, and the node reconnects by itself when the relay is back.

---

## Part B: Talk to Mercury from any device (no WhatsApp needed)

On any paired device, laptop or phone:

```powershell
py -m mercury_node ask "What are the five largest files in Downloads on my laptop?"
```

Expect: progress lines, then Mercury's answer with a task id. For a risky request, the approval question appears **in this window** (`y` once, `s` session, anything else denies).

---

## Part C: Add an Android phone (once Mercury's number is set up)

**C1. Phone as the remote control (WhatsApp)**, once Mercury's number is set up:
1. On the hub: set `WHATSAPP_ENABLED=true` and `WHATSAPP_ALLOWED_USERS=<your number>` in `~/.mercury/.env`, run `mercury whatsapp`, and scan the QR code with **Mercury's** phone. Restart `mercury gateway run`.
2. From your own phone, message Mercury's number: `/devices`

Expect: the device list, with the laptop online.

3. Send: *What are the five largest files in Downloads on my laptop?*

Expect: the task runs on the laptop, and the reply with the result, task id and "verified" arrives on the phone.

4. Send a risky request (A9).

Expect: the approval question on WhatsApp. Reply `yes` or `no`.

5. Repeat A10 from the phone, approving the skill with `/skills approve <id>` in WhatsApp.

**C2. Phone without WhatsApp (Termux)**, as a remote control and as a node:
1. Install Termux from F-Droid, then: `pkg install python git termux-api`, then clone the `node` branch and `pip install .` (see the node README).
2. On the hub: `mercury pair phone --approval all`. On the phone: `mercury-node pair --hub ... --code ...`.
3. Remote control: `mercury-node ask "what are the five largest files in Downloads on my laptop?"`

Expect: the laptop runs the task and the answer appears on the phone.

4. Phone as a node: run `mercury-node start` in Termux, then ask Mercury from the laptop or hub: *What's my phone's battery level?*

Expect: with `approval all`, Mercury asks for approval, then runs `termux-battery-status` on the phone and answers.

---

## Where to see what happened

- Every task, on every device: `mercury tasks` on the hub (or the Tasks screen in Mercury Desktop).
- What ran on one device, including anything it refused: `py -m mercury_node log` on that device.
- Skills Mercury has learned and how often they succeed: `/skills pending` in chat, or the Skills screen in Mercury Desktop.
