# Changelog

All notable changes to the Orion Sleep integration are recorded here. The
API client that this integration depends on has its own changelog in the
`orion-sleep-api` package.

## 3.1.0

Adds API key authentication, the Orion Intelligence analytics suite, and
per-sleeper temperature recommendations. Several surfaces in this release
were discovered by Kevin Klaes in his fork; each is credited below and in
the commit that adds it. Where his code was ported closely the commit
carries a `Co-authored-by` trailer. Where only the surface was his and the
implementation is independent, he is credited by name without the trailer.

### Added

- **API key authentication** as a third auth method alongside OTP and the
  partner link. A key is validated against `/v1/auth/me` at config time, a
  bad key is rejected as `invalid_api_key`, and key entries reauth back to
  the key step rather than the OTP step. Keys are never written to any
  commit, log, fixture, or diagnostics bundle; diagnostics redaction now
  matches the `os_live_` key shape explicitly.
- **Orion Intelligence `/v3/insights` sensors.** Day-granularity
  Consistency, Sleep Debt, and Breathing Disturbances, plus one weekly and
  one monthly sleep-score sensor carrying the full metric breakdown as
  attributes. Partner variants of all five. A metric reports `unknown` on
  an empty or calibrating period and on a no-subscription account, never
  `0`, so zero sleep debt and no data stay distinguishable. The v3
  breathing roll-up cross-references the existing v2 apnea AHI as an `ahi`
  attribute rather than duplicating it. The v3 surface was found by Kevin
  Klaes.
- **Temperature-recommendations sensor**, one per sleeper. State is the
  recommendation count (0 is a valid state, not an error); the typed items
  ride as an attribute. Unavailable only when the recommendations key is
  absent. The recommendations field was found by Kevin Klaes, who had only
  ever observed it empty; its populated item schema is measured live here.
- **`capabilities` documentation** on the Device schema in `openapi.yaml`
  (sensors / alarms / accessories), measured live. Found by Kevin Klaes.
- **Release workflow** that tags a GitHub release on a manifest version
  bump, so HACS sees new versions. Mechanism adopted from Kevin Klaes.

### Fixed

- CI installed a hardcoded `orion-sleep-api==0.1.0` while the manifest
  pinned a newer client, so the test jobs ran against a client from before
  API key auth and v3 insights existed. Both install steps now derive the
  pin from the manifest and cannot drift again.

### Verified, not changed

- Graphable sleep-stage sensors. Kevin added raw-minute stage sensors for
  Influx and Grafana; this integration already ships total / deep / rem /
  light / awake minute sensors with a measurement state class alongside the
  app-style duration strings, so nothing was added.
