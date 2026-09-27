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
