# Changelog

All notable public changes are documented in this file.

## 0.5.0-beta.4 - 2026-07-30

- Add shared per-fixture color state synchronized between the standard light
  entity and the new numeric controls.
- Add a dedicated brightness control for every fixture with 0.1% resolution.
  It uses the Mesh brightness command without changing the active CCT or HSI
  mode.
- Add a color-temperature control for every fixture with a fixed 100 K step.
- Add G/M (`-10` to `+10`), hue (`1 degree`) and saturation (`1%`) controls
  only when the imported fixture capabilities support them.
- Preserve companion color channels when one numeric control is changed.
- Quantize standard Home Assistant CCT commands to the fixture's 100 K
  resolution and preserve the current G/M compensation.
- Restore color state after Home Assistant restarts and update all related
  entities from physical Mesh status reports.
- Keep the native Home Assistant device UI. Its entity list is sorted by the
  localized display name rather than integration creation order.
- Retain recursive Mesh-key redaction and MAC-address pseudonymization in
  public diagnostics.
- Pass 136 offline tests, including the public diagnostics privacy tests.

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
