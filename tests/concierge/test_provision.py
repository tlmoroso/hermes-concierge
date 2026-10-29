"""Provisioning contracts: blueprint -> real profile with pinned model, minimal
toolsets, SOUL, skills, routine cron job, and a channel binding. Runs against a
temp HERMES_HOME with Path.home patched (tests must never touch ~/.hermes).

Task 4.1 discovery notes encoded here (verified against the repo):
- pause is ``enabled=False`` + ``state="paused"`` (cron has NO ``paused`` field);
- a fresh ``no_skills`` profile has NO config.yaml — provision must create it;
- the bot .env copy keeps the profile's 0600 mode (secrets stay owner-only);
- the routine job's origin is ``{"platform": "discord", "chat_id": <channel>}``.

M4 (channel-key hygiene) edge cases locked here — all four were real gaps in
the hand-rolled prefix filter the fix replaced:
- a UTF-8 BOM ahead of the first key and an ``export KEY=`` prefix both dodge a
  ``startswith(prefix)`` test but are channel keys to the canonical tokenizer;
- gateway policy keys (``GATEWAY_ALLOW_ALL_USERS``/``GATEWAY_ALLOWED_USERS``)
  and adapters outside the old seven prefixes (Mattermost, WECOM, relay,
  api_server) must be stripped too;
- shared-prefix keys (``TWILIO_*`` doubles as the telephony tool) follow the
  HUB's adapter activity, not a static list;
- the hub's own .env is never edited, and both secret files land 0600 even when
  the hub's copies are umask-loose.

M4 fix-run B contracts locked here:
- create_bot is TRANSACTIONAL: a failure in any step rolls the profile (and
  its routine) back and re-raises — no half-built bot whose routine keeps
  firing with no registry entry pointing at it;
- reprovisioning with a different blueprint DELETES the replaced bot's profile
  (its binding is being replaced; parking it paused leaks one orphan per
  reprovision);
- retire order is pause -> (optional delete) -> unbind: a mid-retire crash
  always leaves a quiesced, still-bound bot a retry can finish;
- the idempotence check runs under a per-channel provision lock (in-process +
  cross-process): concurrent provisions of one channel yield ONE bot, and a
  lock that cannot be taken fails CLOSED (TimeoutError), not provision-anyway;
- the binding records the applied provider group's config NAME.
"""

import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from agent.secret_scope import load_env_file
from concierge import provision, registry
from hermes_yaml import safe_load as _yaml_safe_load

# Repo root (parent of the ``concierge`` package) so child processes can import it.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(provision.__file__)))

# Hub .env fixture content: one provider key (shared) + one messaging token
# (must NOT be inherited) + a comment line (must be preserved).
HUB_ENV = (
    "# Per-profile secrets for this Hermes profile.\n"
    "OPENAI_API_KEY=sk-x\n"
    "DISCORD_BOT_TOKEN=secret\n"
)


