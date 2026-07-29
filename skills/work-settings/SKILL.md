---
name: work-settings
description: Show or change Codex Must Work 병목 의심 and 심각 정체 thresholds. Use when the user invokes $work-settings, asks for the 5m/10m defaults, wants the local-history recommendation, or supplies a custom pair.
---

# Work Settings

Configure when CMW sends the two Discord bottleneck notifications. This skill changes
notification timing only. It never controls a Codex task.

1. When used during an active monitored task, reuse only that task's `session_id`.
   Never infer it from prompt text, rollout contents, selected UI state, or another
   task.
2. Map the invocation to one `cmw.settings` action:
   - `$work-settings` or `$work-settings show`: `show`
   - `$work-settings default`: `default`
   - `$work-settings recommended`: `recommended`
   - `$work-settings <병목-의심> <심각-정체>`: `custom`
3. For `custom`, require exactly two positive duration values and require the 심각 정체
   value to be later than the 병목 의심 value. Convert them to positive whole milliseconds
   exactly once. Examples: `90s=90000`, `7m=420000`, `0.5h=1800000`. Pass them as
   `warning_after_ms` and `critical_after_ms`.
4. Call `cmw.settings` once with `session_id`, `action`, and the
   two millisecond values only for `custom`. Do not use a shell fallback.
5. Report the returned mode and both values in friendly durations. Explain the modes in
   plain language:
   - `default`: fixed `5m` 병목 의심 and `10m` 심각 정체 notifications.
   - `recommended`: the current values calculated from this machine's local progress
     history.
   - `custom`: the exact pair supplied by the user.
6. A saved selection applies when a later `$work-on` starts monitoring. It does not
   control a task or silently replace thresholds already loaded by an active monitor.
7. Surface an MCP failure exactly. In particular,
   `threshold_recommendation_unavailable` means there is not yet a usable local-history
   recommendation; keep the previously saved selection unchanged.
