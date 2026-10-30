"""Provisioning: render a BotBlueprint into a real Hermes profile + routine.

On-rails by construction: the ONLY inputs are the blueprint key and the
Discord channel. Everything else (model, toolsets, skills, SOUL, schedule,
memory provider) comes from the catalog — users customize tasks and chat, not
infrastructure. Built on the same primitives Bot Mode uses: profiles +
namespaced cron jobs; no new engine.

Task 4.1 discovery facts this module encodes (verified against the repo):
- Model pin: ``model.default`` + ``model.provider`` — the canonical keys
  ``hermes_cli/config.py::_normalize_root_model_keys`` normalizes everything to.
- Honcho activation (Task 9.1, verified against the plugin's load chain):
  ``memory.provider: honcho`` in config.yaml — the key agent/agent_init.py
  reads (``mem_config.get("provider")`` -> plugins.memory.
  load_memory_provider) and DEFAULT_CONFIG registers. The bot's honcho.json
  is SYNTHESIZED by ``_install_honcho`` from a STATIC service key (never a
  byte-copy of the hub's file — OAuth grants are per-profile-host
  single-use refresh chains and cannot survive a copy; see _install_honcho);
  it is CONNECTION config only (apiKey/baseUrl/environment/workspace —
  plugins/memory/honcho/client.py ``resolve_config_path`` chain) and
  activates nothing. When no usable static key exists, the bot gets no
  honcho.json and no memory.provider seed: a bot without memory beats a bot
  with dead OAuth auth. Workspace and session strategy need NO seeding:
  they default inside the plugin (workspace -> the profile host,
  client.py:283 ``look.pick("workspace") or host``; sessionStrategy ->
  "per-directory"), and the hub's root-level choices are carried into the
  synthesized file when it made any.
- Toolset allowlist: the TOP-LEVEL ``toolsets`` list (config_defaults.py:26,
  default ``["hermes-cli"]``); a turn under a bot profile needs only the
  blueprint's sets — the hub's Discord adapter owns the platform connection.
- Cron origin: ``{"platform": "discord", "chat_id": <channel_id>}`` — the
  minimum ``cron/scheduler_delivery.py::_resolve_origin`` accepts; discord is a
  push platform, so ``deliver="origin"`` posts the routine into the channel.
- Pause: cron jobs have NO ``paused`` field — pause is ``enabled=False`` +
  ``state="paused"`` (+ ``paused_at``/``paused_reason``), exactly what
  ``cron.jobs.pause_job`` writes. We write the same fields via ``update_job``.
- A fresh ``create_profile(no_skills=True)`` profile has NO config.yaml and a
  placeholder ``.env`` (0600) — provision seeds both itself.
- M4: the ``.env`` copy's channel-key filter is hermes_cli.profile_channels'
  ``ChannelKeyIndex``/``strip_channel_env_file`` — the clone path's OWN
  mechanism, evaluated in the hub's plugin scope — never a local prefix list;
  ``.env`` and ``honcho.json`` are written with the utils atomic-secret mode
  (explicit 0600).

M4 fix-run B, encoded here (each with tests in tests/concierge/test_provision.py):
- ``create_bot`` is TRANSACTIONAL: the profile, its seeded files and its
  routine exist before the channel binding does, so a failure in any later
  step would strand a half-built bot no registry entry points at — whose
  routine still fires into the channel, unretireable through the Discord
  surface. Any failure now rolls the whole bot back (routines paused, profile
  deleted via ``hermes_cli.profiles.delete_profile`` — the canonical teardown,
  not a bare rmtree) and re-raises the original error.
- ``retire_bot`` pauses routines BEFORE unbinding channels (delete last). The
  old order could die between unbind and pause and leave a LIVE routine with
  NO binding — an orphan that keeps posting and cannot be retired by channel.
  Pause-first means a mid-retire crash always leaves a quiesced, still-bound
  bot a retry can finish.
- Reprovision DELETES the replaced bot's profile: its binding is being
  replaced, so parking it paused leaks one orphan profile + one dead routine
  per reprovision.
- The whole check-create-bind sequence runs under a PER-CHANNEL provision
  lock (in-process RLock + cross-process flock on
  ``<concierge dir>/.provision-<channel>.lock``): the idempotence check is
  read-then-act, and two concurrent provisions of one channel would both
  pass it and orphan the loser (TOCTOU). The lock FAILS CLOSED on timeout —
  a duplicate bot is strictly worse than a picker action the user retries.
- The binding records the applied provider group's NAME
  (``resolve_provider_group`` annotates its winner): without it every
  binding's ``provider_group`` was None (the name is the config dict's key,
  lost when the group was resolved into a bare value dict).
M4b review (truth-in-advertising for the CLI surface): the idempotent early
return carries ``already_provisioned: True`` so a caller can tell "created
just now" from "was already there" — the operator CLI prints "Already
provisioned" instead of "Created" off this flag. Response metadata only:
``bind_channel`` writes just its own keys, so the flag never reaches the
registry (nor any other reader of stored bindings).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Optional

from hermes_yaml import safe_dump as _yaml_safe_dump
from hermes_yaml import safe_load as _yaml_safe_load

from hermes_time import now as _hermes_now
from concierge import registry
from concierge.blueprints import MODEL_PRESETS, get_blueprint
from concierge.provider_groups import apply_group_to_preset as _apply_group
from cron.jobs import use_cron_store
from hermes_constants import get_hermes_home
from hermes_cli.config import read_user_config_raw
from hermes_cli.profile_channels import ChannelKeyIndex, strip_channel_env_file
from hermes_cli.profiles import create_profile, get_profile_dir, validate_profile_name
from utils import atomic_json_write, atomic_write_bytes, atomic_write_text

logger = logging.getLogger(__name__)

__all__ = ["bot_status", "create_bot", "retire_bot"]

_PROFILE_PREFIX = "bot"

# Upper bound on waiting for the per-channel provision lock. Provisioning is
# file work (profile tree, skill copies, one cron job) — seconds, not minutes —
# so a lock held this long means a wedged provision, and failing the caller is
# the honest answer (see _channel_provision_lock).
_PROVISION_LOCK_TIMEOUT_SECONDS = 60.0

# In-process half of the per-channel provision lock: a gateway thread per
# inbound event can provision concurrently. Keyed by channel id; one RLock per
# channel ever provisioned (tiny, and removing entries races waiters).
_provision_thread_locks: dict[str, threading.RLock] = {}
_provision_thread_locks_guard = threading.Lock()


def _unique_profile_name(blueprint_key: str) -> str:
    """`bot-reminders-a1b2` — readable, unique, never 'default'."""
    name = f"{_PROFILE_PREFIX}-{blueprint_key}-{uuid.uuid4().hex[:4]}"
    validate_profile_name(name)
    return name


def _provision_lock_path(channel_id: str) -> Path:
    """Lock file for one channel's provision critical section, CONFINED to the
    concierge dir. A channel id that is not a safe filename component (Discord
    snowflakes are, but this module must not trust that) is hashed — never
    embedded — so a hostile id cannot escape the directory; two distinct ids
    therefore never share a lock (under-locking would reintroduce the race)."""
    cid = str(channel_id)
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid):
        slug = cid
    else:
        slug = hashlib.sha256(cid.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return registry.registry_path().parent / f".provision-{slug}.lock"


@contextlib.contextmanager
def _channel_provision_lock(channel_id: str):
    """Serialize provisioning of ONE channel: in-process RLock (parallel
    gateway threads) plus a cross-process flock (gateway vs. `hermes concierge`
    CLI) on ``<concierge dir>/.provision-<channel>.lock``, reusing the
    registry's bounded non-blocking acquire/release helpers.

    Why: create_bot's idempotence check is read-then-act. Without this lock,
    two concurrent provisions of the same channel (a double-clicked Discord
    picker, a gateway handler racing the CLI) both observe "no binding", both
    build a profile + routine, and the loser's binding is overwritten —
    leaving a live routine that posts into the channel with NO registry entry
    pointing at it (TOCTOU). The lock covers the whole check-create-bind
    sequence, so the second caller re-checks INSIDE it and returns the
    existing bot instead.

    Fail closed: unlike the registry lock (which degrades to in-process-only
    on timeout because a torn write beats a dead registry), a provision that
    cannot take the lock RAISES TimeoutError. Provisioning anyway would risk
    exactly the duplicate this lock exists to prevent, and a holder this
    stale is a wedged provision the operator must see.
    """
    key = str(channel_id)
    with _provision_thread_locks_guard:
        thread_lock = _provision_thread_locks.setdefault(key, threading.RLock())
    with thread_lock:
        if registry.fcntl is None and registry.msvcrt is None:
            yield  # no flock backend: in-process serialization only
            return
        lock_path = _provision_lock_path(key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, "a+", encoding="utf-8")
        acquired = registry._acquire_flock(lock_fd, _PROVISION_LOCK_TIMEOUT_SECONDS)
        if acquired is False:
            lock_fd.close()
            raise TimeoutError(
                f"another provision for channel {key!r} is still holding the "
                f"provision lock ({lock_path}) after "
                f"{_PROVISION_LOCK_TIMEOUT_SECONDS:.0f}s — retry once it finishes"
            )
        if acquired is None:  # defensive: backend vanished since the guard
            lock_fd.close()
            yield
            return
        try:
            yield
        finally:
            registry._release_flock(lock_fd)  # unlocks and closes


def _seed_profile_config(profile_dir: Path, updates: dict) -> None:
    """Read-modify-write the FRESH profile's config.yaml with atomic replace.

    Safe here because we own the file from cradle: create_profile just wrote
    it (or, for ``no_skills`` profiles, not at all — a fresh no-skills profile
    has no config.yaml) and nothing serves the profile yet. Never use this on
    a live profile.
    """
    cfg_path = profile_dir / "config.yaml"
    # Sanctioned raw reader (hermes_cli.config.read_user_config_raw): this is
    # a write-back round-trip, so it must read the file EXACTLY as written —
    # load_config() would defaults-merge, and re-persisting that merge would
    # bloat the deliberately MINIMAL fresh-profile config (small context
    # window) with the full DEFAULT_CONFIG. A missing file (fresh no-skills
    # profile) reads as {}.
    cfg = read_user_config_raw(cfg_path)
    for dotted, value in updates.items():
        node = cfg
        keys = dotted.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    atomic_write_text(cfg_path, _yaml_safe_dump(cfg, sort_keys=False))


def _resolve_skill_source(source_skills_dir: Path, skill: str) -> Optional[Path]:
    """Resolve a blueprint skill's source dir: the TOP level first, then
    CATEGORY SUBDIRS — bundled skills are categorized (skills/research/,
    skills/apple/, skills/productivity/, ...), so a flat ``<root>/<skill>``
    probe alone misses most of the catalog (live evidence: 'skill
    grounded-citations not found' while it lived at
    skills/research/grounded-citations). First hit wins; the subdir scan is
    a sorted glob, so a name collision across categories resolves
    deterministically."""
    root = Path(source_skills_dir)
    direct = root / skill
    if direct.is_dir():
        return direct
    for candidate in sorted(root.glob(f"*/{skill}")):
        if candidate.is_dir():
            return candidate
    return None


def _install_skills(profile_dir: Path, skills: tuple, source_skills_dir: Path) -> None:
    dest_root = profile_dir / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    for skill in skills:
        src = _resolve_skill_source(source_skills_dir, skill)
        if src is None:
            logger.warning(
                "concierge: skill '%s' not found in %s (top level or a "
                "category subdir) — skipping",
                skill, source_skills_dir,
            )
            continue
        dst = dest_root / skill
        if not dst.exists():
            shutil.copytree(src, dst)


# Non-secret behavior tuning carried from the hub's honcho.json ROOT into the
# bot's synthesized config. Deliberately exclude everything secret or
# host-specific (apiKey variants, hosts blocks, oauth, endpoint credentials).
_HONCHO_TUNING_KEYS = (
    "workspace",
    "saveMessages",
    "sessionStrategy",
    "dialecticCadence",
    "writeFrequency",
    "recallMode",
    "observationMode",
    "dialecticReasoningLevel",
)


def _static_honcho_key(raw: dict) -> Optional[str]:
    """A usable STATIC service key from the hub's honcho.json, or None.

    Preference order (mirrors how the bot will actually resolve auth —
    _HostLookup.pick falls back to root): root ``serviceApiKey`` (a static
    service key, e.g. minted for headless use), then root ``apiKey``, then
    the apiKey of the hub's active host block (``hosts.hermes``, or
    ``defaultHost`` when the file pins one — the same resolution
    client.py::resolve_active_host applies to the default/hub profile).
    Every candidate must pass ``is_oauth_access_token`` == False: an
    ``hch-at-`` token is an OAuth access token that expires (and the
    per-profile-host refresh chain cannot follow it into the bot's file) —
    propagating one hands the bot auth that is already dead.
    """
    try:
        from plugins.memory.honcho.oauth import is_oauth_access_token
    except ImportError:
        # Upstream moved Honcho from the bundled tree to a catalog plugin
        # (``hermes plugins install honcho``). A hub honcho.json with the
        # plugin not installed degrades to "no usable key" — the same honest
        # outcome as any other unusable Honcho credential: provisioning
        # skips the memory seed instead of crashing.
        logger.warning(
            "concierge: honcho plugin not installed — memory disabled "
            "(hermes plugins install honcho)"
        )
        return None

    hosts = raw.get("hosts") or {}
    active_host = str(raw.get("defaultHost") or "hermes").strip() or "hermes"
    host_block = hosts.get(active_host) or {}
    candidates = (
        raw.get("serviceApiKey"),
        raw.get("apiKey"),
        host_block.get("apiKey"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate and not is_oauth_access_token(candidate):
            return candidate
    return None


def _install_honcho(profile_dir: Path, profile_name: str) -> bool:
    """Synthesize the bot's honcho.json from a STATIC service key — NEVER a
    byte-copy of the hub's file. Return True iff Honcho is usable for this
    bot (honcho.json written); False means the caller must skip the
    ``memory.provider: honcho`` activation seed too.

    Why not copy (M5, the live incident): Honcho OAuth grants are
    per-profile-host single-use refresh chains. The bot's profile host key
    (``hermes_bot_<slug>_<suffix>``, plugins/memory/honcho/client.py::
    profile_host_key) does not exist in a copied file, so the bot's host
    lookup falls back to the ROOT apiKey — which, when the hub logged in via
    OAuth, is an ``hch-at-`` access token that expires, with no oauth block
    to refresh it: dead auth plus user-facing re-login nagging. A
    synthesized config with ONLY a root static key works for ANY bot profile
    name (client.py::_HostLookup.pick: host -> root fallback) and never
    refreshes or expires (oauth.py::ensure_fresh_token returns (None, False)
    when no oauth block exists — a static key is permanent by design).

    The synthesized file carries: root ``apiKey`` = the static key, plus the
    non-secret behavior tuning from the hub's root (_HONCHO_TUNING_KEYS).
    Never: hosts blocks, oauth blocks, or any host-specific secret.

    When NO usable static key exists (e.g. the hub's root apiKey is an
    expired OAuth token — the incident shape), Honcho is SKIPPED for this
    bot: no honcho.json, and the caller must not seed memory.provider. A bot
    without memory is correct; a bot that nags the user about OAuth is not.
    One WARNING tells the operator how to fix it.

    The file is written with the atomic-secret mode — explicit 0o600, the
    mode the Honcho plugin's own write path (``save_config`` ->
    ``atomic_json_write``, mirrored by oauth.py::install_grant) pins; a
    umask-default write would land it 0644.
    """
    hub_cfg = get_hermes_home() / "honcho.json"
    if not hub_cfg.is_file():
        # No hub Honcho config at all: the blueprint's memory intent stays
        # unactivated rather than seeding a provider with no connection.
        return False
    try:
        raw = _yaml_safe_load(hub_cfg.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        logger.warning(
            "concierge: hub honcho.json is unreadable for bot %s — memory disabled",
            profile_name, exc_info=True,
        )
        return False
    if not isinstance(raw, dict):
        logger.warning(
            "concierge: hub honcho.json is not a config object for bot %s — memory disabled",
            profile_name,
        )
        return False
    static_key = _static_honcho_key(raw)
    if static_key is None:
        logger.warning(
            "concierge: no usable static Honcho key for bot %s — memory disabled "
            "(run honcho setup with an API key on the hub)",
            profile_name,
        )
        return False
    bot_cfg: dict = {"apiKey": static_key}
    for key in _HONCHO_TUNING_KEYS:
        if key in raw:
            bot_cfg[key] = raw[key]
    atomic_json_write(profile_dir / "honcho.json", bot_cfg, mode=0o600)
    return True


def _hub_discord_home_channel() -> Optional[str]:
    """The hub's DISCORD_HOME_CHANNEL from the hub .env (the same file
    ``_seed_credentials`` copies), or None when unset/unreadable.

    Read from the HUB's file, never the bot's copy: channel settings are
    deliberately stripped from the bot's .env (profiles are credential
    islands and a copied DISCORD_* key would hand the bot the hub's
    platform identity), so the home channel travels as CONFIG — the
    per-turn check (gateway/run_turn_prepare.py) reads the TURNING
    profile's config for a configured home channel, and concierge bots
    carry no DISCORD_* secrets by design.
    """
    hub_env = get_hermes_home() / ".env"
    if not hub_env.is_file():
        return None
    try:
        from agent.secret_scope import load_env_file

        value = (load_env_file(hub_env).get("DISCORD_HOME_CHANNEL") or "").strip()
    except Exception:
        logger.warning(
            "concierge: hub .env unreadable — bot home-channel seed skipped",
            exc_info=True,
        )
        return None
    return value or None


def _seed_credentials(profile_dir: Path) -> dict[str, list]:
    """Copy the hub's .env into the bot profile, minus channel settings.

    Profiles are credential islands (no inheritance from the hub or the
    default profile — hermes_cli/AGENTS.md), so without this every bot turn
    and routine fails auth. Model/provider keys are shared; a copied platform
    token would make two gateways fight over one bot, and inherited
    ``GATEWAY_ALLOW_ALL_USERS``/``GATEWAY_ALLOWED_USERS`` would hand the bot
    the hub's authorization — so channel settings are dropped.

    The drop is the repo's CANONICAL channel-key mechanism, not a local prefix
    list: the same ``strip_channel_env_file`` the clone path runs
    (``hermes profile create --clone`` -> ``hermes_cli.profile_channels``),
    resolved through ``ChannelKeyIndex`` in the HUB's plugin scope so the
    ownership rule (every built-in + plugin adapter's declared keys and
    ``<PLATFORM>_`` prefixes, gateway policy keys, shared-prefix adapters only
    when the hub actually runs them) is judged against the hub that owns the
    credentials. A hand-rolled prefix tuple silently misses adapters
    (Mattermost, WECOM, relay, plugin platforms), gateway policy keys,
    ``export KEY=`` lines and a BOM ahead of the first key — all four of which
    the canonical tokenizer handles (``utf-8-sig`` read, export-aware regex).

    The hub's file is never edited: the copy is written into the bot profile
    first (bytes verbatim, pinned 0o600), then stripped IN PLACE there — the
    clone path's own sequence. Comments, blank lines and every non-channel key
    survive verbatim. Returns ``{platform: [stripped env keys]}``.
    """
    hub_env = get_hermes_home() / ".env"
    if not hub_env.is_file():
        return {}
    dest = profile_dir / ".env"
    atomic_write_bytes(dest, hub_env.read_bytes(), mode=0o600)
    # One index for the whole strip: adapter discovery (platform registry +
    # gateway env-override table + shared-prefix activity) runs once, then
    # every line is judged against it.
    index = ChannelKeyIndex(get_hermes_home())
    return strip_channel_env_file(dest, index)


_RESILIENCE_KEYS = ("fallback_providers", "credential_pool_strategies")


def _copy_resilience_config(profile_dir: Path) -> None:
    """Inherit the hub's provider failover / credential-pool rotation. These are
    config.yaml keys (not secrets), copied verbatim when the hub has them set."""
    hub_cfg_path = get_hermes_home() / "config.yaml"
    if not hub_cfg_path.is_file():
        return
    # Sanctioned raw reader: copy ONLY what the hub's file actually sets.
    # DEFAULT_CONFIG declares both keys (fallback_providers: [],
    # credential_pool_strategies: {}), so a defaults-merged read cannot tell
    # "the hub chose this" from "a default leaked in" — load_config()/
    # load_config_readonly() are wrong shape for a verbatim key copy.
    hub_cfg = read_user_config_raw(hub_cfg_path)
    updates = {k: hub_cfg[k] for k in _RESILIENCE_KEYS if hub_cfg.get(k)}
    if updates:
        _seed_profile_config(profile_dir, updates)


def _pause_bot_routines(profile_name: str, profile_dir: Path) -> None:
    """Pause every ``[bot:<profile>]`` job in the bot's OWN cron store.

    Pause uses cron's own contract (``enabled=False`` + ``state="paused"`` —
    the fields ``pause_job`` writes), applied via ``update_job`` so the
    scheduler's invariants (next_run_at not cleared, terminal-state guard)
    are enforced by cron itself. Errors PROPAGATE: callers order this against
    the unbind (see retire_bot) and must know when the routine could not be
    quiesced.
    """
    from cron import jobs as cron_jobs

    prefix = f"[bot:{profile_name}]"
    with use_cron_store(profile_dir):
        for job in cron_jobs.list_jobs(include_disabled=True):
            if str(job.get("name", "")).startswith(prefix):
                cron_jobs.update_job(
                    job["id"],
                    {
                        "enabled": False,
                        "state": "paused",
                        "paused_at": _hermes_now().isoformat(),
                        "paused_reason": "bot retired",
                    },
                )


def _rollback_failed_provision(profile_name: str) -> None:
    """Tear down a half-built bot after a provision step failed. Best effort and
    NEVER raises: the caller re-raises the provision's own error, and rollback
    noise must not mask it. The channel binding is the LAST thing create_bot
    writes, so at this point no registry entry points at the profile — delete
    it, or its routine would keep firing into the channel with no way to
    retire it through the Discord surface.

    Uses the canonical profile teardown (``delete_profile``: service cleanup,
    multiplexer notify, tombstone, identity purge), falling back to nothing
    more aggressive than a logged manual-removal instruction — a bare rmtree
    would strand a served-profile entry in a live multiplexer and skip
    identity settlement.
    """
    try:
        profile_dir = get_profile_dir(profile_name)
    except Exception:
        return  # the profile was never created: nothing on disk to clean
    if not profile_dir.is_dir():
        return
    try:
        # Pause first (retire order): a scheduler in another process may hold
        # the store's jobs in memory; a persisted pause is what stops it.
        _pause_bot_routines(profile_name, profile_dir)
    except Exception:
        logger.error(
            "concierge: rollback of failed provision %s could not pause its "
            "routine(s) — deleting the profile anyway",
            profile_name, exc_info=True)
    try:
        from hermes_cli.profiles import delete_profile as _delete_profile

        _delete_profile(profile_name, yes=True)
    except Exception:
        logger.error(
            "concierge: rollback of failed provision %s could not delete the "
            "profile — remove it manually with: hermes profile delete %s",
            profile_name, profile_name, exc_info=True)


def create_bot(
    blueprint_key: str,
    *,
    channel_id: str,
    guild_id: Optional[str] = None,
    provider_group: Optional[dict] = None,
    source_skills_dir: Optional[Path] = None,
    display_name: Optional[str] = None,
) -> dict:
    """Create (or return the existing) bot bound to ``channel_id``.

    Idempotent per channel: a channel never gets two bots. The whole
    check-create-bind sequence runs under a per-channel provision lock — the
    idempotence check is read-then-act, and two concurrent provisions of one
    channel (a double-clicked picker, a gateway handler racing the CLI) would
    both pass it and orphan the loser's routine (TOCTOU). Re-provisioning a
    channel with a DIFFERENT blueprint retires and DELETES the old bot first —
    its binding is being replaced, so a parked-paused profile would leak one
    orphan per reprovision. The provision itself is TRANSACTIONAL: any failure
    after the profile exists rolls the whole bot back and re-raises the
    original error — a half-built bot whose routine still fires is worse than
    a clean failure the user can retry. Returns the binding, annotated with
    ``already_provisioned: True`` when the channel's existing bot (same
    blueprint) was returned as-is instead of being created.
    """
    with _channel_provision_lock(channel_id):
        existing = registry.get_binding(channel_id)
        if existing and existing["blueprint"] == blueprint_key:
            return {**existing, "already_provisioned": True}
        if existing:
            # Reprovision with a different blueprint: retire AND delete — the
            # old bot's binding is being replaced, and its routines would
            # keep firing into this channel forever otherwise.
            retire_bot(existing["profile"], delete_profile=True)

        bp = get_blueprint(blueprint_key)
        preset = MODEL_PRESETS[bp.model_preset]
        model = _apply_group(provider_group, preset)
        profile_name = _unique_profile_name(blueprint_key)

        try:
            # 1) profile — empty (no bundled skills): minimal context window
            profile_dir = create_profile(
                profile_name, no_skills=True, description=display_name or bp.title
            )

            # 2) pin the model + allowlist the toolsets + activate long-lived
            #    memory when the blueprint calls for it
            config_seeds = {
                "model.default": model.model,  # canonical pin (config.py::_normalize_root_model_keys)
                "model.provider": model.provider,
                "toolsets": list(bp.toolsets),  # top-level allowlist key (CLI/local lane)
                # The key the DISCORD lane actually reads: a bot's turns arrive
                # through the hub's Discord adapter, and
                # gateway/run_turn.py::_resolve_enabled_toolsets_for_source
                # resolves them via hermes_cli.tools_config._get_platform_tools(
                # config, 'discord') — which reads ``platform_toolsets.discord``
                # and falls back to the platform's ~17-set default composite
                # when the key is absent (live bug bot-reminders-94d3: a
                # ``toolsets: [todo]`` bot ran terminal/browser/delegation).
                # The top-level seed above never reaches that lane.
                "platform_toolsets.discord": list(bp.toolsets),
                # The key the CRON lane reads: a tool-created job carries
                # enabled_toolsets=None, so its fire resolves toolsets via
                # cron/scheduler.py::_resolve_cron_enabled_toolsets ->
                # _get_platform_tools(cfg, 'cron'), which reads
                # ``platform_toolsets.cron`` and falls back to the cron
                # platform's ~17-set composite default — the live bloat of job
                # c4bcac5a9d5d's tick ("tool_search activated (tier 1): 17
                # core/visible tools kept" on a single-tool-call bot). Seed it
                # next to the Discord lane's key.
                "platform_toolsets.cron": list(bp.toolsets),
                # On-rails bot behavior (live E2E 2026-09-27):
                # - Low reasoning: with NO effort pinned, GLM-5.3-flash thinks
                #   at the server default — a runaway 900s/2.78MB stream on a
                #   trivial reminder. Bots are single-tool-call agents; 'low'
                #   is the GLM-5.3 vocabulary's floor (GLM53_EFFORTS) and the
                #   canonical key read via hermes_constants.resolve_reasoning_config
                #   (per-model overrides > this global).
                "agent.reasoning_effort": "low",
                # - Verbatim cron delivery: scheduler_delivery.py reads
                #   ``cron.wrap_response`` (default True) and wraps routine
                #   output in 'Cronjob Response: <name> (job_id: ...)' +
                #   'to stop or manage' scaffolding the user must never see.
                #   False delivers the model's final text bare.
                "cron.wrap_response": False,
                # - Answer-only bots (live E2E 2026-09-27): with no
                #   ``display:`` section, per-turn display resolution
                #   (gateway/run_turn.py::_resolve_turn_display) falls back to
                #   the Discord platform tier default — TIER_HIGH — which posts
                #   intermediate chatter into the bot's channel: ⚙️ tool
                #   progress, ⏳ 'Working — N min' notices, and mid-turn
                #   interim assistant messages. Seed False for all three under
                #   display.platforms.discord: the tristate normaliser maps
                #   False → 'off' (gateway/display_config.py::_NORMALISERS),
                #   consumed at gateway/run_turn.py:3013
                #   (``tool_progress_enabled = progress_mode not in
                #   {"off","log"}``); the final answer is still delivered.
                "display.platforms.discord.tool_progress": False,
                "display.platforms.discord.long_running_notifications": False,
                "display.platforms.discord.interim_assistant_messages": False,
            }
            # The per-turn home-channel check reads the TURNING profile's
            # config; the launch config's platforms.discord.home_channel does
            # NOT reach the bot-scoped check (verified live: bots provisioned
            # after the launch-config fix still got the 'No home channel is
            # set for Discord' notice). Seed the hub's home channel into the
            # BOT's config when it has one; skip the key when it doesn't.
            hub_home_channel = _hub_discord_home_channel()
            if hub_home_channel:
                # PlatformConfig.from_dict only materializes home_channel
                # from the DICT shape (HomeChannel.from_dict; a bare string
                # is silently dropped — gateway/config.py) — and
                # persist_home_channel writes exactly this shape. Match it.
                config_seeds["platforms.discord.home_channel"] = {
                    "platform": "discord",
                    "chat_id": str(hub_home_channel),
                    "name": "Home",
                }
            # 5) long-lived memory: Honcho — SYNTHESIZED from a static service
            #    key (never a byte-copy of the hub's OAuth-bearing file; see
            #    _install_honcho). The install runs BEFORE the config seed
            #    consults its verdict: when no usable static key exists the
            #    bot gets NO honcho.json AND NO memory.provider activation —
            #    a bot without memory is correct; a bot that nags the user
            #    about OAuth re-login is not.
            honcho_installed = (
                bp.memory_provider == "honcho"
                and _install_honcho(profile_dir, profile_name)
            )
            if honcho_installed:
                # The Honcho ACTIVATION key (Task 9.1, verified against the
                # plugin's load chain — not guessed): ``memory.provider`` is
                # what agent/agent_init.py reads
                # (``mem_config.get("provider")`` -> plugins.memory.
                # load_memory_provider) and what DEFAULT_CONFIG registers
                # (hermes_cli/config_defaults.py, "memory" -> "provider").
                # The honcho.json written just above is CONNECTION config only —
                # without this key the plugin never loads and the bot remembers
                # nothing across sessions.
                config_seeds["memory.provider"] = "honcho"
            _seed_profile_config(profile_dir, config_seeds)

            # 3) SOUL.md from the blueprint template. The {name} is the bot's own
            #    identity: the user's display name when given, else the blueprint's
            #    internal slug — never the Discord picker copy (title/description),
            #    which would leak the marketing voice into the persona (Task 4.2 test).
            atomic_write_text(
                profile_dir / "SOUL.md", bp.soul.format(name=display_name or bp.key)
            )

            # 4) exact skills, nothing more
            _install_skills(
                profile_dir,
                bp.skills,
                Path(source_skills_dir) if source_skills_dir else get_hermes_home() / "skills",
            )

            # 5) long-lived memory: Honcho — installed above (before the config
            #    seed, whose memory.provider activation depends on its verdict)

            # 5b) credentials: profiles are credential islands (no inheritance) — seed
            #     provider keys from the hub, minus every channel setting (the clone
            #     path's canonical strip, judged in the hub's scope)
            stripped = _seed_credentials(profile_dir)
            if stripped:
                logger.debug(
                    "concierge: bot %s did not inherit channel settings: %s",
                    profile_name, {pid: keys for pid, keys in stripped.items()},
                )

            # 5c) resilience: inherit the hub's provider failover + credential-pool
            #     rotation (config.yaml keys, not secrets) so N bots sharing one quota
            #     don't 429 together and routines survive a provider outage
            _copy_resilience_config(profile_dir)

            # 6) routine -> namespaced cron job IN THE BOT PROFILE'S OWN cron store.
            #    cron/jobs.py header: each profile owns <profile_home>/cron/jobs.json and
            #    a job runs with the .env/config/skills of the profile whose store holds
            #    it; cross-profile callers must scope with use_cron_store(). Creating the
            #    job in the hub's store would run it with the HUB's soul/skills.
            if bp.routine:
                from cron.jobs import create_job

                with use_cron_store(profile_dir):
                    create_job(
                        prompt=bp.routine.prompt,
                        schedule=bp.routine.schedule,
                        name=f"[bot:{profile_name}] {bp.routine.name}",
                        model=model.model,
                        provider=model.provider,
                        enabled_toolsets=list(bp.toolsets),
                        monitor_script=bp.routine.monitor,  # None = always run (default)
                        deliver="origin",
                        origin={"platform": "discord", "chat_id": str(channel_id)},
                    )

            # 7) bind LAST: until this succeeds, the profile is uncommitted —
            #    any failure above (or in the bind itself) rolls it all back.
            return registry.bind_channel(
                channel_id,
                profile=profile_name,
                blueprint=bp.key,
                guild_id=guild_id,
                provider_group=(provider_group or {}).get("name"),
            )
        except BaseException:
            _rollback_failed_provision(profile_name)
            raise


def retire_bot(profile_name: str, *, delete_profile: bool = False) -> None:
    """Pause routines, unbind every channel; optionally delete the profile.

    ORDER (M4 fix): pause BEFORE unbind, delete LAST. If the process dies (or
    a step raises) mid-retire, the binding still exists and points at a bot
    whose routine is already quiesced — a retry (or a manual unbind) finishes
    the job, and nothing keeps firing. The previous order (unbind, then pause)
    could die between the two and leave a LIVE routine with NO registry entry:
    unretireable through the Discord surface and posting into the channel
    forever.

    Pause uses cron's own contract (``enabled=False`` + ``state="paused"`` —
    the fields ``pause_job`` writes), applied via ``update_job`` so the
    scheduler's invariants (next_run_at not cleared, terminal-state guard)
    are enforced by cron itself. A missing profile dir is tolerated: unbinding
    still happens, and there is no store to pause.
    """
    try:
        profile_dir = get_profile_dir(profile_name)
    except Exception:
        logger.warning(
            "concierge: retire_bot — profile dir for %r unresolvable; "
            "unbinds still happen, nothing to pause or delete",
            profile_name,
        )
        profile_dir = None
    if profile_dir is not None and profile_dir.is_dir():
        # 1) PAUSE while the binding still exists: a crash from here on leaves a
        #    quiesced, still-bound bot (retryable), never a live orphan.
        _pause_bot_routines(profile_name, profile_dir)
        # 2) Optional delete — the store is gone with the dir, so pause had to
        #    happen first (and the canonical delete_profile teardown handles
        #    services, the multiplexer and identity settlement). A failure here
        #    leaves the binding in place pointing at the paused bot: retryable.
        if delete_profile:
            from hermes_cli.profiles import delete_profile as _delete_profile

            _delete_profile(profile_name, yes=True)

    # 3) UNBIND LAST — the binding is the commit pointer: it is removed only
    #    after the bot can no longer fire and its profile is gone. Any earlier
    #    crash leaves a bot the registry still knows about and a retry can
    #    finish (pause and delete are both idempotent). Channels bound to a
    #    ghost profile (no dir at all) still unbind.
    for channel_id in registry.channels_for_profile(profile_name):
        registry.unbind_channel(channel_id)


def bot_status(profile_name: str) -> dict:
    """One bot's channels + routine state, for /bot list."""
    try:
        profile_dir = get_profile_dir(profile_name)
        from cron import jobs as cron_jobs

        with use_cron_store(profile_dir):
            jobs = [
                j
                for j in cron_jobs.list_jobs(include_disabled=True)
                if str(j.get("name", "")).startswith(f"[bot:{profile_name}]")
            ]
    except Exception:
        jobs = []
    return {
        "profile": profile_name,
        "channels": registry.channels_for_profile(profile_name),
        "routines": [
            {"name": j["name"], "paused": not j.get("enabled", True)} for j in jobs
        ],
    }
