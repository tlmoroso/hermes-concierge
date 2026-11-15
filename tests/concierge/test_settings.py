"""concierge config contract: DEFAULT_CONFIG key exists AND has a runtime reader
(repo invariant: every registered key is read; every reader is registered)."""
import pytest

from hermes_cli.config_defaults import DEFAULT_CONFIG
from concierge.settings import concierge_settings


def test_default_config_has_concierge_section():
    concierge = DEFAULT_CONFIG["concierge"]
    assert concierge["enabled"] is False
    assert concierge["managed_category_ids"] == []
    assert concierge["hub_profile"] == "concierge"
    assert isinstance(concierge["provider_groups"], dict)
    assert concierge["location_channel_id"] is None


def test_reader_merges_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = concierge_settings({"concierge": {"enabled": True}})
    assert settings.enabled is True          # override honored
    assert settings.managed_category_ids == []  # default filled in
    assert settings.location_channel_id is None  # default filled in


def test_reader_reads_location_channel_id(tmp_path, monkeypatch):
    """Task 10.1: the location webhook's target channel, read as a string
    (Discord ids arrive as int from YAML just as often as str)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = concierge_settings({"concierge": {"location_channel_id": "100"}})
    assert settings.location_channel_id == "100"
    settings = concierge_settings({"concierge": {"location_channel_id": 100}})
    assert settings.location_channel_id == "100"  # int id coerced to str


from concierge.provider_groups import resolve_provider_group, apply_group_to_preset
from concierge.blueprints import MODEL_PRESETS


def test_guild_level_provider_group_wins_over_default():
    groups = {"personal": {"provider": "openai", "model": "gpt-4o-mini",
                           "guild_ids": ["42"]}}
    group = resolve_provider_group(groups, guild_id="42", category_id="7",
                                   default_group="fallback")
    assert group is not None and group["provider"] == "openai"


def test_category_match_beats_guild_match():
    groups = {
        "cheap": {"provider": "openai", "model": "gpt-4o-mini", "guild_ids": ["42"]},
        "premium": {"provider": "anthropic", "model": "claude-sonnet-4-20250514",
                    "category_ids": ["7"]},
    }
    group = resolve_provider_group(groups, guild_id="42", category_id="7")
    assert group["provider"] == "anthropic"  # category is more specific


def test_no_match_returns_none_then_default():
    assert resolve_provider_group({}, guild_id="1", category_id="2") is None
    group = resolve_provider_group({"fb": {"provider": "x", "model": "y"}},
                                   guild_id="1", category_id="2", default_group="fb")
    assert group is not None


def test_apply_group_overrides_preset():
    preset = MODEL_PRESETS["core"]
    applied = apply_group_to_preset({"provider": "groq", "model": "llama-3.3-70b"}, preset)
    assert (applied.provider, applied.model) == ("groq", "llama-3.3-70b")


def test_settings_roundtrip_through_the_effective_loader(tmp_path, monkeypatch):
    """Loader-backed invariant (hermes_cli/AGENTS.md): a DEFAULT_CONFIG key is proven
    through the loader its consuming surface uses. The Discord surface reads user
    config via load_user_config_effective — which merges NO defaults — so this pins
    the real roundtrip: keys set in config.yaml override, keys absent from the file
    are filled in by the reader, and Discord ids are coerced to str."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "concierge:\n"
        "  enabled: true\n"
        "  managed_category_ids: ['123', 456]\n"
        "  default_provider_group: personal\n"
        "  location_channel_id: 100\n"
        "  provider_groups:\n"
        "    personal: {provider: openai, model: gpt-4o-mini, guild_ids: ['42']}\n",
        encoding="utf-8",
    )
    from hermes_cli.config_effective import load_user_config_effective

    effective = load_user_config_effective(home / "config.yaml")
    assert "hub_profile" not in effective["concierge"]  # the loader adds no defaults
    settings = concierge_settings(effective)
    assert settings.enabled is True
    assert settings.managed_category_ids == ["123", "456"]  # int id coerced to str
    assert settings.default_provider_group == "personal"
    assert settings.provider_groups["personal"]["model"] == "gpt-4o-mini"
    assert settings.hub_profile == "concierge"  # reader filled the DEFAULT_CONFIG value
    assert settings.location_channel_id == "100"  # int id coerced to str


