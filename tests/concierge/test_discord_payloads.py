"""Pure Discord payload builders (Tasks 5.4/5.5/5.7): the decisions behind the
``/bot`` commands and the managed-channel auto-offer.

Every builder in ``concierge/discord_payloads.py`` is pure — plain values in,
plain dicts out, no discord.py, no filesystem — so these tests pin the DECISIONS
(picker options, list rows, retire choices, create-vs-bind) without any event
plumbing. The discord.py side is verified live in the Task 5.6 manual E2E.
"""

import pytest

from concierge.blueprints import CATALOG
from concierge.discord_payloads import (
    ChannelPlan,
    build_list_payload,
    build_new_bot_response,
    build_retire_choices,
    build_room_created_response,
    build_room_disband_response,
    build_room_info_response,
    build_room_member_choices,
    channel_name_for_blueprint,
    create_vs_bind,
    room_channel_name,
    validate_room_selection,
)
from concierge.routing import should_auto_offer
from concierge.settings import ConciergeSettings


def _settings(managed=(), **kw):
    return ConciergeSettings(
        enabled=True, hub_profile="concierge",
        managed_category_ids=list(managed), **kw)


BINDINGS = {
    "100": {"profile": "bot-reminders-a1b2", "blueprint": "reminders",
            "guild_id": "42"},
    "200": {"profile": "bot-reminders-a1b2", "blueprint": "reminders",
            "guild_id": "42"},
    "300": {"profile": "bot-researcher-b2c3", "blueprint": "researcher",
            "guild_id": "77"},
    # CLI-provisioned (no guild recorded): always the operator's
    "400": {"profile": "bot-notes-d4e5", "blueprint": "notes", "guild_id": None},
}


# ── /bot new picker payload (Task 5.4) ─────────────────────────────────────


def test_new_bot_response_carries_the_whole_catalog_in_order():
    payload = build_new_bot_response()
    assert payload["choices"] == [
        {"value": bp.key, "label": bp.title, "description": bp.description}
        for bp in CATALOG.values()
    ]
    assert payload["content"]  # the greeting copy is never empty


def test_new_bot_response_shows_titles_not_hermes_concepts():
    """Users never see profile/model/Hermes words in the picker copy."""
    payload = build_new_bot_response()
    blob = " ".join(c["label"] + " " + (c.get("description") or "")
                   for c in payload["choices"])
    for banned in ("Hermes", "profile", "provider", "model", "config"):
        assert banned.lower() not in blob.lower()


# ── /bot list payload (Task 5.4) ────────────────────────────────────────────


def test_list_groups_channels_per_profile():
    payload = build_list_payload(BINDINGS, None, guild_id="42")
    assert payload["empty"] is False
    # guild 42 sees reminders (2 channels) + the guild-less notes bot
    assert len(payload["lines"]) == 2
    reminders = next(l for l in payload["lines"] if "Reminder Bot" in l)
    assert "100" in reminders and "200" in reminders
    assert "300" not in reminders  # researcher is guild 77's


def test_list_scope_hides_other_guilds_bots():
    payload = build_list_payload(BINDINGS, None, guild_id="42")
    assert not any("Researcher Bot" in l for l in payload["lines"])
    # but guild 77 sees it
    payload77 = build_list_payload(BINDINGS, None, guild_id="77")
    assert any("Researcher Bot" in l for l in payload77["lines"])


def test_list_shows_guildless_bindings_to_every_guild():
    payload = build_list_payload(BINDINGS, None, guild_id="42")
    assert any("Notes Bot" in l for l in payload["lines"])


def test_list_empty_when_no_bindings():
    payload = build_list_payload({}, None, guild_id="42")
    assert payload["empty"] is True
    assert payload["lines"] == []


def test_list_routine_state_comes_from_status_fn():
    status = {
        "bot-reminders-a1b2": {
            "channels": ["100"],
            "routines": [{"name": "[bot:x] morning-agenda", "paused": False}],
        }
    }
    payload = build_list_payload(BINDINGS, status.get, guild_id="42")
    line = next(l for l in payload["lines"] if "Reminder Bot" in l)
    assert "morning-agenda" in line and "scheduled" in line
    status["bot-reminders-a1b2"]["routines"][0]["paused"] = True
    payload = build_list_payload(BINDINGS, status.get, guild_id="42")
    line = next(l for l in payload["lines"] if "Reminder Bot" in l)
    assert "paused" in line


def test_list_survives_junk_and_raising_status_fn():
    """A hand-edited registry must not crash the list; a broken status probe
    omits the routine line rather than failing the command."""
    junk = {"100": "not-a-dict", "200": {"profile": ""}, "300": {"profile": "bot-x-1a2b"}}
    payload = build_list_payload(junk, None, guild_id="42")
    assert len(payload["lines"]) == 1  # only the well-formed binding

    def _boom(_profile):
        raise RuntimeError("status blew up")

    payload = build_list_payload(BINDINGS, _boom, guild_id="42")
    assert payload["empty"] is False


