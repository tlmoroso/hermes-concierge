"""Blueprint catalog contracts: stable keys, valid presets, complete blueprints."""
import pytest
from concierge.blueprints import CATALOG, MODEL_PRESETS, get_blueprint, blueprint_choices


def test_catalog_has_the_five_launch_blueprints():
    for key in ("reminders", "scheduler", "researcher", "email_triage", "notes"):
        assert key in CATALOG


def test_every_blueprint_pins_a_known_preset_and_one_or_two_toolsets():
    for bp in CATALOG.values():
        assert bp.model_preset in MODEL_PRESETS
        assert 1 <= len(bp.toolsets) <= 2, f"{bp.key} must stay minimal (context bloat)"
        assert bp.soul and "{name}" in bp.soul
        bp.soul.format(name="Test Bot")  # renders cleanly - no stray braces that break provisioning
        if bp.routine:
            assert bp.routine.schedule and bp.routine.prompt


def test_get_blueprint_unknown_key_raises():
    with pytest.raises(KeyError, match="Known:"):
        get_blueprint("nope")


def test_blueprint_choices_is_renderable():
    choices = blueprint_choices()
    assert all({"value": bp.key, "label": bp.title, "description": bp.description}
               == c for c, bp in zip(choices, CATALOG.values(), strict=True))
