# Voice front-door

- **Flow:** spoken input `_handle_voice_channel_input` (run_voice.py) → duck-typed
  `_restate_voice_routing_stamps` (concierge_voice.py — re-applies room/binding registry
  verdicts that `clear_identity` + source serialization drop; without it spoken input ran a
  plain hub-lane turn) → the wrap → fan-out. Member lanes speak through the BASE auto-TTS
  chain; `ConciergeVoiceMixin` overrides `_wants_auto_tts`/`_synthesize_auto_tts` (called
  back-to-back at base.py:~4505-07, NO await between — the stash invariant; drift = silent
  verbatim, watch for a debug log) to swap in aux-LLM summaries.
- **Config (launch home):** `concierge.voice_relay: {mode: summary|verbatim|off (default off),
  max_chars: 400}` — garbage resolves to off/400 (fail-closed). The aux model pins under
  `auxiliary.voice_relay`; CoT-only responses are rejected (verbatim fallback), never spoken.
- **Suites:** test_discord_concierge_voice_ingress / _lane_delivery / _relay.py (32 tests).
- **VM deps:** libopus0 + ffmpeg (apt) gate VC playback — the "Opus codec not found" log line
  IS the gate. STT/TTS ride `stt.provider`/`tts.provider: nous` (Portal OAuth, no static keys).
- **Deploy gotcha:** the local-session guard blocks concierge-deploy.sh (it pattern-matches
  the REMOTE restart inside the script) — rsync manually with the same excludes, then pipe the
  restart over SSH stdin (§6 pattern). Deploy excludes now include `venv/` (a stale
  Mac-binary venv/ had shipped to the VM).
- **Voice auto-join (`concierge.voice_auto_join`, 2026-11-14, ships OFF):** an
  allowlisted user JOINING a Discord voice channel makes the bot auto-join
  it via the programmatic `join_voice_channel` seam with the transcription
  binding pointed at THE JOINER'S home channel — spoken input then routes to
  their orchestrator through the EXISTING machinery (restamp → lane → relay),
  zero new routing logic. Gate ladder in `_concierge_maybe_auto_join_voice`
  (concierge_mixin.py): events-wanted + flag + join-event + human + allowed
  (`_allowed_user_ids`, receiver parity) + no voice client in the guild (one
  per guild, second joiner ignored, no moves) + LIVE orchestrator
  (`_profile_exists` posture) + home channel. The join runs as a background
  task, warns ONCE per (guild, user) on failure, never persists a voice mode
  (default-state path), and is dispatched from `on_voice_state_update`'s
  duck-typed TAIL (fail-safe, after the DAVE bookkeeping). Manual
  `/voice join` unchanged. Suite:
  tests/gateway/test_discord_concierge_voice_auto_join.py.
