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


def test_managed_workflow_does_not_create_a_competing_goal_owner() -> None:
    # Given: the managed workflow owns continuation and replacement turns.
    instructions = (Path(__file__).parents[1] / "skills" / "work-on" / "SKILL.md").read_text(
        encoding="utf-8",
    )

    # When: its tool-routing instructions are inspected.
    competing_goal_tools = ("create_goal", "update_goal")

    # Then: no second automatic turn owner is created by the workflow.
    assert all(tool not in instructions for tool in competing_goal_tools)
