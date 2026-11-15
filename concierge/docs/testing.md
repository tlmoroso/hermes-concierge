# Testing

- ALWAYS `scripts/run_tests.sh <paths>` (never bare pytest). Tests import
  `hermes_yaml`, never third-party `yaml`.
- Known machine artifact: tests importing `gateway.run` hit the home-io guard
  (checkout lives inside `~/.hermes`; `payload_venv` probes
  `<checkout>/../manifest.json`). Byte-identical on pristine baseline — verify
  with changes stashed before excluding anything.
- Test-rig traps (each cost a debugging cycle): MagicMock enums mint a NEW
  member per attribute access — bind `discord.MessageType.default` once at
  module scope; patching `discord.Thread` makes stubs Thread-instances, which
  thread-exemptions then bypass — use plain marker classes.
- Rig fixtures stub `hermes_bootstrap`, which is why gateway-concierge suites
  run clean on this checkout while unrelated gateway suites hit the artifact.
- TDD discipline: RED for the RIGHT reason first (read the failure), then GREEN,
  then the suite gate. One commit per logical fix.
