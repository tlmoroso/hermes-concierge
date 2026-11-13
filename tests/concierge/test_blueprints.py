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
    """The picker payload is CATALOG minus the orchestrator (see
    blueprint_choices: the orchestrator is auto-provisioned per user, the
    picker offers workers only), order preserved."""
    workers = [bp for key, bp in CATALOG.items() if key != "orchestrator"]
    choices = blueprint_choices()
    assert all({"value": bp.key, "label": bp.title, "description": bp.description}
               == c for c, bp in zip(choices, workers, strict=True))


def test_orchestrator_blueprint_matches_locked_design():
    """PLAN.md decision 7: key/title/description/model_preset/toolsets/skills/
    memory_provider/connects/routine — plus the answers-alone constraint
    (no terminal, no file: the orchestrator delegates hands-on work, it
    never performs it)."""
    bp = get_blueprint("orchestrator")
    assert bp.title == "Orchestrator"
    assert bp.description == (
        "Your front door: triages requests, hands work to your helper bots, "
        "and answers with the result."
    )
    assert bp.model_preset == "core"
    assert bp.toolsets == ("web",)  # a real bundle (catalog-wide test enforces that)
    assert "terminal" not in bp.toolsets
    assert "file" not in bp.toolsets
    assert bp.skills == ()
    assert bp.memory_provider == "honcho"
    assert bp.connects == ()
    assert bp.routine is None  # deferred with provision (T3); avoids inventing behavior


def test_orchestrator_soul_carries_dispatch_protocol_and_worker_catalog():
    """The SOUL is the contract the inbound wrap (T5) parses: the directive
    syntax must appear EXACTLY, the fail-closed triage rule and the
    ENTIRE-reply constraint must appear verbatim (the future T5 dispatch
    regex depends on both), and every non-orchestrator catalog key+title
    must be listed as a dispatch target. The persona never names the
    platform."""
    soul = get_blueprint("orchestrator").soul
    assert "@dispatch <blueprint-key> <task for the worker>" in soul
    # Drift-critical sentences, asserted verbatim so deleting either one
    # fails the suite. The fail-closed triage rule — the unsure case must
    # answer the user directly, never guess a dispatch:
    assert (
        "When you are unsure whether to dispatch, answer the user directly."
        in soul
    )
    # The no-prose constraint the T5 dispatch regex depends on — the reply
    # is the directive line alone:
    assert (
        "your ENTIRE reply must be exactly one line, nothing before or after:"
        in soul
    )
    # Triage tie-breaker: research-flavored requests always dispatch.
    assert (
        "Any request that needs search, sources, or citations goes to the "
        "researcher." in soul
    )
    assert "Hermes" not in soul
    for key, bp in CATALOG.items():
        if key == "orchestrator":
            continue
        # The exact rendered "key — title" line, not a bare substring: a
        # future key that is a substring of another ("email" vs
        # "email_triage") must not pass spuriously.
        assert f"  {key} — {bp.title}" in soul, (
            f"worker catalog line {key!r} — {bp.title!r} missing or malformed"
        )


def test_orchestrator_is_last_in_catalog_and_absent_from_picker():
    """The orchestrator is LAST IN CATALOG (CLI + programmatic dispatch use
    it there) but ABSENT FROM the picker: the provisioning model
    auto-provisions a per-user orchestrator on first contact — the
    orchestrator owns the user, never a channel — so a picker-selected
    orchestrator would be a channel-bound v1-style bot, contradicting that
    model. The dedicated exclusion assertions below pin this ON PURPOSE:
    absence must never happen by accident (a zip mismatch, a filter typo)."""
    assert list(CATALOG)[-1] == "orchestrator"
    values = [c["value"] for c in blueprint_choices()]
    assert "orchestrator" not in values
    assert values == [key for key in CATALOG if key != "orchestrator"]
