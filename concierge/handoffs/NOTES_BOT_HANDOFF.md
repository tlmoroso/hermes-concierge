# Notes Bot handoff — Zettelkasten vault + Syncthing sync

**Status: DESIGN APPROVED, NOT BUILT.** This document is the complete handoff for
the agent that builds it. The operator (Tyler) approved every decision below on
2026-10-30 — implement as specified; revisit nothing unless a decision is
factually broken. Read `concierge/AGENTS.md` first (repo rules, seams, testing,
deployment, snapshot discipline).

## 0. Decisions already made (do not revisit)

1. **Sync = Syncthing**, NOT git/GitHub. Rationale: reproducible for multiple
   users with no third-party data holder and no GitHub dependency.
2. **Vault location = a neutral path OUTSIDE personal folders.** Approved path:
   `~/hermes-vault` on the Mac, `/root/hermes-vault` on the VM (parameterize
   everything through `OBSIDIAN_VAULT_PATH`; never hardcode). Deliberately NOT
   `~/Documents/Obsidian Vault` (the skill's fallback) and NOT in iCloud.
3. **No pre-defined sections.** The method (Zettelkasten) is prescribed; the
   taxonomy is not. Budget/rolodex/contacts-style sections EMERGE when topic
   clusters form — the bot creates a MOC at that moment, never before. Do not
   scaffold `People/`, `Budget/`, etc.
4. **There is currently NO vault at all** (verified 2026-10-30: no
   `OBSIDIAN_VAULT_PATH` anywhere, no vault at the fallback path, nothing in
   iCloud). You are creating it from scratch.
5. The notes blueprint's toolsets are ALREADY correct: `('file',)` (commit
   93dbcabd925 — the obsidian skill drives file tools; `hermes-cli` was a
   composite that leaked 17 toolsets). Skill: `('obsidian',)`; skill copying
   from category subdirs was fixed in 2ec0d7f4d7b.

## 1. What to build (in order)

### 1.1 The vault skeleton (Mac first)

```
~/hermes-vault/
├── CONVENTIONS.md   # THE rulebook — the bot reads this; data, not law
└── Home.md          # the root MOC (index hub) — starts nearly empty
```

That is all. `CONVENTIONS.md` must specify the METHOD only:

- **Atomicity:** one claim / fact / idea / event per note. Long captures are
  SPLIT into a sequence of atomic notes linked to each other.
- **Search before create:** look for an existing note on the claim; update and
  relink instead of duplicating.
- **Titles:** human-readable unique slugs (`jane-doe-accountant.md`, not
  timestamps); disambiguate with a suffix when needed.
- **Connectivity (the load-bearing rule):** every new note carries wikilinks to
  (a) at least one MOC and (b) every related note it touches. A note that links
  to nothing is not done — revise until it connects.
- **Emergent structure:** when a topic cluster forms (≈3+ related notes), create
  a MOC for it (named from the domain, e.g. `MOC/Budget.md`), link it from
  `Home.md`, and backlink members. MOCs update on EVERY write in their domain.
- **Frontmatter:** `created:`, `type: zettel|moc`, `tags:` — keep it minimal.
- **People are nodes:** a person note emerges on first mention; meetings and
  decisions link `[[person-note]]` — the graph is the memory, folders are
  convenience.
- **Answering questions = graph traversal:** search, then follow links
  (both directions); never just grep-and-guess.

`Home.md` starts as a stub MOC ("the index of indexes") and grows.

### 1.2 The SOUL rewrite (code change)

`concierge/blueprints.py`, notes blueprint. Keep the SOUL SHORT (minimal-context
invariant — see AGENTS.md §11); it should describe the PERSONA (knowledge
gardener: captures atomic notes, connects them, tends MOCs, answers from the
graph) and point at the vault's `CONVENTIONS.md` as the rulebook — so the method
is tunable as data without a code change. Do not inline the whole method into
the SOUL. Light test pin: assert the SOUL mentions `CONVENTIONS.md`.

### 1.3 Syncthing wiring (the reproducible recipe)

- **Mac:** `brew install syncthing`, `brew services start syncthing` (the web
  GUI is 127.0.0.1:8384). NOTE for the building agent: this session's guards
  block `launchctl`-adjacent operations — `brew services` must run from the
  operator's Terminal (hand them the one-liner).
