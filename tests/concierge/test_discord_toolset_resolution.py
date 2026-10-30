"""Regression test for the live concierge toolset-leakage bug (bot-reminders-94d3).

A provisioned bot's config pinned the blueprint allowlist only on the
TOP-LEVEL ``toolsets`` key, but the gateway's Discord lane resolves a turn's
toolsets via ``hermes_cli.tools_config._get_platform_tools(config,
'discord')`` (gateway/run_turn.py::_resolve_enabled_toolsets_for_source):
that resolver reads ``platform_toolsets.discord`` and — when the key is
absent — falls back to the discord platform's DEFAULT COMPOSITE, a ~17-set
bundle (terminal, browser, file, delegation, …). Live evidence: a reminders
bot (config ``toolsets: [todo]``) executed ``terminal`` and ``tool_search``
and hit an output-length refusal — 19.7s for a one-line reminder.

The fix provisions ``platform_toolsets.discord`` from the blueprint. These
tests run the REAL resolver against the REAL provisioned bot config and
assert it yields exactly the blueprint's toolsets — they fail RED on a
provision that seeds only the top-level key.

The ``notes`` blueprint is deliberately absent from the parametrization: its
``hermes-cli`` entry is a COMPOSITE name, which ``_explicit_toolsets``
expands back to the full configurable set — a separate blueprint-catalog
issue (flagged, not fixed here; changing blueprints beyond the reminders
cronjob need is out of scope for this bug).
"""

from pathlib import Path

import pytest

from concierge import provision
from hermes_cli.tools_config import _get_platform_tools
from hermes_yaml import safe_load as _yaml_safe_load

from concierge.blueprints import CATALOG

# Blueprints whose toolset entries are plain bundle names — the resolver
# yields them back exactly once platform_toolsets.discord is seeded.
_EXACT_BLUEPRINTS = [key for key in CATALOG if key != "notes"]


@pytest.fixture
def concierge_home(tmp_path, monkeypatch):
    # Same nested shape as tests/concierge/test_provision.py (load-bearing:
    # create_profile's profile-root resolution and Path.home-anchored call
    # sites must both stay inside the test tmp tree).
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _bot_config(home: Path, profile: str) -> dict:
    cfg_path = home / "profiles" / profile / "config.yaml"
    return _yaml_safe_load(cfg_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("key", _EXACT_BLUEPRINTS)
def test_provisioned_bot_discord_lane_yields_exactly_blueprint_toolsets(
        concierge_home, key):
    """The regression test for the live bug: the resolver the Discord lane
    actually calls must yield EXACTLY the blueprint's allowlist for a real
    provisioned bot's config.yaml — not the ~17-set composite fallback."""
    bp = CATALOG[key]
    binding = provision.create_bot(
        key, channel_id="123", guild_id="42",
        source_skills_dir=concierge_home / "skills",
    )
    cfg = _bot_config(concierge_home, binding["profile"])
    resolved = _get_platform_tools(cfg, "discord")
    assert resolved == set(bp.toolsets), (
        f"{key}: Discord-lane toolsets must be exactly the blueprint "
        f"allowlist {sorted(bp.toolsets)}, got {sorted(resolved)}"
    )


def test_unseeded_discord_lane_leaks_the_platform_composite(concierge_home):
    """The inverse lock: WITHOUT ``platform_toolsets.discord``, the same
    resolver widens the reminders bot to the platform composite (the live
    bug's mechanism). Proves the seeded key — not some unrelated resolver
    change — is what keeps the lane narrow."""
    binding = provision.create_bot(
        "reminders", channel_id="123", guild_id="42",
        source_skills_dir=concierge_home / "skills",
    )
    cfg = _bot_config(concierge_home, binding["profile"])
    leaked = _get_platform_tools(dict(cfg, platform_toolsets={}), "discord")
    assert "terminal" in leaked and "browser" in leaked
    assert len(leaked) > len(CATALOG["reminders"].toolsets)
