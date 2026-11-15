"""The code-health ratchet's baseline resolution (scripts/code_health/gitio.py::default_base).

A private fork's concierge feature is absent from upstream ``origin/main``; the ratchet must
compare a branch against the fork's own ``main``, never upstream. ``personal`` is a
snapshot-artifact repo (see concierge/docs/repo-hygiene.md), so it is not a usable merge-base
either -- local ``main`` is the fork's real main line. When several refs are configured the
NEWEST common ancestor wins, so a stale remote cannot drag the baseline back past code already
on fork main. Hermetic: a throwaway git repo, no network, no ruff.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.code_health import gitio

_ENV = "HERMES_CODE_HEALTH_BASE_REFS"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True, timeout=60).stdout.strip()


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _init(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def test_measures_against_fork_main_and_never_upstream(tmp_path, monkeypatch):
    """The branch is judged against the fork's own main, and upstream is never consulted.

    Upstream ``origin/main`` lacks the fork's feature entirely, so measuring against it counts
    long-merged fork code as "new". ``personal/main`` is a snapshot artifact, not a history
    ancestor. Both exist here and both are wrong answers; only local ``main`` is right.
    """
    monkeypatch.delenv(_ENV, raising=False)
    repo = _init(tmp_path)
    upstream = _commit(repo, "upstream c0")
    _commit(repo, "c1")
    fork_main = _commit(repo, "fork main c2")          # local main: the fork's real line
    _git(repo, "checkout", "-q", "-b", "feature", fork_main)
    head = _commit(repo, "tip")
    _git(repo, "update-ref", "refs/remotes/origin/main", upstream)
    _git(repo, "update-ref", "refs/remotes/personal/main", upstream)

    # The fork's main point wins; upstream and the snapshot repo are ignored.
    assert gitio.default_base(repo, head) == fork_main
    assert gitio.default_base(repo, head) != upstream

    # Policy invariant: the default candidate set carries no upstream (NousResearch) ref.
    assert not any("origin" in ref for ref in gitio.DEFAULT_BASE_REFS)

    # Fully configurable: forcing the upstream ref reproduces the upstream comparison.
    monkeypatch.setenv(_ENV, "origin/main")
    assert gitio._base_ref_candidates() == ("origin/main",)
    assert gitio.default_base(repo, head) == upstream


def test_stale_fork_snapshot_does_not_drag_baseline_back(tmp_path, monkeypatch):
    """This fork's real topology: personal/main is an old snapshot lineage whose merge-base is
    OLDER than the local fork main divergence. The newest common ancestor must still win."""
    monkeypatch.delenv(_ENV, raising=False)
    repo = _init(tmp_path)
    upstream = _commit(repo, "c0")
    fork_main = _commit(repo, "fork main c1")          # branch forks from here
    _git(repo, "checkout", "-q", "-b", "feature", fork_main)
    head = _commit(repo, "tip")
    # A remote snapshot that diverged from c0 (sibling of c1): its merge-base with HEAD is c0.
    _git(repo, "checkout", "-q", "-b", "snapshot", upstream)
    snapshot = _commit(repo, "stale snapshot")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "update-ref", "refs/remotes/personal/main", snapshot)
    _git(repo, "update-ref", "refs/remotes/origin/main", upstream)
    # 'main' stays at fork_main (c1); feature is checked out.

    assert gitio.default_base(repo, head) == fork_main   # newest common ancestor
    assert gitio.default_base(repo, head) != upstream    # not the stale snapshot's c0
