"""Gate de conformité au spec : verdict, reviewer, persistance, intégration superviseur."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.contracts import (
    BadConfig,
    BadSprintLayout,
    GateVerdict,
    SpecVerdict,
    Story,
    StoryOutcome,
)
from conductor.supervisor import DefaultSpecReviewer, superviser


class _StubBad:
    """BadRunner factice : 1 story (verte), remédiation idempotente."""

    def __init__(self, outcome: StoryOutcome) -> None:
        self._outcome = outcome

    def run_sprint(self, layout: BadSprintLayout) -> list[StoryOutcome]:
        return [self._outcome]

    def remediate(self, story_id: str, layout: BadSprintLayout) -> StoryOutcome:
        return self._outcome


class _FailingSpecReviewer:
    """Toujours un under-build → spec_ok=False."""

    def review(self, story: Story, outcome: StoryOutcome, cwd: Path) -> SpecVerdict:
        return SpecVerdict.from_findings(
            [{"kind": "under-build", "criterion": "c", "detail": "d", "severity": "moyenne"}]
        )


class _AlwaysApprove:
    def approve(self, summary: str, results: object) -> bool:
        return True


def _design_pass(_outcome: StoryOutcome) -> GateVerdict:
    return GateVerdict(gate="design", passed=True)


def _layout(tmp_path: Path) -> BadSprintLayout:
    return BadSprintLayout(
        project_root=tmp_path,
        epics_md=tmp_path / "epics.md",
        sprint_status_yaml=tmp_path / "s.yaml",
        bmad_config_yaml=tmp_path / "c.yaml",
        config=BadConfig(),
    )


def test_specverdict_from_findings_over_build_only_passes() -> None:
    v = SpecVerdict.from_findings([{"kind": "over-build", "criterion": "x", "detail": "y"}])
    assert v.passed is True
    assert len(v.findings) == 1


def test_specverdict_from_findings_under_build_fails() -> None:
    v = SpecVerdict.from_findings([{"kind": "under-build", "criterion": "x", "detail": "y"}])
    assert v.passed is False


def test_specverdict_from_findings_empty_passes() -> None:
    assert SpecVerdict.from_findings([]).passed is True


def test_default_spec_reviewer_passes(tmp_path: Path) -> None:
    v = DefaultSpecReviewer().review(
        Story(id="1", epic="e", title="t"), StoryOutcome(story_id="1", code_ok=True), tmp_path
    )
    assert v.passed is True


def test_spec_failure_blocks_story(tmp_path: Path) -> None:
    outcome = StoryOutcome(story_id="1", code_ok=True, pr_url="http://pr/1")
    report = superviser(
        _layout(tmp_path),
        bad=_StubBad(outcome),
        design_check=_design_pass,
        hitl=_AlwaysApprove(),
        spec_reviewer=_FailingSpecReviewer(),
        stories=[Story(id="1", epic="e", title="t", acceptance=["c"])],
    )
    assert report.results[0].status == "blocked"  # under-build non résolu → blocked après retries


def test_resolve_spec_reviewer_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from conductor.harness.resolve import resolve_spec_reviewer

    monkeypatch.delenv("CONDUCTOR_ENABLE_SPEC_REVIEW", raising=False)
    assert isinstance(resolve_spec_reviewer(), DefaultSpecReviewer)


def test_resolve_spec_reviewer_env_zero_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from conductor.harness.resolve import resolve_spec_reviewer

    monkeypatch.setenv("CONDUCTOR_ENABLE_SPEC_REVIEW", "0")
    assert isinstance(resolve_spec_reviewer(), DefaultSpecReviewer)


def test_resolve_spec_reviewer_on_no_claude_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from conductor.harness.resolve import resolve_spec_reviewer

    monkeypatch.setenv("CONDUCTOR_ENABLE_SPEC_REVIEW", "1")
    monkeypatch.setattr("conductor.harness.resolve.shutil.which", lambda _name: None)
    assert isinstance(resolve_spec_reviewer(), DefaultSpecReviewer)


class _FakeCli:
    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path) -> str:
        self.prompts.append(prompt)
        return self._raw


def test_claude_spec_reviewer_parses_under_build(tmp_path: Path) -> None:
    from conductor.harness.spec_reviewer import ClaudeCliSpecReviewer

    cli = _FakeCli(
        '{"findings": [{"kind": "under-build", "criterion": "c", "detail": "d",'
        ' "severity": "moyenne"}]}'
    )
    v = ClaudeCliSpecReviewer(runner=cli).review(
        Story(id="1", epic="e", title="t", acceptance=["c"]),
        StoryOutcome(story_id="1", code_ok=True, pr_url="http://pr/1"),
        tmp_path,
    )
    assert v.passed is False
    assert "c" in cli.prompts[0]  # critère injecté dans le prompt


def test_claude_spec_reviewer_invalid_json_falls_back_to_pass(tmp_path: Path) -> None:
    from conductor.harness.spec_reviewer import ClaudeCliSpecReviewer

    v = ClaudeCliSpecReviewer(runner=_FakeCli("pas du json")).review(
        Story(id="1", epic="e", title="t", acceptance=["c"]),
        StoryOutcome(story_id="1", code_ok=True),
        tmp_path,
    )
    assert v.passed is True  # do-no-harm : interprétation indisponible → ne bloque pas