- **VM:** `apt-get install -y syncthing`, `systemctl enable --now
  syncthing@root`, folder `/root/hermes-vault`.
- **Pairing:** exchange device IDs (each GUI shows one), share the
  `hermes-vault` folder Mac↔VM, accept on both sides. This one-time exchange
  is the only manual step — everything else is scriptable for new users.
- **Network:** the VM must accept syncthing's sync port (22000/tcp + udp).
  Hetzner applies no cloud firewall by default; check `ufw status` on the VM
  and open 22000 if needed. Global discovery/relays will otherwise still work
  (slower); direct is preferred.
- **Versioning:** enable staggered file versioning on the folder (both sides) —
  free safety net against bot mistakes.
- **Conflicts:** concurrent edits produce `.sync-conflict.*` files.
  `CONVENTIONS.md` should state the bot's posture: on noticing a conflict file,
  read both, merge into one note, and delete the conflict copy — report the
  merge to the user. Single-writer is NOT assumed.

### 1.4 Env wiring

Set `OBSIDIAN_VAULT_PATH` to the ABSOLUTE vault path in BOTH hub `.env` files
(Mac local hub if notes ever runs there; the VM hub `.env`). Provisioning copies
non-messaging env vars into every bot automatically (`_seed_credentials` strips
only messaging prefixes) — so the Notes Bot inherits its vault path with zero
extra code. The obsidian skill documents and reads `OBSIDIAN_VAULT_PATH`.

### 1.5 Deploy + E2E

1. Commit the blueprint/SOUL change (TDD, lint gate, explicit adds).
2. Snapshot-push to `personal` (AGENTS.md §7 — end of every session).
3. Deploy: `~/.hermes/concierge-deploy.sh` (rsync + VM gateway restart).
4. Spawn the Notes Bot from Discord (managed category → picker → Notes).
5. E2E from the phone, laptop-closed: "note that Jane Doe is my accountant,
   she prefers email" → verify on the Mac side (syncthing pulled it) that a
   Jane zettel exists, links into Home, and is atomic. Then later ask the bot
   "who's my accountant?" → a graph-traversed answer, not a raw grep.

## 2. Multi-user reproducibility (the reason Syncthing was chosen)

The whole chain must onboard a NEW user with only per-user parameters:
vault path (env), a syncthing pairing (device IDs), and a fresh vault skeleton
(CONVENTIONS.md + Home.md — same method, zero content). Nothing in the recipe
is Tyler-specific. If a second user's notes bot is ever provisioned, this file
+ `concierge/DEPLOY.md` is the runbook. Consider a `vault-init` helper or a
setup script later if onboarding happens more than twice.

## 3. Pitfalls specific to this build

- **Obsidian the app is OPTIONAL.** The vault is plain markdown; the operator
  can open `~/hermes-vault` as a vault in the Obsidian desktop app anytime
  ("Open folder as vault"). Do not couple anything to the app; do not let the
  app's own sync plugins near it (no iCloud path — decision #2).
- **The SOUL must stay small.** The method lives in `CONVENTIONS.md` (vault
  data), not the SOUL (prompt context). Bloat here costs every single turn.
- **No sections means NO sections.** Resist scaffolding folders during
  skeleton creation — the first MOC should be born from real usage.
- The skill's fallback path (`~/Documents/Obsidian Vault`) must NEVER silently
  win: `OBSIDIAN_VAULT_PATH` must be set before the bot's first turn, and the
  E2E should assert the note landed in `~/hermes-vault` (not the fallback).
- Syncthing's first sync of an empty folder is instant; verify both sides agree
  (compare a marker file's content hash) before the E2E, so a bot write is
  never mistaken for a sync gap.

## 4. Definition of done

Vault skeleton + conventions + SOUL committed; syncthing paired and syncing;
`OBSIDIAN_VAULT_PATH` set on the VM hub; blueprint tests + full concierge
suite green; deployed; Notes Bot spawned on the VM; phone E2E (capture →
atomic linked note visible on the Mac → recall question answered from the
graph); fresh snapshot pushed to `personal`.
