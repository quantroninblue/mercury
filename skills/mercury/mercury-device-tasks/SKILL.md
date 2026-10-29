---
name: mercury-device-tasks
description: Run a task on one of the user's paired devices (Windows, Linux, macOS, Android/Termux) and return a verified result. Use for any request that touches a laptop, PC or phone.
version: 1.0.0
author: Mercury
license: MIT
metadata:
  hermes:
    tags: [Mercury, Devices, Remote, Windows, Android, Verification]
    related_skills: [mercury-learn-skill]
---

# Running tasks on devices

## 1. Choose the device

Call `mercury_devices`. Each entry has `name`, `online`, `platform` (`windows`, `linux`, `darwin`, `android`), `hostname`, `capabilities.shells` and `approval`.

- Use the device the user named. Otherwise pick by fit: Windows-only tasks go to a `windows` device, phone tasks to `android`.
- If the right device is offline, tell the user (`/devices` on WhatsApp shows the list) instead of using another one.

## 2. Write the command for that device's shell

| Platform | Default shell | Notes |
|---|---|---|
| windows | PowerShell 5.1+ | Use full cmdlet names (`Get-ChildItem`, not `ls`). Paths: `$env:USERPROFILE\Downloads`. Output objects with `ConvertTo-Json -Depth 3` or `Format-Table -AutoSize \| Out-String -Width 200` so text is not truncated. |
| linux / darwin | sh | POSIX sh; do not assume bash features unless `capabilities.shells` lists bash. |
| android | sh (Termux) | Home is `/data/data/com.termux/files/home`. Phone data via Termux:API commands (`termux-battery-status`, `termux-notification`) when `capabilities.termux_api` is true. Shared storage is `~/storage/shared` after `termux-setup-storage`. |

Keep commands read-only unless the user asked for a change. Quote paths with spaces.

## 3. Always verify

Pass `verify_command`, a second check that exits 0 only if the outcome is real:

- Created or changed a file: `Test-Path <file>` (PowerShell: `if (Test-Path X) { exit 0 } else { exit 1 }`) or `test -s <file>`.
- Reported a value: re-read it a different way, or check the output is non-empty and well-formed.
- Produced files: list them in `collect_paths`; the device returns their size and SHA-256.

Statuses: `verified` (command and check passed), `completed` (no check given, so unverified), `failed`, `verify_failed`, `rejected` (device's local policy said no), `blocked` (hub policy), `offline`, `timeout`. Only call a task done when it is `verified`.

## 4. Long jobs

For anything over about two minutes use `wait: false`, tell the user the task id, and check with `mercury_task`. `mercury_tasks` lists recent tasks for "what's the status" questions from any device.

## 5. Approvals

Destructive or system-changing commands pause for the user's yes automatically. If a command is refused or not approved, do not retry it in another form. Explain and offer a safer option.

## 6. Reporting back

On a phone, reply with: what ran, on which device, the result (trimmed), and `verified` or why not. Include the task id so the user can ask about it later.