def test_same_tier_tie_breaks_to_first_listed_group():
    """Contract: two groups matching at the same tier resolves deterministically by
    config order — the first entry in concierge.provider_groups wins."""
    guild_tie = {
        "first": {"provider": "openai", "model": "m1", "guild_ids": ["42"]},
        "second": {"provider": "anthropic", "model": "m2", "guild_ids": ["42"]},
    }
    guild_winner = resolve_provider_group(guild_tie, guild_id="42", category_id="1")
    assert guild_winner is not None and guild_winner["provider"] == "openai"
    category_tie = {
        "first": {"provider": "openai", "model": "m1", "category_ids": ["7"]},
        "second": {"provider": "anthropic", "model": "m2", "category_ids": ["7"]},
    }
    category_winner = resolve_provider_group(category_tie, guild_id="1", category_id="7")
    assert category_winner is not None and category_winner["provider"] == "openai"


def test_malformed_group_raises_helpful_keyerror():
    """Contract: a provider-groups entry missing 'provider'/'model' (a config typo)
    fails at this boundary with a KeyError that names the missing key and the config
    section to fix — never a bare ``KeyError: 'model'`` from deep in provisioning."""
    with pytest.raises(KeyError) as excinfo:
        apply_group_to_preset({"provider": "groq"}, MODEL_PRESETS["core"])
    msg = str(excinfo.value)
    assert "'model'" in msg
    assert "concierge.provider_groups" in msg
    assert "groq" in msg  # the offending entry is identifiable in the message


# ── §8 config surface: home_category + threading (card A) ──────────────────
# Root invariant: every DEFAULT_CONFIG key has a runtime reader. The new nested
# keys ship OFF (disabled placement, managed scope, mentions threading) so an
# absent/disabled config is byte-for-byte today's behavior. Later cards read the
# one typed surface: settings.home_category.* and settings.threading.mode.


def test_default_config_has_home_category_and_threading_keys():
    """The registered defaults exist and pin the conservative ship-OFF values."""
    hc = DEFAULT_CONFIG["concierge"]["home_category"]
    assert hc["enabled"] is False
    assert hc["visibility"] == "private"
    assert hc["general_channel"] == "general"
    assert hc["unbound_scope"] == "managed"
    threading = DEFAULT_CONFIG["concierge"]["threading"]
    assert threading["mode"] == "mentions"


def test_reader_returns_conservative_defaults_on_empty_config(tmp_path, monkeypatch):
    """concierge_settings() with no config fills every new key from DEFAULT_CONFIG."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = concierge_settings()
    assert settings.home_category.enabled is False
    assert settings.home_category.visibility == "private"
    assert settings.home_category.general_channel == "general"
    assert settings.home_category.unbound_scope == "managed"
    assert settings.threading.mode == "mentions"


def test_reader_honors_explicit_home_category_and_threading_values(tmp_path, monkeypatch):
    """An explicit value for each new key is honored (bool gate, non-blank strs)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = concierge_settings({"concierge": {
        "home_category": {
            "enabled": True,
            "visibility": "public",
            "general_channel": "lobby",
            "unbound_scope": "guild",
        },
        "threading": {"mode": "always"},
    }})
    assert settings.home_category.enabled is True
    assert settings.home_category.visibility == "public"
    assert settings.home_category.general_channel == "lobby"
    assert settings.home_category.unbound_scope == "guild"
    assert settings.threading.mode == "always"


