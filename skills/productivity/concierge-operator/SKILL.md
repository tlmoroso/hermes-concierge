---
name: concierge-operator
description: "Create, list, or retire Concierge helper bots by channel."
version: 0.1.0
author: Tyler Moroso, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Concierge, Bots, Discord, Productivity]
    related_skills: [google-workspace, email-inbox-triage, obsidian, grounded-citations, himalaya]
---

# Concierge Operator

Provision and retire the user's Concierge helper bots — the per-channel assistants created from the blueprint catalog. This skill is the only sanctioned management surface: it drives the `hermes concierge` CLI through the `terminal` tool and never edits bot profiles, the registry, or cron jobs by hand.

## When to Use

- The user — the Concierge hub persona in Discord, or a human at a terminal — asks for a new bot: "set up a reminders bot here", "I need something watching my inbox".
- The user asks for a change of bots: "replace this channel's bot with a researcher".
- The user asks what bots exist or where they live: "what bots do I have?"
- The user asks to remove a bot: "retire the notes bot".

Don't use for: chatting with an existing bot (just reply in its channel), or creating plain Hermes profiles — `hermes profile create` is unrelated to bot provisioning.

## Prerequisites

- The `hermes` CLI on PATH, run from the Hermes home that owns the concierge registry (the hub's). The gateway may run on another host; these commands belong wherever the registry lives.
- A channel id. It comes from the session's platform context — in Discord, the channel the conversation is happening in. Never ask the user for a raw id.

## How to Run

Every command runs through the `terminal` tool:

```bash
hermes concierge new <blueprint> --channel <channel-id>
hermes concierge list
hermes concierge retire <profile> [--delete]
hermes concierge bindings
```

`new` also takes `--guild <id>` (provider-group matching) and `--name <display>` (the bot's persona name). Bare `hermes concierge` means `list`. Every command exits 0 on success and 1 with an operator-readable error line.

## Quick Reference

Blueprint keys and their catalog titles — the source of truth is `concierge/blueprints.py`:

| Key | Title | Does |
| --- | --- | --- |
| `reminders` | Reminder Bot | Keeps track of your daily tasks and pings you when they're due. |
| `scheduler` | Scheduler Bot | Reports on upcoming meetings and events so you always know what's next. |
| `researcher` | Researcher Bot | Finds things out and writes you a short, cited brief. |
| `email_triage` | Email Triage Bot | Watches your inbox for emails that need action from you and turns them into to-dos. |
| `notes` | Notes Bot | Keeps notes for you and remembers people, meetings, and details. |

## Procedure

### 1. Map the need to one blueprint

Pick the single key whose job matches the request (tasks and pings → `reminders`; calendar and events → `scheduler`; look things up → `researcher`; act on email → `email_triage`; capture people and meetings → `notes`). If nothing fits, say so and offer the closest fit instead of inventing a bot type.

### 2. Resolve the channel id

Take the id from the session's platform context — in Discord, the channel the conversation is happening in. If there is genuinely no channel in context (a human at a bare terminal), ask which channel, then proceed.

### 3. Provision

Run `hermes concierge new <key> --channel <id>`, adding `--name` when the user gave the bot a name. The output names the profile (`bot-<key>-<suffix>`), the channel, the blueprint, and the routine (if any) with its schedule. "Already provisioned" means the channel already had this exact bot — nothing was created.

### 4. Verify

`hermes concierge list` shows every bot with its channel(s) and routine state; `hermes concierge bindings` prints the raw channel → profile table. The new channel must appear in both.

### 5. Retire or replace

Retire with `hermes concierge retire <profile>`, passing the profile name exactly as `list` shows it. Retire unbinds the channel(s) and pauses the bot's routine; the profile is kept by default (reversible) and removed for good with `--delete`. To replace a channel's bot, just run `new` with the different blueprint — see Pitfalls for what that does.

## Pitfalls

- **Bots are per-channel.** One channel holds at most one bot. `new` on a channel that already has the same blueprint is idempotent and returns the existing bot.
- **Re-provisioning a channel with a different blueprint replaces the bot.** The CLI retires and deletes the old bot, then creates the new one in the same channel. Confirm with the user before replacing a bot that has history.
- **`retire` only works on Concierge bots.** The CLI refuses any profile that is not a provisioned bot (a registry binding or a `bot-`-prefixed profile) — `default` and plain human profiles are rejected. Always copy the profile name from `hermes concierge list`.
- **Never manage bots by hand.** Editing the registry JSON, bot profiles, or `[bot:<profile>]` cron jobs directly breaks the invariants the CLI maintains. If the CLI can't do it, it doesn't get done that way.
- **Don't confuse listing surfaces.** `list` groups by bot profile; `bindings` is the flat channel → bot table. Both read the same registry — use `bindings` to answer "which bot is this channel".

## Verification

- After `new`: exit code 0, and the channel appears in `hermes concierge bindings` mapped to a `bot-`-prefixed profile.
- After `retire`: the channel no longer appears in `hermes concierge bindings`, and the retired profile is gone from `hermes concierge list` (present but unbound never happens — retire unbinds first).