# ── /bot retire choices (Task 5.4) ──────────────────────────────────────────


def test_retire_choices_offers_only_the_guilds_bots():
    choices = build_retire_choices(BINDINGS, "42")
    values = {c["value"] for c in choices}
    assert "bot-reminders-a1b2" in values
    assert "bot-researcher-b2c3" not in values  # guild 77's
    assert "bot-notes-d4e5" in values            # guild-less: operator's


def test_retire_choices_merge_channels_per_bot():
    choices = build_retire_choices(BINDINGS, "42")
    reminders = next(c for c in choices if c["value"] == "bot-reminders-a1b2")
    assert "100" in reminders["description"] and "200" in reminders["description"]


def test_retire_choices_carry_human_labels():
    choices = build_retire_choices(BINDINGS, "42")
    reminders = next(c for c in choices if c["value"] == "bot-reminders-a1b2")
    assert reminders["label"] == "Reminder Bot"


def test_retire_choices_empty_guild():
    assert build_retire_choices({}, "42") == []
    assert build_retire_choices(BINDINGS, "999") == [
        c for c in build_retire_choices(BINDINGS, "999")
    ]  # only the guild-less bot


# ── Task 5.5: should_auto_offer ────────────────────────────────────────────


def test_auto_offer_true_for_managed_category():
    assert should_auto_offer("555", ["555"]) is True


def test_auto_offer_false_for_unmanaged_or_uncategorized():
    assert should_auto_offer("556", ["555"]) is False
    assert should_auto_offer(None, ["555"]) is False  # no category at all


def test_auto_offer_accepts_settings_object_and_int_ids():
    """The adapter hands its ConciergeSettings straight through; YAML int ids
    normalize like registry keys."""
    settings = _settings(managed=["555"])
    assert should_auto_offer("555", settings) is True
    assert should_auto_offer(555, settings) is True
    assert should_auto_offer("556", settings) is False


def test_auto_offer_empty_managed_list_is_false():
    assert should_auto_offer("555", []) is False
    assert should_auto_offer("555", _settings(managed=[])) is False


# ── Task 5.7: create_vs_bind ───────────────────────────────────────────────


def test_create_when_new_bot_flow_in_managed_category():
    """/bot new in bot country: the hub creates a fresh #bot-<name> channel —
    "one message from the user, channel appears, bot says hi"."""
    plan = create_vs_bind(
        can_create_channels=True, category_id="555",
        managed_category_ids=["555"], channel_id="100", prefer_create=True)
    assert plan.mode == "create"


def test_bind_invoking_channel_when_one_exists():
    """The auto-offer picker (no prefer_create) binds the channel it was
    posted in — that channel is the user's fresh canvas."""
    plan = create_vs_bind(
        can_create_channels=True, category_id="555",
        managed_category_ids=["555"], channel_id="100")
    assert plan.mode == "bind"


def test_bind_when_creation_denied():
    """THE fallback rule: a permissions denial must never fail provisioning —
    the invoking channel is bound instead."""
    plan = create_vs_bind(
        can_create_channels=False, category_id="555",
        managed_category_ids=["555"], channel_id="100", prefer_create=True)
    assert plan.mode == "bind"
    assert "denied" in plan.reason


def test_bind_when_creation_denied_and_no_channel():
    """Denied + nothing to bind: still a bind-mode plan (the caller answers
    with guidance, never a crash)."""
    plan = create_vs_bind(
        can_create_channels=False, category_id="555",
        managed_category_ids=["555"], channel_id=None, prefer_create=True)
    assert plan.mode == "bind"
    assert plan.channel_name is None


def test_bind_when_category_not_managed():
    """/bot new outside bot country: the invoking channel is the destination
    (managed geography is what makes creation the plan)."""
    plan = create_vs_bind(
        can_create_channels=True, category_id="556",
        managed_category_ids=["555"], channel_id="100", prefer_create=True)
    assert plan.mode == "bind"


def test_create_vs_bind_accepts_settings_object_and_int_ids():
    settings = _settings(managed=["555"])
    plan = create_vs_bind(
        can_create_channels=True, category_id=555,
        managed_category_ids=settings, channel_id="100", prefer_create=True)
    assert plan.mode == "create"


def test_channel_name_is_a_clean_slug():
    assert channel_name_for_blueprint("reminders") == "bot-reminder"
    assert channel_name_for_blueprint("email_triage") == "bot-email-triage"
    assert channel_name_for_blueprint("notes") == "bot-notes"


def test_channel_name_unknown_blueprint_degrades_to_key():
    name = channel_name_for_blueprint("no_such_bp")
    assert name == "bot-no-such-bp"  # punctuation → hyphen, capped


def test_channel_plan_is_a_value_object():
    plan = ChannelPlan("bind")
    assert plan.mode == "bind"
    assert plan.channel_name is None
    assert "bind" in repr(plan)


