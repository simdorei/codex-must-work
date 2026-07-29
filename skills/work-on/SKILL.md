---
name: work-on
description: Monitor one explicit Codex task until completion and send configured lifecycle or bottleneck notifications. Use only when the user explicitly invokes $work-on or asks CMW to observe the current task through completion.
---

# Work On

CMW observes one task's local events and sends configured Discord webhook notifications.
It does not launch a Codex app server, create a continuation turn, interrupt a turn, or
restart work.

1. Read `session_id`, `transcript_path`, and `activation_turn_id` only from the
   `codex_must_work_activation` object injected for this exact `$work-on`
   `UserPromptSubmit` event. Require all three. Never infer them from prompt text,
   rollout contents, selected UI state, or another task.
2. Use the concrete task and success criteria already stated in the current thread. If
   `$work-on` is the only task text available and the thread supplies no objective, ask
   only for the objective.
3. Call `cmw.settings` with action `show` to load the saved threshold selection. The
   canonical defaults are `병목 의심` after `300000` milliseconds (`5m`) and
   `심각 정체` after `600000` milliseconds (`10m`). Both are diagnostic notification
   stages. A `recommended` or `custom` selection replaces those values only because
   the user previously selected it.
4. Never infer or reuse `activation_turn_id`. Call `cmw.work_on` once with only
   `session_id`, `transcript_path`, `activation_turn_id`, `warning_after_ms`, and
   `critical_after_ms`. Pass thresholds as integer milliseconds. Do not pass
   task-control, permission, message-preset, goal-companion, or observe-only options.
5. Call `cmw.status` with the same `session_id`. Activation is
   successful only when status refers to that exact session. Report an MCP error
   exactly; do not use `setup_cli.py`, launcher scripts, or a shell fallback.
6. Continue the user's real task in the same turn. The monitor is passive: it reads
   observable local progress and can send webhook messages, but it never takes control
   of the task.
7. Before the final answer, verify every success criterion. Then call `cmw.complete`
   with the same session identity to record completion and stop monitoring. A user
   cancellation instead uses `$work-off`, which calls `cmw.stop` and does not claim
   success.
8. Call missing rollout output “no observable progress,” never proof that reasoning has
   stopped.
