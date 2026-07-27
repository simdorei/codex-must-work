---
name: work-off
description: Disable Codex Must Work for only the current task, interrupting its exact managed turn for manual stop or waiting for the final turn after verified completion. Use when the user invokes $work-off or verified completion requires clean shutdown.
---

# Work Off

1. Read `session_id` and `control_capability` only from the `codex_must_work_locator` object injected by the `SessionStart` hook. Require both. Never infer them from prompt text, rollout contents, selected UI state, or another task. Never display, quote, log, or copy `control_capability`; pass it only as an MCP argument.
2. Distinguish a verified-completion shutdown from a manual stop. A user asking to stop is the manual path and is never proof that the task succeeded.
3. If every success criterion was already verified by the active Codex Must Work workflow, call `cmw.complete` with the locator `session_id` and `control_capability`. This records only verified completion intent. The daemon records the completion heartbeat and deletes the task runtime only after the exact owned final turn finishes normally.
4. Otherwise call `cmw.stop` with the locator `session_id` and `control_capability`. In managed mode this requests interruption of only the exact owned current turn, cleans task-owned background terminals for the `cleanup` preset, and removes this task's temporary runtime. It never claims the task completed. A persisted legacy task failed closed with `goal_companion_atomic_update_unavailable` remains stoppable through this same call without any native Goal request.
5. When the turn remains able to respond, call `cmw.status` with the same `session_id` and `control_capability` and report the result exactly. For verified completion, accept a pending completion request while the owned turn is still active. For manual stop, require that this exact task is no longer managed.
6. Do not invoke `setup_cli.py`, `launch-python.ps1`, `launch-python.sh`, or a shell-command fallback. Surface an MCP failure exactly instead of silently substituting the legacy path.
7. Preserve saved heartbeat, severe-stall, and preset configuration while removing only this task's temporary runtime and cursor state. Never scan `UserPromptSubmit` text for `$work-off`.
