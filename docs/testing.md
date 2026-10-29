# Test log

Automated and manual test results for the hub.

## How to run

```bash
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn websockets httpx pytest
.venv/bin/python -m pytest
```

The suite runs the real relay (uvicorn) and real WebSocket connections from a reference node (`tests/fake_node.py`) that speaks the same protocol as the `node` branch, and loads the agent plugin the way the engine does (`register(ctx)`).

## Status

| Feature | Evidence | Status |
|---|---|---|
| Device pairing (one-time codes, hashed tokens, expiry, single use, rate limit) | `test_db.py`, `test_relay.py` | Passed (automated) |
| Authenticated device connection, replace and revoke | `test_relay.py` | Passed (automated) |
| Remote task execution with verification and artifact hashes | `test_relay.py::test_verified_task_round_trip`, `test_cli.py` | Passed (automated, local node) |
| Policy: hard floor, approval rules (Windows and POSIX), device modes | `test_policy.py`, relay floor test | Passed (automated) |
| Agent tools and approval hook directives | `test_plugin.py` | Passed (automated, engine not running) |
| Skill outcome tracking | `test_db.py::test_skill_stats`, `test_plugin.py` | Passed (automated) |
| Hub CLI (`pair`, `devices`, `run`, `tasks`, `policy`, `revoke`, `admin-token`) | `test_cli.py` (real relay process) | Passed (automated) |
| Device talks to the agent (`/v1/ask`), approval answered from the device | `test_ask.py` (agent API stubbed) | Passed (automated) |
| Real node client with real relay: pair, online, verified task, floor block, audit log, revoke stops node | live run, both processes on Linux | Passed (manual, 2026-10-29 16:52) |
| install.sh home setup (idempotent, secrets generated, `.env` is 600) | manual run with `--no-env` in a scratch HOME | Passed (manual, 2026-10-29) |
| Engine running with the plugin (CLI chat, approval prompt) | engine installed; plugin loads and is enabled (`mercury plugins list`) | Plugin load verified; live run: [device-setup.md](device-setup.md) Part 0, A8, A9 |
| Self-learning: research, skill, sandbox test, approval, reuse | runs once a model is chosen | [device-setup.md](device-setup.md) A10 |
| Windows laptop as a real node | Windows support built in (DPAPI token store, PowerShell runner) | [device-setup.md](device-setup.md) Part A |
| Phone to laptop over WhatsApp | once Mercury's WhatsApp number is set up | [device-setup.md](device-setup.md) Part C |

## Latest automated run

Run on 2026-10-29 17:34 IST, Python 3.12.3, Linux.

