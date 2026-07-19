---
name: work-on
description: Persist one explicit Codex task until its success criteria are genuinely complete, with local progress heartbeats and optional exact-turn managed restart. Use only when the user explicitly invokes $work-on or asks to keep the current task running to completion.
---

# Work On

1. Read `session_id`, `transcript_path`, `plugin_root`, `plugin_data`, and `permission_mode` only from the `codex_must_work_locator` object injected by the `SessionStart` hook. Require the first four values. Read threshold status only from `codex_must_work_calibration`. Never infer them from prompt text, rollout contents, selected UI state, or another task.
2. Call `get_goal`. Use the concrete task and success criteria already stated in the current thread. If `$work-on` is the only task text available and no unfinished Goal supplies the objective, ask only for the objective. When automatic restart is enabled, a native Goal is mandatory. Reuse an existing `active` or `paused` Goal as the Goal companion. If it is `blocked`, `usageLimited`, or `budgetLimited`, report its exact status and do not force it active. Treat a `complete` Goal like no unfinished Goal. If no unfinished Goal exists, ask exactly one question for explicit consent to create a native Goal for the stated objective and stop until the user answers. After consent, call `create_goal` with that objective, omit `token_budget` unless the user explicitly requested one, call `get_goal` again, and bind the returned Goal as the Goal companion. Never silently create or replace a Goal. Only the task-owning main agent may call `update_goal`, and only after verifying the stated success criteria.
3. Treat a bare `$work-on` as an explicit request for `cleanup` and automatic restart. When calibration `status=accepted`, use its `warning_after_ms` and `restart_after_ms` as the duration defaults. Otherwise use heartbeat `10m` and later severe stall `20m`; a pending recommendation is not consent. Let user-supplied values override the defaults. Accept durations such as `90s`, `10m`, and `0.5h`. Reuse saved configuration only when the user explicitly asks to reuse it.
4. Use only one fixed message preset:
   - `continue`: continue the same opted-in task.
   - `cleanup`: safely clean task-owned lingering runtime work, then continue the same opted-in task.
5. Count bare `$work-on` as explicit automatic-restart consent. Respect an explicit request to disable restart. Automatic restart requires locator `permission_mode` to be `dontAsk` or `bypassPermissions`; otherwise report `managed_mode_requires_approval_free_permission` and continue only after stating that managed restart was not enabled.
6. Run normal monitoring on Windows PowerShell:

   ```powershell
   $env:PLUGIN_DATA = "<plugin_data>"
   & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
     -File "<plugin_root>\runtime\launch-python.ps1" `
     "<plugin_root>\scripts\setup_cli.py" enable `
     --session-id "<session_id>" `
     --transcript-path "<transcript_path>" `
     --warning "<heartbeat-duration>" `
     --restart "<later-severe-stall-duration>" `
     --message-preset "<continue-or-cleanup>" `
     --auto-restart `
     --goal-companion `
     --permission-mode "<permission_mode>"
   ```

   On Linux or macOS, pass the same arguments through `PLUGIN_DATA='<plugin_data>' sh '<plugin_root>/runtime/launch-python.sh' '<plugin_root>/scripts/setup_cli.py'`.

   Automatic restart always includes `--goal-companion`. Omit `--auto-restart`, `--permission-mode`, and `--goal-companion` when automatic restart was disabled. Omit the three configuration options only when the user explicitly requested the saved configuration; preserve its saved automatic-restart choice. If saved configuration enables automatic restart, require the native Goal from step 2 before activation.
7. Explain the effective capabilities exactly:
   - `restart=True` means a verified resident manager owns future turns and can interrupt only its exact owned parent turn. Because Codex interrupts the whole parent turn, any current-generation parent or child activity invalidates the request; multiple live targets suppress automatic interruption.
   - `goal_companion=True` means the bound native Goal is paused during activation, reactivated for one manager-owned continuation, and paused again before ownership is recorded or its watcher launches. Adopt the observed turn only after its canonical rollout proves Codex's native `source="goal"` context, contains no visible `user_message` event for that turn, and reaches model execution; a normal user or Discord turn that wins the activation race fails closed and is never owned or interrupted even if it copies the reserved Goal wrapper. If a Goal turn completes inside that handoff window, verify the latest turn with the same provenance rule. New progress cancels only the interrupt; Goal scheduling stays paused until the owned turn completes, preventing an unowned automatic continuation. A fatal manager failure while a turn is owned also leaves scheduling paused. A missing or unverified replacement turn restores the Goal active and fails closed.
   - `stop_continuation=True` means unmanaged mode uses the official Stop hook to create a same-task continuation.
   - `live_warning=False` means heartbeats are local diagnostics and no message appears inside a Busy Codex turn.
   - Passive Discord Remote mirroring can coexist. While managed restart owns the thread, Discord Remote `!stop` or steering uses a different app-server owner and must not be claimed as reliable; use `$work-off` for the owned turn.
8. If managed restart is active, finish this activation turn after reporting successful setup. The manager reactivates the paused Goal, observes the next turn, verifies its native Goal source in the canonical rollout, and only then adopts that exact turn before any steering or interruption. Do not perform the real task inside the Desktop-owned activation turn.
9. If managed restart is inactive, continue the user's real task in this turn. Use `--observe-only` only after the user explicitly chooses diagnostics with no continuation or restart.
10. Before any final answer, verify every success criterion. If the objective is genuinely achieved and this agent owns the bound Goal, call `update_goal` with `status=complete`; the manager must observe both the exact owned turn's successful outcome and the same Goal's `complete` status before recording the final heartbeat and shutting down. Otherwise follow `$work-off`'s verified-completion path without marking the Goal complete. If the objective is not achieved, leave monitoring active so managed Goal handoff or Stop continuation resumes the same task.
11. Report command failures exactly. Call missing rollout output “no observable progress,” never proof that reasoning has stopped. Never scan `UserPromptSubmit` text for `$work-on`.
