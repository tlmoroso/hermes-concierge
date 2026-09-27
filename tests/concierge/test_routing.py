"""Routing decision contracts (Task 5.2): the pure channel->profile verdict.

``resolve_target_profile`` is the single decision function behind per-channel
bot routing. It must stay pure: plain-value inputs, no filesystem, no gateway
imports — the registry read happens in the adapter seam (Task 5.3), which passes
this function its mapping. Every contract here is behavioral, not a snapshot.
"""

import pytest

from concierge.routing import (
    RoutingVerdict,
    resolve_room_targets,
    resolve_target_profile,
    should_auto_offer,
)

# Registry shape from concierge/registry.py (bind_channel's stored entry).
BINDINGS = {
    "100": {"profile": "bot-reminders-a1b2", "blueprint": "reminders"},
    "200": {"profile": "bot-researcher-b2c3", "blueprint": "researcher"},
}


def test_bound_channel_routes_to_its_bot():
    verdict = resolve_target_profile(BINDINGS, channel_id="100")
    assert verdict.profile == "bot-reminders-a1b2"
    assert verdict.binding is BINDINGS["100"]
    assert verdict.routed is True  # re-homes away from the fallback


def test_unbound_channel_falls_back():
    verdict = resolve_target_profile(BINDINGS, channel_id="999")
    assert verdict.profile is None
    assert verdict.binding is None
    assert verdict.routed is False


def test_fallback_profile_is_returned_verbatim_when_unbound():
    """``fallback_profile`` is the hub/active profile name: the unbound verdict
    carries it so the adapter seam can stamp it unchanged on the source."""
    verdict = resolve_target_profile(BINDINGS, channel_id="999", fallback_profile="hub")
    assert verdict.profile == "hub"
    assert verdict.routed is False  # hub == fallback: no re-home


def test_thread_inherits_the_parent_channel_bot():
    """A Discord thread/forum post runs as its parent channel's bot — the
    thread id is never a registry key, the parent's binding covers it."""
    verdict = resolve_target_profile(
        BINDINGS, channel_id="thread-555", parent_channel_id="200")
    assert verdict.profile == "bot-researcher-b2c3"
    assert verdict.routed is True


def test_direct_channel_binding_beats_parent():
    """Order is channel-then-parent: a binding keyed by the thread's own id
    wins over the parent's (specificity mirrors ProfileRoute.matches)."""
    bindings = {**BINDINGS, "thread-555": {"profile": "bot-notes-c4d5"}}
    verdict = resolve_target_profile(
        bindings, channel_id="thread-555", parent_channel_id="200")
    assert verdict.profile == "bot-notes-c4d5"


def test_bot_author_never_routes_in_a_bound_channel():
    """Anti-loop: a bot's own message in its bound channel must degrade to the
    fallback — routing it to its own profile would make the bot answer itself.
    Admission (allow_bots: none) drops these in production; this is the
    routing-side half of the twice-enforced guarantee."""
    verdict = resolve_target_profile(BINDINGS, channel_id="100", author_is_bot=True)
    assert verdict.profile is None
    assert verdict.binding is None
    assert verdict.routed is False


def test_bot_author_in_unbound_channel_still_falls_back():
    verdict = resolve_target_profile(BINDINGS, channel_id="999", author_is_bot=True,
                                     fallback_profile="hub")
    assert verdict.profile == "hub"
    assert verdict.routed is False


def test_int_channel_id_matches_string_registry_key():
    """Inbound ids can arrive int-shaped (YAML loaders, test doubles); the
    decision must not silently miss a binding over a str/int mismatch."""
    verdict = resolve_target_profile(BINDINGS, channel_id=100)
    assert verdict.profile == "bot-reminders-a1b2"


def test_blank_profile_in_binding_is_skipped_not_fatal():
    """A hand-edited registry with a junk binding must not crash routing nor
    route to a blank profile; the channel behaves as unbound."""
    bindings = {"100": {"profile": "   "}, "200": {"profile": ""}, "300": {}}
    for cid in ("100", "200", "300"):
        verdict = resolve_target_profile(bindings, channel_id=cid)
        assert verdict.profile is None
        assert verdict.routed is False


