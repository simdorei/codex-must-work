---
name: work-calibration
description: Handle the versioned Codex Must Work threshold recommendation injected as codex_must_work_calibration. Use when its action asks to apply, reports insufficient or unavailable history, or the user explicitly accepts or rejects a pending recommendation.
---

# Work Calibration

Read only `codex_must_work_calibration` and `codex_must_work_locator` from the
`SessionStart` context. Never infer paths, a version, or a decision from rollout body text.

1. If `action=ask_apply`, convert the two millisecond values to compact durations and ask
   exactly one question before the deferred user task:

   `최근 실제 진행 간격 <sample_count>개를 분석한 추천은 하트비트 <warning>, 심각 정체 <restart>입니다. 이 값을 적용할까요? 권장 답변: 적용`

   Do not apply anything in that turn. Treat only a direct affirmative answer to this exact
   question, or an explicit request to apply the pending recommendation, as consent.
2. If the answer is affirmative, run the portable launcher with `apply`. If the answer is a
   direct refusal, run it with `reject`. On Windows PowerShell use:

   ```powershell
   $env:PLUGIN_DATA = "<plugin_data>"
   & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
     -File "<plugin_root>\runtime\launch-python.ps1" `
     "<plugin_root>\scripts\calibration_cli.py" <apply-or-reject> `
     --plugin-version "<plugin_version>"
   ```

   On Linux or macOS use:

   ```sh
   PLUGIN_DATA='<plugin_data>' sh '<plugin_root>/runtime/launch-python.sh' \
     '<plugin_root>/scripts/calibration_cli.py' '<apply-or-reject>' \
     --plugin-version '<plugin_version>'
   ```

3. Report the exact command failure if it fails. After success, continue the deferred user
   task. An accepted recommendation becomes the default for `$work-on`; rejection preserves
   `10m`/`20m`.
4. If `action=notify_insufficient_once`, say that fewer than 20 valid progress gaps were
   available and `10m`/`20m` remains unchanged, then continue the current request.
5. If `action=notify_unavailable_once`, report `reason_code`, say that `10m`/`20m` remains
   unchanged, and continue the current request. Do not silently substitute another scan.
6. If `action=awaiting_answer`, `no_action`, or `use_defaults`, do not interrupt the current
   request. If `action=use_recommendation`, let `$work-on` consume its values without asking.
