"""Abstraction du provider Git (portabilité — backlog P-04).

Découple la forge de GitHub. Interface ``GitProvider`` (observation des PR/MR, source de vérité
du run) + implémentations : **GitHub** (via la CLI ``gh``), **Azure DevOps** (via la CLI ``az
repos``) et **GitLab** (stub tracé, à implémenter). Sélection par l'URL du remote ``origin``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from conductor.harness._text import clip
from conductor.harness.gh import GhRunner, SubprocessGh
from conductor.process import ProcessRunner, SubprocessProcessRunner


class GitProvider(Protocol):
    """Observe l'état des PR/MR d'un dépôt (source de vérité du run)."""

    def list_prs(self, cwd: Path) -> list[dict[str, Any]]: ...


class GitHubProvider:
    """Provider GitHub : délègue à la CLI ``gh`` (parité du comportement actuel)."""

    def __init__(self, runner: GhRunner | None = None) -> None:
        self._gh = runner or SubprocessGh()

    def list_prs(self, cwd: Path) -> list[dict[str, Any]]:
        return self._gh.list_prs(cwd)


class UnsupportedProvider:
    """Stub tracé pour un provider pas encore implémenté (Azure DevOps, GitLab)."""

    def __init__(self, name: str) -> None:
        self.name = name

    def list_prs(self, cwd: Path) -> list[dict[str, Any]]:
        raise NotImplementedError(
            f"Provider Git '{self.name}' non implémenté : seul GitHub (gh) l'est aujourd'hui. "
            "Ajouter une impl GitProvider pour cette forge (list_prs via son API/CLI)."
        )


class AzRunner(Protocol):
    """Observe les PR Azure DevOps via ``az repos`` (injectable ; fake en test)."""

    def list_prs(self, cwd: Path) -> list[dict[str, Any]]: ...
    def list_policies(self, cwd: Path, pr_id: int) -> list[dict[str, Any]]: ...


class SubprocessAz:
    """Runner de prod : ``az repos pr …`` via le ProcessRunner (P-07, portable). Auto-détecte
    organisation/projet/dépôt depuis la config git du dépôt (``--detect true``)."""

    def __init__(self, *, runner: ProcessRunner | None = None, timeout_s: int = 60) -> None:
        self._runner: ProcessRunner = runner or SubprocessProcessRunner()
        self._timeout_s = timeout_s

    def _az_json(self, args: list[str], cwd: Path) -> list[dict[str, Any]]:
        res = self._runner.run(
            ["az", *args, "--detect", "true", "--output", "json"], cwd=cwd, timeout_s=self._timeout_s
        )
        if res.returncode != 0:
            joined = " ".join(args)
            raise RuntimeError(f"az {joined} a échoué (code {res.returncode}) : {clip(res.stderr, 500)}")
        out = res.stdout.strip()
        return json.loads(out) if out else []

    def list_prs(self, cwd: Path) -> list[dict[str, Any]]:
        return self._az_json(["repos", "pr", "list", "--status", "active"], cwd)

    def list_policies(self, cwd: Path, pr_id: int) -> list[dict[str, Any]]:
        return self._az_json(["repos", "pr", "policy", "list", "--id", str(pr_id)], cwd)


# Statut d'évaluation de policy AzDO → conclusion iso-GitHub (cf. bad_runner._PASS).
# `notApplicable`/inconnu → non mappé (ignoré) : anti faux-positif.
_AZ_STATUS: dict[str, str] = {
    "approved": "SUCCESS",
    "rejected": "FAILURE",
    "queued": "PENDING",
    "running": "PENDING",
}


def _strip_ref(ref: str) -> str:
    """`refs/heads/feature/x` → `feature/x` (headRefName iso-GitHub)."""
    return ref.removeprefix("refs/heads/")


def _pr_web_url(pr: dict[str, Any]) -> str | None:
    repo = pr.get("repository") or {}
    web = repo.get("webUrl")
    pr_id = pr.get("pullRequestId")
    return f"{web}/pullrequest/{pr_id}" if web and pr_id is not None else None


class AzureDevOpsProvider:
    """Provider Azure DevOps : observe les PR actives via ``az repos`` et mappe vers le contrat
    commun (``headRefName`` / ``url`` / ``statusCheckRollup``).

    Les « checks » proviennent des *branch policies* **bloquantes** de la PR (build validation).
    Anti faux-positif : une PR sans policy bloquante évaluée → rollup vide → traitée comme « non
    terminée » par ``bad_runner._code_ok`` (parité avec GitHub sans statusCheck)."""

    def __init__(self, runner: AzRunner | None = None) -> None:
        self._az: AzRunner = runner or SubprocessAz()

    def list_prs(self, cwd: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for pr in self._az.list_prs(cwd):
            pr_id = pr.get("pullRequestId")
            rollup = self._rollup(cwd, pr_id) if isinstance(pr_id, int) else []
            out.append(
                {
                    "headRefName": _strip_ref(str(pr.get("sourceRefName", ""))),
                    "url": _pr_web_url(pr),
                    "statusCheckRollup": rollup,
                }
            )
        return out

    def _rollup(self, cwd: Path, pr_id: int) -> list[dict[str, Any]]:
        rollup: list[dict[str, Any]] = []
        for ev in self._az.list_policies(cwd, pr_id):
            cfg = ev.get("configuration") or {}
            if not cfg.get("isBlocking"):
                continue  # seules les policies bloquantes comptent comme "check"
            conclusion = _AZ_STATUS.get(str(ev.get("status", "")).lower())
            if conclusion is not None:
                rollup.append({"conclusion": conclusion})
        return rollup


def detect_provider(cwd: Path, *, runner: ProcessRunner | None = None) -> str:
    """Devine le provider depuis l'URL du remote ``origin`` (github par défaut/inconnu)."""
    r = runner or SubprocessProcessRunner()
    try:
        url = r.run(["git", "remote", "get-url", "origin"], cwd=cwd).stdout.strip().lower()
    except RuntimeError:
        return "github"  # git absent ou pas de remote → défaut GitHub
    if "dev.azure.com" in url or "visualstudio.com" in url:
        return "azure-devops"
    if "gitlab" in url:
        return "gitlab"
    return "github"


def resolve_git_provider(cwd: Path, *, runner: ProcessRunner | None = None) -> GitProvider:
    """Sélectionne l'impl GitProvider selon le remote (GitHub + Azure DevOps réels ; GitLab → stub)."""
    name = detect_provider(cwd, runner=runner)
    if name == "azure-devops":
        return AzureDevOpsProvider()
    if name == "gitlab":
        return UnsupportedProvider(name)
    return GitHubProvider()  # github + défaut inconnu