def test_non_mapping_binding_values_do_not_crash():
    verdict = resolve_target_profile({"100": "junk"}, channel_id="100")
    assert verdict.profile is None


def test_none_channel_id_is_unbound():
    verdict = resolve_target_profile(BINDINGS, channel_id=None)
    assert verdict.profile is None
    assert verdict.routed is False


def test_empty_registry_is_unbound():
    verdict = resolve_target_profile({}, channel_id="100")
    assert verdict.profile is None


def test_bindings_mapping_is_not_mutated():
    snapshot = {k: dict(v) for k, v in BINDINGS.items()}
    resolve_target_profile(BINDINGS, channel_id="100", parent_channel_id="200")
    assert BINDINGS == snapshot


def test_verdict_repr_names_profile_and_routed():
    """Debugging aid: a repr that hides the verdict's two load-bearing facts is
    a silent log line; keep them named."""
    text = repr(resolve_target_profile(BINDINGS, channel_id="100"))
    assert "bot-reminders-a1b2" in text
    assert "routed=True" in text


def test_verdict_is_a_plain_value_not_a_promise():
    """The verdict must be constructible and inspectable without the resolver
    (the adapter seam stores it on the event for later logging)."""
    verdict = RoutingVerdict("p", {"profile": "p"}, None)
    assert verdict.profile == "p"
    assert verdict.routed is True
    with pytest.raises(AttributeError):
        getattr(verdict, "no_such_attribute")  # slots keep the value object closed


# ── Task 5.5: the managed-category auto-offer decision ─────────────────────


def test_auto_offer_managed_category_is_true():
    assert should_auto_offer("555", ["555", "556"]) is True


def test_auto_offer_unmanaged_category_is_false():
    assert should_auto_offer("900", ["555", "556"]) is False


def test_auto_offer_uncategorized_channel_is_false():
    """A channel with no category (or a non-channel create event) never
    triggers the picker — managed geography is the whole decision."""
    assert should_auto_offer(None, ["555"]) is False


def test_auto_offer_normalizes_int_ids():
    """YAML configs deliver int ids; inbound category ids can be int-shaped
    (same normalization contract as registry keys)."""
    assert should_auto_offer(555, [555]) is True
    assert should_auto_offer("555", [555]) is True
    assert should_auto_offer(555, ["555"]) is True


def test_auto_offer_accepts_the_settings_object():
    """The adapter hands its frozen ConciergeSettings straight through — the
    decision duck-types anything with ``managed_category_ids``."""
    from concierge.settings import ConciergeSettings

    settings = ConciergeSettings(
        enabled=True, hub_profile="concierge", managed_category_ids=["555"])
    assert should_auto_offer("555", settings) is True
    assert should_auto_offer("900", settings) is False


def test_auto_offer_empty_or_falsy_managed_list_is_false():
    assert should_auto_offer("555", []) is False
    assert should_auto_offer("555", None) is False


# ── Task 7.2: room fan-out (resolve_room_targets) ─────────────────────────
#
# A room is one channel with several member profiles; this decision says
# WHICH members run one inbound message. Pure like resolve_target_profile:
# the membership arrives as a value (registry.room_members() output in the
# adapter seam), so these tests never touch the filesystem.

ROOM_MEMBERS = ["bot-reminders-a1b2", "bot-scheduler-c3d4", "bot-notes-e5f6"]


def test_room_no_mention_fans_out_to_all_members_in_stored_order():
    """Decision: the broadcast case (no member addressed) has no user-chosen
    order, so the registry's stored order — creation/add order — is the reply
    queue."""
    assert resolve_room_targets(ROOM_MEMBERS, author_is_bot=False) == ROOM_MEMBERS


def test_room_single_mention_runs_only_that_member():
    assert resolve_room_targets(
        ROOM_MEMBERS, author_is_bot=False,
        mentioned_profiles=("bot-scheduler-c3d4",)) == ["bot-scheduler-c3d4"]


def test_room_multiple_mentions_preserve_mention_order():
    """Decision: the mentioned case follows MENTION order (the adapter's
    resolved order = the user's addressing order), NOT stored member order —
    @A then @B means A replies first."""
    assert resolve_room_targets(
        ROOM_MEMBERS, author_is_bot=False,
        mentioned_profiles=("bot-notes-e5f6", "bot-reminders-a1b2")
    ) == ["bot-notes-e5f6", "bot-reminders-a1b2"]


