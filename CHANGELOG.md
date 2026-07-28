# Changelog

All notable public changes are documented in this file.

## 0.4.4-beta.1 - 2026-07-28

First public beta.

- Import an existing Amaran Desktop / Sidus Mesh and expose multiple fixtures
  as Home Assistant light entities.
- Validate T2c, 200x and 300c capability profiles in a four-fixture Mesh.
- Add a control switch that acquires a persistent Mesh Proxy connection and
  releases it for official applications when disabled.
- Add candidate health tracking, failover, GATT cooldowns and post-open
  quarantine.
- Separate the 30-second operational window from the 120-second trusted-proxy
  window.
- Classify disconnects during the trusted-proxy probation period and quarantine
  the candidate for 300 seconds.
- Add periodic state refresh, conservative availability tracking and connection
  diagnostics.
- Recursively redact Mesh keys and pseudonymize MAC addresses in downloaded
  diagnostics.
- Add HACS metadata, English documentation, automated tests, HACS validation
  and Hassfest workflows.

## Private development milestones

Versions before `0.4.4-beta.1` were private development builds and were not
published as releases:

- `0.4.0`: four-fixture import and per-model capability validation.
- `0.4.1`: candidate cooldown persistence and unsupported-state restoration
  fixes.
- `0.4.2`: bounded GATT candidate attempts and stale cooldown cleanup.
- `0.4.3`: post-open quarantine and extended failover validation.
- `0.4.4`: trusted-proxy probation baseline used by this public beta.
