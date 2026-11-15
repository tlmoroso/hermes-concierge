# Standing working patterns (operator's preferences)

- Delegate implementation to GLM-5.3-flash subagents, review with GLM-5.3
  (cheap builders, strong checkers); TDD; escalate to the operator only for
  real blockers (API keys, config decisions, live E2E gates).
- Minimal contexts everywhere: 1–3 toolsets per blueprint, small SOULs,
  `reasoning_effort: low`, answer-only display
  (`tool_progress/long_running_notifications/interim_assistant_messages: false`),
  `cron.wrap_response: false`. Bots should be fast single-tool-call flows.
- Never expose Hermes internals in bot personas; never print secret values
  (read `.env` by key name only).
- Thin duck-typed hooks over core surgery; core must never import
  `concierge/`. Fail-closed: routing degrades to the hub; resolver exceptions
  return None; wrap failures delegate.
- Registry is the single routing source of truth; never duplicate it into
  config routes (rooms make that impossible anyway — same-specificity routes
  raise `admission_conflict`).