def test_room_bot_authored_never_fans_out():
    """Anti-loop, unchanged from resolve_target_profile: a member's own reply
    must not trigger the other members (the room would talk to itself)."""
    assert resolve_room_targets(ROOM_MEMBERS, author_is_bot=True) == []


def test_room_anti_loop_takes_precedence_over_mentions():
    """Bot-authored + mention is STILL [] — the anti-loop check runs before
    any mention matching."""
    assert resolve_room_targets(
        ROOM_MEMBERS, author_is_bot=True,
        mentioned_profiles=("bot-reminders-a1b2", "bot-scheduler-c3d4")) == []


def test_room_empty_members_is_not_a_room():
    """The adapter calls this only for rooms, but an empty/absent membership
    must never fan out (a no-mention broadcast of nothing would be a silent
    no-op bug, not hub handling)."""
    assert resolve_room_targets([], author_is_bot=False) == []
    assert resolve_room_targets(
        (), author_is_bot=False, mentioned_profiles=("bot-reminders-a1b2",)) == []


def test_room_mention_of_non_member_is_ignored():
    """Decision: mentioned names that are not members are IGNORED, not an
    error — the adapter resolves mentions of humans, the hub, or stale
    members right alongside room members; filtering is this function's job."""
    assert resolve_room_targets(
        ROOM_MEMBERS, author_is_bot=False,
        mentioned_profiles=("hub", "bot-reminders-a1b2")) == ["bot-reminders-a1b2"]


def test_room_mentions_matching_no_member_are_room_wide():
    """Decision: a message whose mentions match NO member (@mentioning a
    human friend, say) is room-wide traffic — every member runs. Silencing
    the room because someone tagged a human would be the surprising read of
    "no member was addressed"."""
    assert resolve_room_targets(
        ROOM_MEMBERS, author_is_bot=False,
        mentioned_profiles=("some-human", "hub")) == ROOM_MEMBERS


def test_room_duplicate_mentions_collapse():
    """A member runs ONCE however often it is mentioned — the result is a
    member queue, not a per-mention task list."""
    assert resolve_room_targets(
        ROOM_MEMBERS, author_is_bot=False,
        mentioned_profiles=("bot-reminders-a1b2", "bot-reminders-a1b2",
                            "bot-scheduler-c3d4")
    ) == ["bot-reminders-a1b2", "bot-scheduler-c3d4"]


def test_room_tuple_members_keep_stored_order():
    """room_members() returns a list, but a tuple membership (frozen test
    doubles, future callers) is equally valid input."""
    assert resolve_room_targets(
        ("bot-a", "bot-b"), author_is_bot=False) == ["bot-a", "bot-b"]


def test_room_junk_members_are_skipped_defensively():
    """A hand-edited registry entry: blank/non-string members can neither
    crash the decision nor run as targets (same defensive posture as
    resolve_target_profile's blank-profile skip)."""
    assert resolve_room_targets(
        ["bot-a", "", "   ", 7, "bot-b"], author_is_bot=False) == ["bot-a", "bot-b"]


def test_room_non_sequence_members_is_not_a_room():
    """None (no lookup result) and a bare string (a caller bug — iterating it
    would char-split) read as "not a room" -> []."""
    assert resolve_room_targets(None, author_is_bot=False) == []
    assert resolve_room_targets("bot-a", author_is_bot=False) == []


def test_room_inputs_are_not_mutated():
    members = ["bot-a", "bot-b"]
    mentions = ["bot-b", "bot-a"]
    out = resolve_room_targets(
        members, author_is_bot=False, mentioned_profiles=mentions)
    assert out == ["bot-b", "bot-a"]
    assert members == ["bot-a", "bot-b"]
    assert mentions == ["bot-b", "bot-a"]


def test_room_result_is_a_fresh_list():
    """The caller owns the result (the adapter pops members off it as turns
    complete); it must never alias the stored membership."""
    members = ["bot-a", "bot-b"]
    out = resolve_room_targets(members, author_is_bot=False)
    out.append("bot-c")
    assert members == ["bot-a", "bot-b"]
