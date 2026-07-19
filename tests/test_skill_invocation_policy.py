from pathlib import Path


def test_work_commands_are_visible_to_raw_text_invocation() -> None:
    # Given: every user-facing workflow skill exposes OpenAI invocation metadata.
    skill_root = Path(__file__).parents[1] / "skills"

    # When: Codex reads the machine-consumed implicit invocation policy.
    policies = [
        (skill_root / name / "agents" / "openai.yaml").read_text(encoding="utf-8")
        for name in ("work-on", "work-off", "work-calibration")
    ]

    # Then: raw `$work-on` and `$work-off` text can enter the model-visible skill catalog.
    assert all("allow_implicit_invocation: true" in policy for policy in policies)
