---
name: work-off
description: Disable Codex Must Work for only the current task, interrupting its exact managed turn for manual stop or waiting for the final turn after verified completion. Use when the user invokes $work-off or verified completion requires clean shutdown.
---

# Work Off

1. Read `session_id`, `plugin_root`, and `plugin_data` only from the `codex_must_work_locator` object injected by the `SessionStart` hook. Require all three. Never infer them from prompt text, rollout contents, selected UI state, or another task.
2. Distinguish a verified-completion shutdown from a manual stop. A user asking to stop is the manual path and is never proof that the task succeeded.
3. If every success criterion was already verified by the active Codex Must Work workflow, run:

   ```powershell
   $env:PLUGIN_DATA = "<plugin_data>"
   & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
     -File "<plugin_root>\runtime\launch-python.ps1" `
     "<plugin_root>\scripts\setup_cli.py" disable `
     --session-id "<session_id>" `
     --completed
   ```

   For a managed turn, this records a verified-completion request. The managed owner records the completion heartbeat only after the final turn finishes normally, then deletes its runtime. On Linux or macOS, pass the same arguments through `PLUGIN_DATA='<plugin_data>' sh '<plugin_root>/runtime/launch-python.sh' '<plugin_root>/scripts/setup_cli.py'`.
4. Otherwise run:

   ```powershell
   $env:PLUGIN_DATA = "<plugin_data>"
   & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
     -File "<plugin_root>\runtime\launch-python.ps1" `
     "<plugin_root>\scripts\setup_cli.py" disable --session-id "<session_id>"
   ```

   In managed mode this requests interruption of the exact owned current turn, cleans task-owned background terminals for the `cleanup` preset, and removes this task's runtime. It never claims the task completed.
5. Report the command result exactly when the turn remains able to respond. Preserve saved heartbeat, severe-stall, and preset configuration while removing only this task's temporary runtime and cursor state.
6. Never scan `UserPromptSubmit` text for `$work-off`.