```
tests/test_ask.py::test_device_asks_and_gets_reply PASSED
tests/test_ask.py::test_conversation_continues_until_new PASSED
tests/test_ask.py::test_approval_round_trip_from_device PASSED
tests/test_ask.py::test_ask_requires_device_token_and_ownership PASSED
tests/test_ask.py::test_ask_when_agent_api_not_configured PASSED
tests/test_cli.py::test_cli_end_to_end PASSED
tests/test_cli.py::test_admin_token_write PASSED
tests/test_db.py::test_pairing_code_is_single_use_and_token_is_hashed PASSED
tests/test_db.py::test_expired_and_unknown_codes_rejected PASSED
tests/test_db.py::test_duplicate_names_and_bad_modes_rejected PASSED
tests/test_db.py::test_revoke_kills_token_and_frees_name PASSED
tests/test_db.py::test_result_status_derivation PASSED
tests/test_db.py::test_final_task_is_immutable PASSED
tests/test_db.py::test_output_is_clipped PASSED
tests/test_db.py::test_skill_stats PASSED
tests/test_db.py::test_fail_unfinished PASSED
tests/test_plugin.py::test_registers_tools_hooks_and_commands PASSED
tests/test_plugin.py::test_devices_and_run_tools PASSED
tests/test_plugin.py::test_unverified_result_is_flagged PASSED
tests/test_plugin.py::test_pre_tool_call_directives PASSED
tests/test_plugin.py::test_skill_outcomes_recorded_from_session PASSED
tests/test_plugin.py::test_relay_down_is_reported_not_raised PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[rm -rf /] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[rm -rf ~] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[sudo rm -rf / --no-preserve-root] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[mkfs.ext4 /dev/sda1] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[dd if=/dev/zero of=/dev/sda bs=1M] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[:(){ :|:& };:] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[Format-Volume -DriveLetter D] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[format c: /q] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[diskpart /s wipe.txt] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[bcdedit /set {default} safeboot minimal] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[Remove-Item -Recurse -Force C:\\] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[Remove-Item -Recurse -Force 'C:\\Windows'] PASSED
tests/test_policy.py::test_hard_floor_blocks_whatever_the_device_mode[vssadmin delete shadows /all] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[Remove-Item C:\\Users\\me\\Downloads\\old.zip] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[del report.txt] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[rm notes.txt] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[Stop-Process -Name chrome] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[taskkill /IM notepad.exe /F] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[Restart-Computer] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[shutdown /s /t 0] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[winget install Git.Git] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[pip install requests] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[Set-ExecutionPolicy Unrestricted] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[reg add HKCU\\Software\\X /v Y /d 1] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[iex (irm https://example.com/x.ps1)] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[curl -fsSL https://example.com/x.sh | bash] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[git push --force origin main] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[net user bob P@ss /add] PASSED
tests/test_policy.py::test_destructive_commands_need_approval[sudo apt install htop] PASSED
tests/test_policy.py::test_read_and_benign_commands_run_without_approval[Get-ChildItem $env:USERPROFILE\\Downloads | Sort-Object Length -Descending | Select-Object -First 5] PASSED
tests/test_policy.py::test_read_and_benign_commands_run_without_approval[Get-CimInstance Win32_Battery] PASSED
tests/test_policy.py::test_read_and_benign_commands_run_without_approval[powercfg /batteryreport /output $env:TEMP\\battery.html] PASSED
tests/test_policy.py::test_read_and_benign_commands_run_without_approval[ls -la ~/Downloads] PASSED
tests/test_policy.py::test_read_and_benign_commands_run_without_approval[df -h] PASSED
tests/test_policy.py::test_read_and_benign_commands_run_without_approval[termux-battery-status] PASSED
tests/test_policy.py::test_read_and_benign_commands_run_without_approval[echo hello > out.txt] PASSED
tests/test_policy.py::test_read_and_benign_commands_run_without_approval[python3 script.py] PASSED
tests/test_policy.py::test_device_modes PASSED
tests/test_policy.py::test_device_blocked_patterns PASSED
tests/test_policy.py::test_empty_and_nul_commands_blocked PASSED
tests/test_relay.py::test_health_is_public_and_admin_requires_token PASSED
tests/test_relay.py::test_pair_connect_and_list_online PASSED
tests/test_relay.py::test_verified_task_round_trip PASSED
tests/test_relay.py::test_failed_and_unverified_outcomes PASSED
tests/test_relay.py::test_hard_floor_blocked_at_relay_even_without_agent PASSED
tests/test_relay.py::test_offline_device_and_unknown_device PASSED
tests/test_relay.py::test_node_local_policy_rejection PASSED
tests/test_relay.py::test_timeout_sends_cancel PASSED
tests/test_relay.py::test_async_task_then_poll PASSED
tests/test_relay.py::test_revoke_disconnects_and_token_stops_working PASSED
tests/test_relay.py::test_bad_device_token_and_pairing_rate_limit PASSED
tests/test_relay.py::test_policy_update PASSED
tests/test_relay.py::test_newer_connection_replaces_older PASSED
tests/test_relay.py::test_skills_listing PASSED
============================= 76 passed in 10.11s ==============================
```
