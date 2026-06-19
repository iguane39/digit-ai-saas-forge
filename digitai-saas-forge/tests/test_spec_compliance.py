"""Gate de conformité au spec : verdict, reviewer, persistance, intégration superviseur."""

from __future__ import annotations

from conductor.contracts import SpecVerdict


def test_specverdict_from_findings_over_build_only_passes() -> None:
    v = SpecVerdict.from_findings([{"kind": "over-build", "criterion": "x", "detail": "y"}])
    assert v.passed is True
    assert len(v.findings) == 1


def test_specverdict_from_findings_under_build_fails() -> None:
    v = SpecVerdict.from_findings([{"kind": "under-build", "criterion": "x", "detail": "y"}])
    assert v.passed is False


def test_specverdict_from_findings_empty_passes() -> None:
    assert SpecVerdict.from_findings([]).passed is True
