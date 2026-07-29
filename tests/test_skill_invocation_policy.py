from pathlib import Path


def test_work_commands_are_visible_to_raw_text_invocation() -> None:
    # Given: every user-facing workflow skill exposes OpenAI invocation metadata.
    skill_root = Path(__file__).parents[1] / "skills"

    # When: Codex reads the machine-consumed implicit invocation policy.
    policies = [
        (skill_root / name / "agents" / "openai.yaml").read_text(encoding="utf-8")
        for name in ("work-on", "work-off", "work-calibration", "work-settings")
    ]

    # Then: only explicit work-on opt-in can activate monitoring; sibling controls remain visible.
    assert "allow_implicit_invocation: false" in policies[0]
    assert all("allow_implicit_invocation: true" in policy for policy in policies[1:])