# ── Task 7.4: /room payload builders ───────────────────────────────────────


def test_room_choices_offers_only_the_guilds_bound_bots():
    """Guild scoping (plan: '2–6 of this guild's bound bots'): a bot bound in
    another guild is not this guild's to room; CLI-provisioned guildless
    bindings are the operator's and stay eligible (retire/list parity)."""
    choices = build_room_member_choices(BINDINGS, "42")
    values = [c["value"] for c in choices]
    assert "bot-reminders-a1b2" in values       # guild 42's
    assert "bot-notes-d4e5" in values           # guildless: the operator's
    assert "bot-researcher-b2c3" not in values  # guild 77's
    # human labels from the blueprint titles, never Hermes concepts
    labels = " ".join(c["label"] for c in choices)
    assert "Reminder Bot" in labels
    for banned in ("Hermes", "profile", "provider", "model"):
        assert banned.lower() not in labels.lower()


def test_room_choices_dedupe_per_profile_across_channels():
    """A bot bound in two of this guild's channels is ONE choice (one persona
    per room member), and every choice carries a usable description."""
    choices = build_room_member_choices(BINDINGS, "42")
    values = [c["value"] for c in choices]
    assert len(values) == len(set(values))
    by_profile = {c["value"]: c for c in choices}
    assert "100" in by_profile["bot-reminders-a1b2"]["description"]
    assert "200" in by_profile["bot-reminders-a1b2"]["description"]


def test_room_choices_empty_guild():
    assert build_room_member_choices(BINDINGS, "999") == [
        {"value": "bot-notes-d4e5", "label": "Notes Bot",
         "description": "Channels: 400"}]  # only the guildless operator bot


def test_room_selection_of_two_known_bots_is_valid():
    choices = build_room_member_choices(BINDINGS, "42")
    assert validate_room_selection(
        ["bot-reminders-a1b2", "bot-notes-d4e5"], choices) is None
    # six is the cap and still valid
    six = [f"bot-{i}" for i in range(6)]
    assert validate_room_selection(six, [
        {"value": v, "label": v, "description": ""} for v in six]) is None


def test_room_selection_refuses_fewer_than_two():
    """Cap enforcement, lower bound (the plan's 2–6): a one-bot 'room' is
    just a single-bot channel — /bot new is that flow."""
    choices = build_room_member_choices(BINDINGS, "42")
    error = validate_room_selection(["bot-reminders-a1b2"], choices)
    assert error is not None and "two" in error


def test_room_selection_refuses_more_than_six():
    seven = [f"bot-{i}" for i in range(7)]
    error = validate_room_selection(seven, [
        {"value": v, "label": v, "description": ""} for v in seven])
    assert error is not None and "6" in error


def test_room_selection_refuses_unbound_bots():
    """Refusal of unbound bots: a name that isn't one of this guild's bound
    bots never enters a room (it might not even be a profile)."""
    choices = build_room_member_choices(BINDINGS, "42")
    error = validate_room_selection(
        ["bot-reminders-a1b2", "bot-stranger-9999"], choices)
    assert error is not None and "bot-stranger-9999" in error
    # a OTHER-GUILD bot is equally refused (guild scoping)
    error = validate_room_selection(
        ["bot-reminders-a1b2", "bot-researcher-b2c3"], choices)
    assert error is not None and "bot-researcher-b2c3" in error


def test_room_selection_dedupes_and_keeps_pick_order():
    choices = build_room_member_choices(BINDINGS, "42")
    # duplicates collapse (first occurrence wins); the picker's order is the
    # stored member order, so it must survive validation verbatim
    assert validate_room_selection(
        ["bot-notes-d4e5", "bot-reminders-a1b2", "bot-notes-d4e5"],
        choices) is None


def test_room_channel_name_slugs_the_topic():
    assert room_channel_name("Trip Planning") == "room-trip-planning"
    assert room_channel_name("Q4 reviews & prep!") == "room-q4-reviews-prep"
    assert room_channel_name("  ") == "room"      # blank topic: plain room
    assert room_channel_name(None) == "room"
    assert len(room_channel_name("x" * 500)) <= 90  # Discord name cap


def test_room_created_response_names_every_member():
    content = build_room_created_response(["Reminder Bot", "Scheduler Bot"])
    assert "Reminder Bot" in content and "Scheduler Bot" in content
    assert "room" in content.lower()


def test_room_disband_response_names_members_and_keeps_the_channel():
    content = build_room_disband_response(["Reminder Bot", "Scheduler Bot"])
    assert "Reminder Bot" in content and "Scheduler Bot" in content
    assert "channel" in content  # the channel stays; the bots leave it


def test_room_info_response_lists_members_with_the_cap():
    info = build_room_info_response(["Reminder Bot", "Scheduler Bot"])
    assert "Reminder Bot" in info and "Scheduler Bot" in info
    assert "2/6" in info  # occupancy against MAX_ROOM_MEMBERS
