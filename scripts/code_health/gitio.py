"""Git plumbing: which files changed between two trees, and their contents on either side.

A tree is a revision (``"abc123"``) or ``None`` for the working tree. Both sides are read
through the same functions so base and head are measured identically.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path) -> Path:
    return Path(git(start, "rev-parse", "--show-toplevel").strip())


def resolve_rev(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def resolve_tree(repo: Path, rev: str) -> str:
    """A head may be any tree-ish: a commit, or the index's tree from ``git write-tree``."""
    return git(repo, "rev-parse", "--verify", f"{rev}^{{tree}}").strip()


def commit_or_none(repo: Path, rev: str) -> str | None:
    """``rev`` as a commit sha, or None when it does not name a commit (a tree, a missing ref)."""
    out = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False)
    return out.strip() or None


# The reference a branch is measured against: the FORK's own main, never upstream.
#
# ``origin`` is upstream NousResearch, whose main predates this fork and contains none of the
# concierge feature, so measuring against it counts long-merged fork code as "new" -- that is
# exactly how the ratchet reported 119 blocking findings for code already on fork main. Do not
# add ``origin/main`` (or any upstream ref) back.
#
# ``personal`` is the fork's *snapshot-artifact* repo, not a history mirror (see
# concierge/docs/repo-hygiene.md): its commits are tree snapshots with their own short chain,
# not ancestors of this tree, so it is not a usable merge-base either. Local ``main`` is the
# fork's real main line and is the correct baseline.
#
# Override for a different layout with HERMES_CODE_HEALTH_BASE_REFS (comma-separated,
# ordered), or pass --base explicitly (CI).
DEFAULT_BASE_REFS = ("main",)
_BASE_REFS_ENV = "HERMES_CODE_HEALTH_BASE_REFS"


def _base_ref_candidates() -> tuple[str, ...]:
    override = os.environ.get(_BASE_REFS_ENV, "").strip()
    if override:
        return tuple(ref.strip() for ref in override.split(",") if ref.strip())
    return DEFAULT_BASE_REFS


def default_base(repo: Path, tip: str = "HEAD") -> str:
    """Newest common ancestor of ``tip`` with the fork's own ``main`` lineage.

    Local runs compare a branch against fork main, never upstream ``origin/main`` (a private
    fork's features are absent there). Of the resolvable candidate refs we return the most
    recent common ancestor, so a stale fork-remote snapshot or upstream cannot drag the
    baseline back past code already on fork main. CI passes ``--base`` explicitly. Candidate
    refs come from HERMES_CODE_HEALTH_BASE_REFS or DEFAULT_BASE_REFS.
    """
    refs = _base_ref_candidates()
    bases: list[str] = []
    for ref in refs:
        if not git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip():
            continue
        mb = git(repo, "merge-base", tip, ref, check=False).strip()
        if mb:
            bases.append(mb)
    if not bases:
        raise RuntimeError(f"no merge-base of {tip} with any of {', '.join(refs)}; pass --base")
    # Every merge-base is an ancestor of ``tip``; the newest is the one with the fewest commits
    # in ``mb..tip``. That is the fork-main point the branch actually forked from.
    return min(set(bases), key=lambda mb: int(
        git(repo, "rev-list", "--count", f"{mb}..{tip}", check=False).strip() or 0))


@dataclass(frozen=True)
class Change:
    status: str  # A, M, D, R
    old: str | None
    new: str | None


def _parse_name_status(out: str) -> list[Change]:
    fields = out.split("\0")
    changes: list[Change] = []
    i = 0
    while i < len(fields) and fields[i]:
        code = fields[i][0]
        if code in "RC":
            old, new = fields[i + 1], fields[i + 2]
            changes.append(Change("R" if code == "R" else "A", old if code == "R" else None, new))
            i += 3
            continue
        path = fields[i + 1]
        if code == "D":
            changes.append(Change("D", path, None))
        elif code == "A":
            changes.append(Change("A", None, path))
        else:
            changes.append(Change("M", path, path))
        i += 2
    return changes


def changed_files(repo: Path, base: str, head: str | None) -> list[Change]:
    args = ["diff", "--name-status", "-M", "-z", "--no-color", base]
    if head is not None:
        args.append(head)
    changes = _parse_name_status(git(repo, *args))
    if head is None:
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        changes.extend(Change("A", None, p) for p in untracked.split("\0") if p)
    return changes


def read_file(repo: Path, tree: str | None, path: str) -> str | None:
    if tree is None:
        try:
            return (repo / path).read_text(encoding="utf-8-sig", errors="replace")
        except (FileNotFoundError, IsADirectoryError):
            return None
    proc = subprocess.run(
        ["git", "show", f"{tree}:{path}"],
        cwd=repo,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return None
    # Same decoding as the working-tree read: a BOM must not make the two sides disagree.
    return proc.stdout.decode("utf-8-sig", errors="replace")


def tracked_files(repo: Path, tree: str | None) -> list[str]:
    if tree is None:
        return [p for p in git(repo, "ls-files", "-z").split("\0") if p]
    return [p for p in git(repo, "ls-tree", "-r", "--name-only", "-z", tree).split("\0") if p]


def known_env_names(repo: Path, tree: str | None) -> set[str]:
    """Every HERMES_* literal in non-test Python of a tree (the HX002 allow set)."""
    args = ["grep", "-h", "-o", "-I", "-E", r"HERMES_[A-Z0-9_]+"]
    if tree is not None:
        args.append(tree)
    args += ["--", "*.py", ":!tests/**", ":!**/tests/**"]
    out = git(repo, *args, check=False)
    names = set()
    for line in out.splitlines():
        # `git grep <tree>` prefixes "<tree>:" even with -h on some versions.
        names.add(line.rsplit(":", 1)[-1])
    return names
