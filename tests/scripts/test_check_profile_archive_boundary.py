"""Behavioral tests for the profile archive CI guard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "ci"
    / "check_profile_archive_boundary.py"
)


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_root_passes(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0
    assert "No profile export archives" in result.stdout


def test_root_profile_archive_fails_without_printing_contents(tmp_path):
    default_archive = tmp_path / "default.tar.gz"
    alternate_archive = tmp_path / "backup.TGZ"
    default_archive.write_bytes(b"profile webhook secret must never be printed")
    alternate_archive.write_bytes(b"another archive")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "default.tar.gz" in result.stdout
    assert "backup.TGZ" in result.stdout
    assert "profile webhook secret" not in result.stdout
    assert "another archive" not in result.stdout


def test_nested_profile_archive_is_also_rejected(tmp_path):
    nested = tmp_path / "fixtures"
    nested.mkdir()
    (nested / "fixture.tar.gz").write_bytes(b"test fixture")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "fixtures/fixture.tar.gz" in result.stdout


def test_regenerable_build_cache_archives_are_ignored(tmp_path):
    """Public, regenerable build inputs under .cache/.build are not profile exports.

    The desktop/electron-builder bundler drops .tar.gz/.tgz downloads into
    .cache/desktop-inputs (and works out of .build/desktop-job).  Those roots are already
    excluded by .gitignore and .dockerignore; their suffix-only names must not be mistaken for
    a secret-bearing profile export.
    """
    cache = tmp_path / ".cache" / "desktop-inputs" / "packager" / "builder"
    cache.mkdir(parents=True)
    (cache / "7zip-darwin-arm64.tar.gz").write_bytes(b"public build input")
    (cache / "icons-bundle.tgz").write_bytes(b"public build input")
    build = tmp_path / ".build" / "desktop-job"
    build.mkdir(parents=True)
    (build / "bundle.tar.gz").write_bytes(b"public build input")

    result = _run(tmp_path)

    assert result.returncode == 0
    assert "No profile export archives" in result.stdout


def test_real_export_still_blocked_when_build_cache_present(tmp_path):
    """Skipping regenerable caches must not disable the guard for a genuine export."""
    cache = tmp_path / ".cache" / "desktop-inputs"
    cache.mkdir(parents=True)
    (cache / "7zip-darwin-arm64.tar.gz").write_bytes(b"public build input")
    export = tmp_path / "default-20260823-120000.tar.gz"
    export.write_bytes(b"profile webhook secret must never be printed")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "default-20260823-120000.tar.gz" in result.stdout
    assert "7zip-darwin-arm64.tar.gz" not in result.stdout
    assert "profile webhook secret" not in result.stdout