def test_reader_partial_subsection_fills_missing_keys_from_defaults(tmp_path, monkeypatch):
    """A partial sub-section deep-merges over defaults: flipping only enabled
    keeps the other conservative defaults (no shallow-replace of the sub-dict)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = concierge_settings({"concierge": {
        "home_category": {"enabled": True},
        "threading": {},
    }})
    assert settings.home_category.enabled is True
    assert settings.home_category.visibility == "private"     # default filled in
    assert settings.home_category.general_channel == "general"
    assert settings.home_category.unbound_scope == "managed"
    assert settings.threading.mode == "mentions"


@pytest.mark.parametrize("garbage", ["nope", [], {"a": 1}])
def test_reader_ignores_non_dict_subsection(tmp_path, monkeypatch, garbage):
    """A sub-section that isn't a mapping is junk: its defaults stay in force."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = concierge_settings({"concierge": {"home_category": garbage}})
    assert settings.home_category.enabled is False
    assert settings.home_category.visibility == "private"
    assert settings.home_category.unbound_scope == "managed"


@pytest.mark.parametrize("field,garbage", [
    ("visibility", ""),
    ("visibility", "   "),
    ("visibility", None),
    ("visibility", 123),
    ("visibility", ["public"]),
    ("general_channel", None),
    ("general_channel", 5),
    ("unbound_scope", {}),
    ("unbound_scope", "  "),
])
def test_reader_str_fields_fail_closed_to_default_on_garbage(field, garbage, tmp_path, monkeypatch):
    """Blank / wrong-typed values resolve to the DEFAULT_CONFIG default,
    fail-closed — mirroring _coerce_config_set_value's reject-junk disposition."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    defaults = DEFAULT_CONFIG["concierge"]["home_category"]
    settings = concierge_settings({"concierge": {"home_category": {field: garbage}}})
    assert getattr(settings.home_category, field) == defaults[field]


@pytest.mark.parametrize("garbage", ["", "  ", None, 7, ["x"], {"a": 1}, False])
def test_reader_threading_mode_fails_closed_to_default_on_garbage(garbage, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = concierge_settings({"concierge": {"threading": {"mode": garbage}}})
    assert settings.threading.mode == DEFAULT_CONFIG["concierge"]["threading"]["mode"]


def test_reader_preserves_string_enum_off_not_bool(tmp_path, monkeypatch):
    """Mirror _coerce_config_set_value: a string-typed enum member such as
    threading.mode='off' stays the string 'off'. It must never be coerced to a
    bool (which would silently flip the surface-wide threading policy)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = concierge_settings({"concierge": {"threading": {"mode": "off"}}})
    assert settings.threading.mode == "off"
    assert isinstance(settings.threading.mode, str)


@pytest.mark.parametrize("garbage", ["", None, [], "not-a-bool", 5, {"x": 1}])
def test_reader_home_category_enabled_fails_closed_on_garbage(garbage, tmp_path, monkeypatch):
    """The placement gate is fail-closed: junk never turns the feature ON."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = concierge_settings({"concierge": {"home_category": {"enabled": garbage}}})
    assert settings.home_category.enabled is False


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False), ("true", True), ("on", True), ("no", False), ("off", False),
])
def test_reader_home_category_enabled_honors_bool_and_scalar_words(raw, expected, tmp_path, monkeypatch):
    """YAML bools and the scalar words `hermes config set` recognizes are honored."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    settings = concierge_settings({"concierge": {"home_category": {"enabled": raw}}})
    assert settings.home_category.enabled is expected


def test_new_keys_roundtrip_through_effective_loader(tmp_path, monkeypatch):
    """Loader-backed invariant (hermes_cli/AGENTS.md): prove the real
    config.yaml -> load_user_config_effective -> concierge_settings roundtrip.
    The effective loader adds NO defaults, so an absent key is filled in by the
    reader with its conservative DEFAULT_CONFIG value."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "concierge:\n"
        "  home_category:\n"
        "    enabled: true\n"
        "    visibility: public\n"
        "  threading:\n"
        "    mode: always\n",
        encoding="utf-8",
    )
    from hermes_cli.config_effective import load_user_config_effective

    effective = load_user_config_effective(home / "config.yaml")
    settings = concierge_settings(effective)
    assert settings.home_category.enabled is True
    assert settings.home_category.visibility == "public"
    assert settings.home_category.general_channel == "general"  # absent -> reader default
    assert settings.home_category.unbound_scope == "managed"    # absent -> reader default
    assert settings.threading.mode == "always"
