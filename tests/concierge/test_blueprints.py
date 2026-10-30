"""Blueprint catalog contracts: stable keys, valid presets, complete blueprints."""
import pytest
from concierge.blueprints import CATALOG, MODEL_PRESETS, get_blueprint, blueprint_choices


def test_catalog_has_the_five_launch_blueprints():
    for key in ("reminders", "scheduler", "researcher", "email_triage", "notes"):
        assert key in CATALOG


def test_every_blueprint_pins_a_known_preset_and_minimal_toolsets():
    for bp in CATALOG.values():
        assert bp.model_preset in MODEL_PRESETS
        # ≤3, not ≤2: CLI-skill bots (email_triage, scheduler) need terminal
        # for the CLI their skill drives plus cronjob for their routine. The
        # invariant's purpose is minimal contexts, not a hard two.
        assert 1 <= len(bp.toolsets) <= 3, f"{bp.key} must stay minimal (context bloat)"
        assert bp.soul and "{name}" in bp.soul
        bp.soul.format(name="Test Bot")  # renders cleanly - no stray braces that break provisioning
        if bp.routine:
            assert bp.routine.schedule and bp.routine.prompt


def test_cli_skill_blueprints_carry_the_bundles_their_skills_drive():
    """Live-flagged gaps. notes drives the obsidian skill over the FILE tools
    (read_file/write_file/patch/search_files) — its old ('hermes-cli',)
    entry is a COMPOSITE (toolsets.py::_bundle) that re-expands to the full
    ~17-set default range (hermes_cli/tools_config.py::_explicit_toolsets),
    not the file bundle. email_triage and scheduler drive CLI skills
    (himalaya, google-workspace's gws): a CLI runs through terminal, and
    their routines need cronjob."""
    assert get_blueprint("notes").toolsets == ("file",)
    assert get_blueprint("email_triage").toolsets == ("todo", "cronjob", "terminal")
    assert get_blueprint("scheduler").toolsets == ("todo", "cronjob", "terminal")


def test_every_blueprint_toolset_is_a_real_non_composite_bundle():
    """Every toolset entry names a real toolsets.py bundle — never a
    ``hermes-*`` composite (those re-expand to the full default tool range
    and defeat the blueprint allowlist)."""
    from toolsets import TOOLSETS

    for bp in CATALOG.values():
        for ts in bp.toolsets:
            assert ts in TOOLSETS, f"{bp.key}: unknown toolset {ts!r}"
            assert not ts.startswith("hermes-"), (
                f"{bp.key}: {ts!r} is a composite bundle — "
                "it re-expands to the full default tool range")


def test_notes_soul_mentions_conventions():
    assert "CONVENTIONS.md" in get_blueprint("notes").soul


def test_get_blueprint_unknown_key_raises():
    with pytest.raises(KeyError, match="Known:"):
        get_blueprint("nope")


def test_blueprint_choices_is_renderable():
    choices = blueprint_choices()
    assert all({"value": bp.key, "label": bp.title, "description": bp.description}
               == c for c, bp in zip(choices, CATALOG.values(), strict=True))
