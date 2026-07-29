---
name: work-off
description: Stop Codex Must Work monitoring for the current task, or record verified completion and stop. Use when the user invokes $work-off or a $work-on task has been fully verified.
---

# Work Off

1. Reuse only the `session_id` supplied by the current task's exact `$work-on`
   activation context. Never infer it from prompt text, rollout contents, selected UI
   state, or another task.
2. Distinguish verified completion from a manual stop. A request to stop is not proof
   that the task succeeded.
3. If every success criterion was already verified, call `cmw.complete` with
   `session_id`. This records completion and stops monitoring.
4. Otherwise call `cmw.stop` with that session ID. This stops monitoring without
   claiming completion. It does not interrupt or restart Codex.
5. Call `cmw.status` only when the control API keeps stopped sessions readable. Report
   the actual result or error; do not substitute a shell fallback.
6. Preserve the user's saved default, recommended, or custom bottleneck selection while
   removing only the current task's temporary monitoring state.
