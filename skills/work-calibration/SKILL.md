---
name: work-calibration
description: Explain the retired implicit calibration workflow and route threshold recommendations to $work-settings. Use when the user asks about an older calibration prompt or recommendation.
---

# Work Calibration

CMW no longer injects calibration or locator context when a thread starts. Do not infer
paths, versions, recommendations, or decisions from rollout body text.

1. If the user asks to see the current recommendation, follow `$work-settings recommended`.
2. If the user asks to keep the fixed thresholds, follow `$work-settings default`.
3. If the user supplies two thresholds, follow `$work-settings <병목-의심> <심각-정체>`.
4. Do not run `calibration_cli.py` or a launcher as a fallback. Surface an MCP failure exactly.
5. A saved setting applies to the next explicit `$work-on`; it does not restart or alter an
   already monitored task.