def _yaml(path):
    return _yaml_safe_load(Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def concierge_home(tmp_path, monkeypatch):
    # The NESTED shape (Path.home -> tmp_path, HERMES_HOME -> tmp_path/.hermes)
    # is load-bearing, not incidental:
    # - create_profile resolves profiles/ via get_default_hermes_root(), which
    #   special-cases HERMES_HOME: a value that is NOT under Path.home()/.hermes
    #   takes the Docker/custom-root branch (HERMES_HOME itself becomes the
    #   profiles root's parent), while tmp/.hermes stays on the NORMAL branch —
    #   the exact resolution production ~/.hermes uses. A flat HERMES_HOME would
    #   exercise a different code path than the one operators run.
    # - Path.home must ALSO be patched: some call sites anchor on Path.home()
    #   directly, outside any HERMES_HOME resolution (delete_profile's wrapper
    #   dir is ~/.local/bin). With only HERMES_HOME patched, those sites would
    #   read/write the REAL home — tests must never touch ~/.hermes or ~.
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _provisioned(home, key="reminders", channel_id="123", **kwargs):
    kwargs.setdefault("source_skills_dir", home / "skills")
    return provision.create_bot(key, channel_id=channel_id, guild_id="42", **kwargs)


@pytest.fixture
def honcho_oauth(monkeypatch):
    """The bundled Honcho plugin moved upstream to a catalog install, so
    ``plugins.memory.honcho.oauth`` is not importable from a bare checkout.
    Inject a stub carrying the real predicate's semantics (oauth.py::
    ACCESS_TOKEN_PREFIX == "hch-at-") so the synthesis contracts still
    exercise the in-tree logic they lock."""
    import types

    pkg = types.ModuleType("plugins.memory.honcho")
    pkg.__path__ = []
    oauth = types.ModuleType("plugins.memory.honcho.oauth")
    oauth.is_oauth_access_token = (
        lambda value: bool(value) and value.startswith("hch-at-"))
    monkeypatch.setitem(sys.modules, "plugins.memory.honcho", pkg)
    monkeypatch.setitem(sys.modules, "plugins.memory.honcho.oauth", oauth)


def test_create_bot_makes_profile_with_pinned_model_and_soul(concierge_home):
    binding = _provisioned(concierge_home)
    profile_dir = concierge_home / "profiles" / binding["profile"]
    assert (profile_dir / "SOUL.md").exists()
    assert (
        "Reminder Bot" not in (profile_dir / "SOUL.md").read_text()
    )  # no Discord copy leaked
    # model pin landed in the profile's own config.yaml
    cfg_text = (profile_dir / "config.yaml").read_text()
    assert "glm-5.3-flash" in cfg_text
    cfg = _yaml(profile_dir / "config.yaml")
    assert cfg["model"]["default"] == "z-ai/glm-5.3-flash"
    assert cfg["model"]["provider"] == "nous"
    # toolset allowlist is the blueprint's exact toolsets, nothing more
    assert cfg["toolsets"] == ["todo"]


def test_create_bot_binds_channel_and_is_idempotent_per_channel(concierge_home):
    binding = _provisioned(concierge_home)
    assert registry.get_binding("123")["profile"] == binding["profile"]
    # re-provisioning the same channel returns the SAME bot (no duplicates)
    again = _provisioned(concierge_home)
    assert again["profile"] == binding["profile"]
    # and did not leave a second profile behind
    assert len(_visible_profiles(concierge_home)) == 1


def test_idempotent_return_is_flagged_already_provisioned(concierge_home):
    """M4b review: the response distinguishes created from already-there. A
    fresh create carries NO flag; the idempotent return carries
    ``already_provisioned: True`` — response metadata only: the STORED binding
    never gains the key (bind_channel writes just its own keys), so no other
    binding reader ever sees it."""
    binding = _provisioned(concierge_home)
    assert "already_provisioned" not in binding
    again = _provisioned(concierge_home)
    assert again["already_provisioned"] is True
    assert again["profile"] == binding["profile"]
    stored = registry.get_binding("123")
    assert stored is not None
    assert "already_provisioned" not in stored


def test_reprovision_with_different_blueprint_deletes_old_bot(concierge_home):
    first = _provisioned(concierge_home, key="reminders")
    second = _provisioned(concierge_home, key="researcher")
    assert second["profile"] != first["profile"]
    # the channel now routes to the new bot
    binding = registry.get_binding("123")
    assert binding is not None and binding["profile"] == second["profile"]
    # the old bot's PROFILE is fully deleted — its binding was replaced, so
    # parking it paused would leak one orphan profile + one dead routine per
    # reprovision (M4 fix-run B)
    old_dir = concierge_home / "profiles" / first["profile"]
    assert not old_dir.exists()
    # and no bindable channel still references the old bot
    assert registry.channels_for_profile(first["profile"]) == []


def _bot_store_jobs(home, profile):
    jobs_file = home / "profiles" / profile / "cron" / "jobs.json"
    if not jobs_file.exists():
        return []
    return json.loads(jobs_file.read_text())["jobs"]


def _hub_store_jobs(home):
    jobs_file = home / "cron" / "jobs.json"
    if not jobs_file.exists():
        return []
    return json.loads(jobs_file.read_text())["jobs"]


def test_routine_lands_in_bot_profile_cron_store_not_hub(concierge_home):
    binding = _provisioned(concierge_home)
    # cron stores are per-profile (cron/jobs.py header): the job must live in
    # the BOT's jobs.json — the hub's store must NOT hold it, or the routine
    # would run with the hub's soul/skills/credentials
    names = [j["name"] for j in _bot_store_jobs(concierge_home, binding["profile"])]
    assert any(n.startswith(f"[bot:{binding['profile']}]") for n in names)
    # the job delivers into the bound Discord channel
    job = next(
        j
        for j in _bot_store_jobs(concierge_home, binding["profile"])
        if j["name"].startswith(f"[bot:{binding['profile']}]")
    )
    assert job["deliver"] == "origin"
    assert job["origin"] == {"platform": "discord", "chat_id": "123"}
    # the hub's store never sees it
    assert not any(
        n.startswith("[bot:")
        for n in (j["name"] for j in _hub_store_jobs(concierge_home))
    )


def test_create_bot_seeds_credentials_minus_messaging_secrets(concierge_home):
    (concierge_home / ".env").write_text(HUB_ENV)
    binding = _provisioned(concierge_home)
    env_text = (concierge_home / "profiles" / binding["profile"] / ".env").read_text()
    assert "OPENAI_API_KEY" in env_text  # provider keys are shared
    assert "DISCORD_BOT_TOKEN" not in env_text  # messaging tokens are not
    assert "# Per-profile secrets" in env_text  # comment lines survive
    # secret file stays owner-only
    mode = stat.S_IMODE(
        (concierge_home / "profiles" / binding["profile"] / ".env").stat().st_mode
    )
    assert mode == 0o600


def test_create_bot_without_hub_env_leaves_placeholder_env(concierge_home):
    binding = _provisioned(concierge_home)
    env_path = concierge_home / "profiles" / binding["profile"] / ".env"
    assert env_path.exists()
    assert "DISCORD_BOT_TOKEN" not in env_path.read_text()


def _mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_seed_credentials_strips_bom_and_export_prefixed_channel_keys(concierge_home):
    """The old prefix filter missed both shapes; the canonical tokenizer strips them."""
    hub_env = (
        "DISCORD_BOT_TOKEN=secret\n"          # first key sits right after the BOM
        "export TELEGRAM_BOT_TOKEN=tt\n"      # export prefix defeated startswith()
        "OPENAI_API_KEY=sk-x\n"
    )
    (concierge_home / ".env").write_bytes("\ufeff".encode("utf-8") + hub_env.encode("utf-8"))
    binding = _provisioned(concierge_home)
    env_path = concierge_home / "profiles" / binding["profile"] / ".env"
    env = load_env_file(env_path)  # the gateway's own reader, BOM/export aware
    assert env == {"OPENAI_API_KEY": "sk-x"}
    assert "DISCORD_BOT_TOKEN" not in env_path.read_text(encoding="utf-8-sig")


def test_seed_credentials_strips_policy_keys_and_unlisted_adapters(concierge_home):
    """Gateway policy + adapters outside the old seven prefixes are channel settings too."""
    (concierge_home / ".env").write_text(
        "GATEWAY_ALLOW_ALL_USERS=true\n"
        "GATEWAY_ALLOWED_USERS=alice\n"
        "MATTERMOST_TOKEN=mm\n"
        "GATEWAY_RELAY_SECRET=relay-secret\n"
        "API_SERVER_KEY=api-secret\n"
        "OPENAI_API_KEY=sk-x\n"
        "HONCHO_API_KEY=hk\n"   # memory provider key: shared, never a channel key
    )
    binding = _provisioned(concierge_home)
    env_path = concierge_home / "profiles" / binding["profile"] / ".env"
    env = load_env_file(env_path)
    assert env == {"OPENAI_API_KEY": "sk-x", "HONCHO_API_KEY": "hk"}
    text = env_path.read_text()
    for channel_key in ("GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS",
                        "MATTERMOST_TOKEN", "GATEWAY_RELAY_SECRET", "API_SERVER_KEY"):
        assert channel_key not in text


def test_seed_credentials_shared_prefix_follows_hub_adapter_activity(concierge_home):
    """TWILIO_* is the sms adapter's prefix AND the telephony tool's key: kept
    while the hub doesn't run sms (no ACCOUNT_SID), stripped when it does."""
    (concierge_home / ".env").write_text(
        "TWILIO_AUTH_TOKEN=tool-key\n"
        "OPENAI_API_KEY=sk-x\n"
    )
    binding = _provisioned(concierge_home)
    env = load_env_file(concierge_home / "profiles" / binding["profile"] / ".env")
    assert env == {"TWILIO_AUTH_TOKEN": "tool-key", "OPENAI_API_KEY": "sk-x"}

    # hub now runs sms (complete enable-credential set) -> the token is the bot's
    # identity and must NOT be inherited
    (concierge_home / ".env").write_text(
        "TWILIO_ACCOUNT_SID=AC1\nTWILIO_AUTH_TOKEN=tool-key\nOPENAI_API_KEY=sk-x\n"
    )
    binding2 = _provisioned(concierge_home, channel_id="456")
    env2 = load_env_file(concierge_home / "profiles" / binding2["profile"] / ".env")
    assert env2 == {"OPENAI_API_KEY": "sk-x"}


def test_seed_credentials_never_touches_hub_env(concierge_home):
    hub_env = "# comment\nDISCORD_BOT_TOKEN=secret\nOPENAI_API_KEY=sk-x\n"
    (concierge_home / ".env").write_text(hub_env)
    before = (concierge_home / ".env").read_bytes()
    _provisioned(concierge_home)
    assert (concierge_home / ".env").read_bytes() == before


def test_secret_files_pinned_0600_even_from_loose_hub_files(concierge_home, honcho_oauth):
    """Explicit 0600, not inherited: hub copies land umask-loose (0644) yet the
    bot's .env and honcho.json are owner-only."""
    (concierge_home / ".env").write_text("OPENAI_API_KEY=sk-x\nDISCORD_BOT_TOKEN=secret\n")
    (concierge_home / "honcho.json").write_text('{"apiKey": "honcho-key"}')
    os.chmod(concierge_home / ".env", 0o644)
    os.chmod(concierge_home / "honcho.json", 0o644)
    binding = _provisioned(concierge_home)
    profile_dir = concierge_home / "profiles" / binding["profile"]
    env_path = profile_dir / ".env"
    honcho_path = profile_dir / "honcho.json"
    assert env_path.exists() and honcho_path.exists()
    assert _mode_of(env_path) == 0o600
    assert _mode_of(honcho_path) == 0o600
    # content round-trips: the synthesized config carries the hub's static key
    # at its root, the discord token doesn't survive the .env strip
    assert json.loads(honcho_path.read_text())["apiKey"] == "honcho-key"
    assert "DISCORD_BOT_TOKEN" not in env_path.read_text()


def test_honcho_bot_seeds_the_memory_provider_activation_key(concierge_home, honcho_oauth):
    """Task 9.1 invariant: a ``memory_provider='honcho'`` blueprint must
    ACTIVATE the Honcho provider in the bot profile, not just install its
    connection config. The real activation key (verified against the
    plugin's load chain, NOT guessed): ``memory.provider`` in config.yaml —
    the key ``agent/agent_init.py`` reads
    (``mem_config.get('provider')`` -> ``plugins.memory.load_memory_provider``),
    the one ``hermes_cli/memory_setup.py:313`` writes, and the one
    ``DEFAULT_CONFIG['memory']['provider']`` registers
    (hermes_cli/config_defaults.py). Without it the synthesized honcho.json is
    inert: the bot never constructs a session and remembers nothing across
    sessions — the silent failure Task 9.1 exists to close."""
    (concierge_home / "honcho.json").write_text('{"apiKey": "honcho-key"}')
    binding = _provisioned(concierge_home, key="reminders")  # memory_provider="honcho"
    cfg = _yaml(concierge_home / "profiles" / binding["profile"] / "config.yaml")
    assert cfg["memory"] == {"provider": "honcho"}


def test_install_honcho_synthesizes_from_static_key_never_copies_oauth(concierge_home, honcho_oauth):
    """The M5 contract (supersedes the byte-copy): the bot's honcho.json is
    SYNTHESIZED from a usable STATIC service key — never a byte-copy of the
    hub's file. Honcho OAuth grants are per-profile-host single-use refresh
    chains: the bot's profile host key (``hermes_bot_<slug>_<suffix>``,
    plugins/memory/honcho/client.py::profile_host_key) does not exist in a
    copied file, so the bot falls back to the ROOT apiKey — which, when the
    hub logged in via OAuth, is itself an ``hch-at-`` access token that
    expires, with no oauth block to refresh it: dead auth plus user-facing
    re-login nagging. A synthesized root-key config (host lookup falls back
    to root, client.py:_HostLookup.pick) authenticates for ANY bot profile
    name and never refreshes (a static key never expires,
    oauth.py::ensure_fresh_token returns (None, False) with no oauth block).
    The bot file carries ONLY the static key + non-secret behavior tuning —
    no hosts blocks, no oauth anywhere, no host-specific secrets."""
    (concierge_home / "honcho.json").write_text(
        json.dumps(
            {
                "apiKey": "hub-static-key",
                "workspace": "personal",
                "saveMessages": False,
                "sessionStrategy": "per-channel",
                "dialecticCadence": 3,
                "writeFrequency": "turn",
                "recallMode": "context",
                "observationMode": "unified",
                "dialecticReasoningLevel": "medium",
                "hosts": {
                    "hermes": {
                        "apiKey": "host-token",
                        "oauth": {
                            "refreshToken": "hch-rt-secret",
                            "expiresAt": 1,
                        },
                    }
                },
            }
        )
    )
    binding = _provisioned(concierge_home)
    bot_cfg_path = concierge_home / "profiles" / binding["profile"] / "honcho.json"
    assert bot_cfg_path.exists()
    assert _mode_of(bot_cfg_path) == 0o600
    bot_cfg = json.loads(bot_cfg_path.read_text())
    # the static key rides at the ROOT — what the bot's host lookup falls
    # back to (its profile host block does not exist in this file)
    assert bot_cfg["apiKey"] == "hub-static-key"
    # never a byte-copy: the hub's host blocks, tokens and oauth chain are gone
    assert "hosts" not in bot_cfg
    assert "oauth" not in bot_cfg
    assert "host-token" not in bot_cfg_path.read_text()
    assert "hch-rt-secret" not in bot_cfg_path.read_text()
    # non-secret behavior tuning is carried from the hub's root
    assert bot_cfg["workspace"] == "personal"
    assert bot_cfg["saveMessages"] is False
    assert bot_cfg["sessionStrategy"] == "per-channel"
    assert bot_cfg["dialecticCadence"] == 3
    assert bot_cfg["writeFrequency"] == "turn"
    assert bot_cfg["recallMode"] == "context"
    assert bot_cfg["observationMode"] == "unified"
    assert bot_cfg["dialecticReasoningLevel"] == "medium"
    # the synthesized file carries nothing else
    assert set(bot_cfg) == {
        "apiKey",
        "workspace",
        "saveMessages",
        "sessionStrategy",
        "dialecticCadence",
        "writeFrequency",
        "recallMode",
        "observationMode",
        "dialecticReasoningLevel",
    }


def test_install_honcho_rejects_expired_oauth_root_key_and_skips_memory(concierge_home, honcho_oauth, caplog):
    """The live incident: the hub's honcho.json root ``apiKey`` is an
    expired OAuth access token (``hch-at-`` prefix) and no static key exists
    anywhere in the file. Propagating that key would give the bot auth that
    is already dead, and a copied oauth block cannot refresh (the refresh
    chain is the hub profile's, single-use). The honest outcome: NO
    honcho.json in the bot profile, NO memory.provider activation seed (a
    bot without memory is correct; a bot that nags the user about OAuth is
    not), and exactly one WARNING telling the operator how to fix it."""
    import logging

    (concierge_home / "honcho.json").write_text(
        json.dumps(
            {
                "apiKey": "hch-at-expired-token",
                "oauth": {"refreshToken": "hch-rt-secret", "expiresAt": 1},
                "hosts": {"hermes": {"apiKey": "hch-at-also-oauth"}},
            }
        )
    )
    with caplog.at_level(logging.WARNING, logger="concierge.provision"):
        binding = _provisioned(concierge_home, key="reminders")
    profile_dir = concierge_home / "profiles" / binding["profile"]
    assert not (profile_dir / "honcho.json").exists()
    cfg = _yaml(profile_dir / "config.yaml")
    assert cfg.get("memory", {}).get("provider") in (None, "")
    assert "memory disabled" in caplog.text
    assert "static Honcho key" in caplog.text
    # one warning, not a log storm
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "memory disabled" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_install_honcho_prefers_root_service_api_key(concierge_home, honcho_oauth):
    """A root ``serviceApiKey`` (a static service key, e.g. one minted for
    headless/bot use) beats the root ``apiKey`` — which may be the hub's
    transient OAuth access token. Same static-only test as ``apiKey``: a
    ``serviceApiKey`` that is itself an ``hch-at-`` token is rejected."""
    (concierge_home / "honcho.json").write_text(
        json.dumps(
            {
                "serviceApiKey": "svc-key",
                "apiKey": "hch-at-oauth-token",
            }
        )
    )
    binding = _provisioned(concierge_home)
    bot_cfg_path = concierge_home / "profiles" / binding["profile"] / "honcho.json"
    assert bot_cfg_path.exists()
    bot_cfg = json.loads(bot_cfg_path.read_text())
    assert bot_cfg["apiKey"] == "svc-key"
    assert "hch-at-oauth-token" not in bot_cfg_path.read_text()


def test_install_honcho_falls_back_to_static_host_block_key(concierge_home, honcho_oauth):
    """No root key (or only an OAuth root token), but the hub's active host
    block (``hermes``) carries a STATIC key: that key works — the bot's host
    lookup falls back to root apiKey, and a static key never refreshes or
    expires. An oauth block on that host is never propagated."""
    (concierge_home / "honcho.json").write_text(
        json.dumps(
            {
                "apiKey": "hch-at-expired",
                "hosts": {"hermes": {"apiKey": "hermes-static-key"}},
            }
        )
    )
    binding = _provisioned(concierge_home)
    bot_cfg_path = concierge_home / "profiles" / binding["profile"] / "honcho.json"
    assert bot_cfg_path.exists()
    bot_cfg = json.loads(bot_cfg_path.read_text())
    assert bot_cfg["apiKey"] == "hermes-static-key"
    assert "hosts" not in bot_cfg and "oauth" not in bot_cfg
    # the rejected OAuth token never reaches the bot file
    assert "hch-at-expired" not in bot_cfg_path.read_text()


def test_install_honcho_skips_memory_when_plugin_not_installed(
        concierge_home, monkeypatch, caplog):
    """The post-merge contract: Honcho left the bundled tree for a catalog
    plugin (upstream), so a hub honcho.json with the plugin NOT installed
    must degrade to the honest skip — no bot honcho.json, no
    memory.provider seed, one WARNING naming the fix — never crash the
    provision."""
    import logging

    # deterministic on any checkout: block the module path even where the
    # catalog plugin happens to be installed
    monkeypatch.setitem(sys.modules, "plugins.memory.honcho", None)
    monkeypatch.setitem(sys.modules, "plugins.memory.honcho.oauth", None)
    (concierge_home / "honcho.json").write_text('{"apiKey": "honcho-key"}')
    with caplog.at_level(logging.WARNING, logger="concierge.provision"):
        binding = _provisioned(concierge_home, key="reminders")
    profile_dir = concierge_home / "profiles" / binding["profile"]
    assert not (profile_dir / "honcho.json").exists()
    cfg = _yaml(profile_dir / "config.yaml")
    assert cfg.get("memory", {}).get("provider") in (None, "")
    assert "honcho plugin not installed" in caplog.text


def test_non_memory_bot_carries_no_memory_activation(concierge_home):
    """The flip side: a blueprint with no memory_provider (researcher) must
    NOT carry the activation key — memory stays the profile's default
    (built-in only), so a non-memory bot never pays Honcho's API usage."""
    binding = _provisioned(concierge_home, key="researcher", channel_id="456")
    cfg = _yaml(concierge_home / "profiles" / binding["profile"] / "config.yaml")
    assert cfg.get("memory", {}).get("provider") in (None, "")


def test_create_bot_pins_low_reasoning_effort(concierge_home):
    """Live E2E (2026-09-27): with NO effort pinned, GLM-5.3-flash defaults to
    thinking ON at the server default — a runaway 900s / 2.78 MB stream on a
    trivial 'remind me in one minute'. Bots are single-tool-call agents; every
    provisioned bot pins ``agent.reasoning_effort: low`` — the canonical key
    every surface reads via ``hermes_constants.resolve_reasoning_config``
    (per-model overrides > global), and 'low' is the GLM-5.3 vocabulary's
    floor (agent/reasoning_effort.py GLM53_EFFORTS)."""
    binding = _provisioned(concierge_home)
    cfg = _yaml(concierge_home / "profiles" / binding["profile"] / "config.yaml")
    assert cfg["agent"]["reasoning_effort"] == "low"
    # the pin resolves to an explicit low effort for the bot's model — not
    # None (the provider-default thinking the live E2E caught)
    from hermes_constants import resolve_reasoning_config

    assert resolve_reasoning_config(cfg) == {"enabled": True, "effort": "low"}


def test_create_bot_disables_cron_response_wrapping(concierge_home):
    """Live E2E (2026-09-27): cron deliveries arrived wrapped in 'Cronjob
    Response: <name> (job_id: ...) ... to stop or manage this job' scaffolding
    the user must never see. ``cron.wrap_response: False`` delivers the
    model's final text VERBATIM (scheduler_delivery.py reads the key with
    default True; hermes_cli/config_defaults.py registers it)."""
    binding = _provisioned(concierge_home)
    cfg = _yaml(concierge_home / "profiles" / binding["profile"] / "config.yaml")
    assert cfg["cron"]["wrap_response"] is False


def test_create_bot_is_answer_only_on_discord(concierge_home):
    """Live E2E (2026-09-27): a provisioned bot with no ``display:`` section
    fell back to the Discord platform tier default (TIER_HIGH) — ⚙️
    tool-progress lines, ⏳ 'Working — N min' long-running notices, and
    mid-turn interim commentary posted into the bot's channel. Bots must be
    ANSWER-ONLY: the final response and nothing else. All three keys seed
    False under ``display.platforms.discord``:

    - ``tool_progress`` — _NORMALISERS['tool_progress'] (tristate) maps
      False → 'off'; run_turn.py:3013 gates on
      ``progress_mode not in {"off", "log"}``.
    - ``long_running_notifications`` — _norm_long_running → False kills the
      ⏳ status lines.
    - ``interim_assistant_messages`` — _norm_bool → False suppresses
      mid-turn assistant bubbles; the final answer is still delivered."""
    binding = _provisioned(concierge_home)
    cfg = _yaml(concierge_home / "profiles" / binding["profile"] / "config.yaml")
    discord_display = cfg["display"]["platforms"]["discord"]
    assert discord_display["tool_progress"] is False
    assert discord_display["long_running_notifications"] is False
    assert discord_display["interim_assistant_messages"] is False


def test_create_bot_inherits_provider_resilience_config(concierge_home):
    (concierge_home / "config.yaml").write_text(
        "fallback_providers: [anthropic]\n"
        "credential_pool_strategies: {openai: round_robin}\n"
    )
    binding = _provisioned(concierge_home)
    cfg_text = (
        concierge_home / "profiles" / binding["profile"] / "config.yaml"
    ).read_text()
    assert "fallback_providers" in cfg_text and "round_robin" in cfg_text
    cfg = _yaml(concierge_home / "profiles" / binding["profile"] / "config.yaml")
    assert cfg["fallback_providers"] == ["anthropic"]
    assert cfg["credential_pool_strategies"] == {"openai": "round_robin"}


def test_create_bot_copies_blueprint_skills_only(concierge_home):
    # a source skills tree with one blueprint skill and one decoy
    src = concierge_home / "skills"
    (src / "google-workspace").mkdir(parents=True)
    (src / "google-workspace" / "SKILL.md").write_text(
        "---\nname: google-workspace\n---\n"
    )
    (src / "decoy").mkdir(parents=True)
    (src / "decoy" / "SKILL.md").write_text("---\nname: decoy\n---\n")
    binding = _provisioned(concierge_home, key="scheduler")
    profile_skills = concierge_home / "profiles" / binding["profile"] / "skills"
    assert (profile_skills / "google-workspace" / "SKILL.md").exists()
    assert not (profile_skills / "decoy").exists()


def test_retire_bot_unbinds_and_pauses_routines(concierge_home):
    binding = _provisioned(concierge_home)
    profile = binding["profile"]
    provision.retire_bot(profile)
    assert registry.channels_for_profile(profile) == []
    for job in _bot_store_jobs(concierge_home, profile):
        if job["name"].startswith(f"[bot:{profile}]"):
            # cron's pause contract: enabled=False + state="paused" (no `paused` field)
            assert job["enabled"] is False
            assert job["state"] == "paused"
            assert job.get("paused_reason") == "bot retired"


def test_retire_bot_missing_profile_is_safe(concierge_home):
    # unbind + pause must not explode when the profile dir is already gone
    registry.bind_channel("77", profile="bot-ghost-0000", blueprint="reminders")
    provision.retire_bot("bot-ghost-0000")
    assert registry.get_binding("77") is None


def test_bot_status_reports_channels_and_routines(concierge_home):
    binding = _provisioned(concierge_home)
    status = provision.bot_status(binding["profile"])
    assert status["profile"] == binding["profile"]
    assert status["channels"] == ["123"]
    assert len(status["routines"]) == 1
    routine = status["routines"][0]
    assert routine["name"] == f"[bot:{binding['profile']}] morning-agenda"
    assert routine["paused"] is False


# ---------------------------------------------------------------------------
# M4 fix-run B: transactional create_bot + rollback
# ---------------------------------------------------------------------------

def _profiles_dir(home):
    return home / "profiles"


def _visible_profiles(home):
    """Real profile dirs under profiles/ — the canonical delete leaves a
    hidden ``.deleted`` tombstone dir behind, which is not a profile."""
    d = _profiles_dir(home)
    if not d.exists():
        return []
    return [p for p in d.iterdir() if not p.name.startswith(".")]


def test_failed_provision_rolls_back_profile_and_reraises(concierge_home, monkeypatch):
    """The provision is TRANSACTIONAL: any failing step (here: the routine's
    create_job, the LAST provision step before the bind) rolls the profile
    back and re-raises the original error. The alternative — a profile on disk
    with a live routine and no binding pointing at it — is an unretireable
    orphan (nothing maps channel -> profile), the worst failure state."""
    from cron import jobs as cron_jobs

    def _boom(*args, **kwargs):
        raise RuntimeError("cron store exploded")

    monkeypatch.setattr(cron_jobs, "create_job", _boom)
    with pytest.raises(RuntimeError, match="cron store exploded"):
        provision.create_bot("reminders", channel_id="123")
    # the half-built profile is gone — no orphan on disk
    assert _visible_profiles(concierge_home) == []
    # and the channel is NOT bound: a retry starts clean
    assert registry.get_binding("123") is None


def test_failed_provision_failure_before_profile_creation_leaves_no_trace(concierge_home, monkeypatch):
    """A failure BEFORE create_profile (bad blueprint) must not attempt any
    rollback and must leave nothing behind."""
    with pytest.raises(KeyError, match="Unknown blueprint"):
        provision.create_bot("no-such-blueprint", channel_id="123")
    assert registry.get_binding("123") is None
    assert _visible_profiles(concierge_home) == []


def test_rollback_survives_delete_profile_failure(concierge_home, monkeypatch, caplog):
    """The rollback is best-effort: a delete_profile failure inside rollback
    must NOT mask the provision's own error — the original exception still
    propagates, with the manual-removal instruction logged."""
    import logging

    from cron import jobs as cron_jobs
    from hermes_cli import profiles as profiles_mod

    def _cron_boom(*args, **kwargs):
        raise RuntimeError("cron store exploded")

    def _delete_boom(name, yes=False):
        raise OSError("disk full")

    monkeypatch.setattr(cron_jobs, "create_job", _cron_boom)
    monkeypatch.setattr(profiles_mod, "delete_profile", _delete_boom, raising=False)
    with caplog.at_level(logging.ERROR, logger="concierge.provision"):
        with pytest.raises(RuntimeError, match="cron store exploded"):
            provision.create_bot("reminders", channel_id="123")
    assert "hermes profile delete" in caplog.text  # the operator instruction


def test_reprovision_failure_rolls_back_new_bot_keeps_channel_routed(concierge_home, monkeypatch):
    """A failure DURING a reprovision (old bot retired+deleted, new bot's
    routine step fails): the NEW half-built profile is rolled back and the
    original error propagates. The channel keeps no stale binding to the
    deleted old bot — a retry provisions cleanly from scratch."""
    _first = _provisioned(concierge_home, key="reminders")
    from cron import jobs as cron_jobs

    _real_create_job = cron_jobs.create_job

    def _boom(*args, **kwargs):
        raise RuntimeError("cron store exploded")

    monkeypatch.setattr(cron_jobs, "create_job", _boom)
    with pytest.raises(RuntimeError, match="cron store exploded"):
        _provisioned(concierge_home, key="scheduler")
    # the half-built SECOND profile is gone; so is the retired first one
    assert _visible_profiles(concierge_home) == []
    # the channel is unbound (the old bot was deleted before the failure):
    # a retry provisions cleanly
    assert registry.get_binding("123") is None
    # and the retry succeeds once the transient fault clears
    monkeypatch.setattr(cron_jobs, "create_job", _real_create_job)
    second = _provisioned(concierge_home, key="scheduler")
    bound = registry.get_binding("123")
    assert bound is not None and bound["profile"] == second["profile"]
    assert bound["blueprint"] == "scheduler"


# ---------------------------------------------------------------------------
# M4 fix-run B: retire order (pause before unbind, delete last)
# ---------------------------------------------------------------------------

def test_retire_bot_pauses_before_unbinding(concierge_home, monkeypatch):
    """ORDER contract: the binding must still exist when the pause runs — a
    crash between them leaves a quiesced, still-bound bot (retryable), not a
    live orphan. Instrumented pause records the binding state at pause time."""
    binding = _provisioned(concierge_home)
    profile = binding["profile"]

    seen = {}
    original_pause = provision._pause_bot_routines

    def _pause_and_record(profile_name, profile_dir):
        seen["binding_at_pause"] = registry.get_binding("123")
        seen["channels_at_pause"] = registry.channels_for_profile(profile_name)
        return original_pause(profile_name, profile_dir)

    monkeypatch.setattr(provision, "_pause_bot_routines", _pause_and_record)
    provision.retire_bot(profile)
    # the pause ran while the channel was still bound
    assert seen["binding_at_pause"] is not None
    assert seen["channels_at_pause"] == ["123"]
    # and after the full retire, nothing is bound
    assert registry.get_binding("123") is None


def test_retire_crash_after_pause_leaves_quiesced_bound_bot(concierge_home, monkeypatch):
    """A crash mid-retire (after pause, before unbind) leaves a PAUSED,
    still-bound bot — every subsequent scan can still find it by channel, and
    a retry finishes the retire. This is the state the order change buys."""
    binding = _provisioned(concierge_home)
    profile = binding["profile"]

    _real_unbind = registry.unbind_channel

    def _unbind_boom(channel_id):
        raise RuntimeError("registry write failed")

    monkeypatch.setattr(registry, "unbind_channel", _unbind_boom)
    with pytest.raises(RuntimeError, match="registry write failed"):
        provision.retire_bot(profile)

    # binding survived, and the routine is paused: retryable, not orphaned
    binding_after = registry.get_binding("123")
    assert binding_after is not None and binding_after["profile"] == profile
    jobs = _bot_store_jobs(concierge_home, profile)
    assert jobs, "bot should still hold its routine job record"
    for job in jobs:
        if job["name"].startswith(f"[bot:{profile}]"):
            assert job["enabled"] is False and job["state"] == "paused"

    # the retry (real unbind restored) finishes cleanly
    monkeypatch.setattr(registry, "unbind_channel", _real_unbind)
    provision.retire_bot(profile)
    assert registry.get_binding("123") is None


def test_retire_bot_delete_profile_removes_profile_and_unbinds(concierge_home):
    """retire_bot(delete_profile=True) is the full teardown: profile gone,
    channels unbound. This is also the reprovision path (create_bot calls it)."""
    binding = _provisioned(concierge_home)
    profile = binding["profile"]
    provision.retire_bot(profile, delete_profile=True)
    assert not (concierge_home / "profiles" / profile).exists()
    assert registry.get_binding("123") is None


# ---------------------------------------------------------------------------
# M4 fix-run B: per-channel provision lock vs TOCTOU
# ---------------------------------------------------------------------------

@pytest.mark.skipif(registry.fcntl is None, reason="POSIX fcntl/flock required")
def test_concurrent_provisions_of_one_channel_yield_one_bot(concierge_home):
    """Two THREADS provisioning the same channel concurrently: exactly one
    profile, one routine, one binding. Without the in-process half of the
    lock both threads pass the read-then-act idempotence check and the loser
    is orphaned (its routine still fires, nothing points at it)."""
    results = []
    errors = []

    def _worker():
        try:
            results.append(_provisioned(concierge_home))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"concurrent provisions raised: {errors}"
    # every caller got the SAME binding (the lock serializes them; each later
    # one re-checks inside it and returns the existing bot)
    assert len(results) == 4
    assert len({r["profile"] for r in results}) == 1
    assert len(_visible_profiles(concierge_home)) == 1
    assert registry.get_binding("123") is not None


@pytest.mark.skipif(registry.fcntl is None, reason="POSIX fcntl/flock required")
def test_provision_lock_is_cross_process(concierge_home):
    """The provision lock must exclude a separate PROCESS (gateway thread vs.
    `hermes concierge` CLI): a child holds channel 999's lock while the parent
    proves it cannot take the same lock file, then provisions after release."""
    ready = concierge_home / "child_holds_999"
    release = concierge_home / "child_may_release"
    holder = concierge_home / "holder.py"
    holder.write_text(
        textwrap.dedent(
            f"""
            import os, sys, time, pathlib
            sys.path.insert(0, {_REPO_ROOT!r})
            os.environ["HERMES_HOME"] = {str(concierge_home)!r}
            from concierge import provision

            with provision._channel_provision_lock("999"):
                pathlib.Path({str(ready)!r}).write_text("1")
                for _ in range(600):
                    if pathlib.Path({str(release)!r}).exists():
                        break
                    time.sleep(0.05)
            """
        )
    )
    child = subprocess.Popen([sys.executable, str(holder)])
    try:
        for _ in range(1000):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "child never acquired the provision lock"
        # while the child holds it, the lock FILE is flock-exclusive: a
        # non-blocking acquire from this process must fail
        lock_file = provision._provision_lock_path("999")
        assert lock_file == concierge_home / "concierge" / ".provision-999.lock"
        import fcntl as _fcntl
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            with pytest.raises(OSError):
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        release.write_text("1")
        child.wait(timeout=30)
    # released: provisioning works and binds
    binding = _provisioned(concierge_home, channel_id="999")
    bound = registry.get_binding("999")
    assert bound is not None and bound["profile"] == binding["profile"]


@pytest.mark.skipif(registry.fcntl is None, reason="POSIX fcntl/flock required")
def test_provision_lock_timeout_fails_closed(concierge_home, monkeypatch):
    """The provision lock FAILS CLOSED: when the flock cannot be taken within
    the bound, create_bot raises TimeoutError instead of provision-anyway —
    a duplicate bot is strictly worse than a picker action the user retries."""
    ready = concierge_home / "child_holds_123"
    release = concierge_home / "child_may_release"
    holder = concierge_home / "holder.py"
    holder.write_text(
        textwrap.dedent(
            f"""
            import os, sys, time, pathlib
            sys.path.insert(0, {_REPO_ROOT!r})
            os.environ["HERMES_HOME"] = {str(concierge_home)!r}
            from concierge import provision

            with provision._channel_provision_lock("123"):
                pathlib.Path({str(ready)!r}).write_text("1")
                for _ in range(600):
                    if pathlib.Path({str(release)!r}).exists():
                        break
                    time.sleep(0.05)
            """
        )
    )
    child = subprocess.Popen([sys.executable, str(holder)])
    try:
        for _ in range(1000):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "child never acquired the provision lock"
        # shrink the timeout so the test stays fast; the child holds the lock
        monkeypatch.setattr(provision, "_PROVISION_LOCK_TIMEOUT_SECONDS", 0.5)
        with pytest.raises(TimeoutError, match="provision lock"):
            _provisioned(concierge_home)
        # nothing was provisioned while the lock was held
        assert registry.get_binding("123") is None
        assert _visible_profiles(concierge_home) == []
    finally:
        release.write_text("1")
        child.wait(timeout=30)


def test_provision_lock_path_confines_hostile_channel_ids(concierge_home):
    """A channel id that is not a safe filename component is HASHED, never
    embedded — a hostile id must not escape the concierge dir, and two distinct
    ids must not collide onto one lock (under-locking reintroduces the race)."""
    base = concierge_home / "concierge"
    safe = provision._provision_lock_path("1234567890")
    assert safe.parent == base and safe.name == ".provision-1234567890.lock"
    hostile = "../../etc/passwd"
    p = provision._provision_lock_path(hostile)
    assert p.parent == base  # confined: no path escape
    assert p != safe
    # distinct hostile ids do not share a lock file
    p2 = provision._provision_lock_path("..%2f..%2fetc%2fpasswd")
    assert p2 != p
    # a >64-char id is hashed too (filesystem name limits)
    long_id = "9" * 65
    pl = provision._provision_lock_path(long_id)
    assert pl.parent == base and pl != provision._provision_lock_path("9" * 64)


# ---------------------------------------------------------------------------
# M4 fix-run B: provider-group name in the binding
# ---------------------------------------------------------------------------

def test_binding_records_provider_group_name(concierge_home):
    """The binding records the applied provider group's config NAME — the key
    in concierge.provider_groups, lost when the group was resolved into a
    bare value dict. Without it, every binding's provider_group was None."""
    group = {"provider": "openai", "model": "gpt-4o-mini", "guild_ids": ["42"]}
    binding = provision.create_bot(
        "reminders", channel_id="123", guild_id="42",
        provider_group=dict(group, name="personal"),
    )
    assert binding["provider_group"] == "personal"
    persisted = registry.get_binding("123")
    assert persisted is not None and persisted["provider_group"] == "personal"
    # the profile's config pins the GROUP's model, not the preset's
    cfg = _yaml(concierge_home / "profiles" / binding["profile"] / "config.yaml")
    assert cfg["model"]["provider"] == "openai"
    assert cfg["model"]["default"] == "gpt-4o-mini"


def test_binding_without_provider_group_stays_none(concierge_home):
    """No group resolved -> no name in the binding (None, not a fabricated
    default); the profile pins the preset's model."""
    binding = _provisioned(concierge_home)
    assert binding["provider_group"] is None
    persisted = registry.get_binding("123")
    assert persisted is not None and persisted["provider_group"] is None
    cfg = _yaml(concierge_home / "profiles" / binding["profile"] / "config.yaml")
    assert cfg["model"]["default"] == "z-ai/glm-5.3-flash"


def test_resolve_provider_group_annotates_winner_with_name():
    """resolve_provider_group returns the winner ANNOTATED with its config key
    under "name" — the value create_bot stores in the binding. Verified at
    every tier: category, guild and default."""
    from concierge.provider_groups import resolve_provider_group

    groups = {
        "personal": {"provider": "openai", "model": "gpt-4o-mini",
                      "guild_ids": ["42"], "category_ids": ["7"]},
        "premium": {"provider": "anthropic", "model": "claude-sonnet"},
    }
    # category tier
    cat = resolve_provider_group(groups, guild_id="42", category_id="7")
    assert cat["name"] == "personal"
    # guild tier
    guild = resolve_provider_group(groups, guild_id="42", category_id="9")
    assert guild["name"] == "personal"
    # default tier
    dflt = resolve_provider_group(groups, guild_id="1", category_id="2",
                                 default_group="premium")
    assert dflt["name"] == "premium"
    # the config mapping itself is never mutated
    assert "name" not in groups["personal"]
    # no match: None
    assert resolve_provider_group(groups, guild_id="1", category_id="2") is None


def test_resolve_provider_group_unknown_default_returns_none():
    """A default_group naming a key that does not exist resolves to None —
    the caller treats it as no group (binding gets None), not a crash."""
    from concierge.provider_groups import resolve_provider_group

    groups = {"a": {"provider": "x", "model": "y"}}
    assert resolve_provider_group(groups, guild_id="1", category_id="2",
                                  default_group="nope") is None
