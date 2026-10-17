# Changelog

All notable public changes are documented in this file.

[繁體中文更新日誌](CHANGELOG.zh-TW.md)

## Unreleased

- Only offer color temperature controls to fixtures whose imported CCT range
  is adjustable. Fixed-white fixtures (`min_kelvin` equal to `max_kelvin`)
  now expose a brightness-only light entity and no color temperature number
  entity, and a leftover color temperature entity from an earlier release is
  removed from the entity registry on startup.
- Reject imported fixtures whose `min_kelvin` is larger than `max_kelvin`.
- Retry a Mesh Proxy candidate with full GATT service discovery when it
  rejects the built-in Telink handle table during notification setup (for
  example ESPHome `error=135 Illegal parameter` on handle 53). Previously such
  fixtures with incompatible layouts could keep rotating without ever trying
  actual service discovery through an ESPHome Bluetooth Proxy. Adds the
  `bootstrap_gatt_profile_rejected` diagnostics hint and the
  `bootstrap_gatt_cache_rejected` and `bootstrap_gatt_cache_fallbacks`
  diagnostics fields.
- Add maintainer regression coverage for successful bootstrap notifications,
  unchanged timeout handling, uncached recovery success/failure, HS-only lights
  and numeric capability combinations. The integrated suite passes 237 tests.
- Review and integration details, including the expired CI approval diagnosis,
  are recorded in the [Traditional Chinese report](docs/testing/PR-3-4-review-2026-10-17.zh-TW.md).

## 0.5.0-beta.107 - 2026-09-11

Source update; the latest tagged HACS release remains `v0.5.0-beta.106`.

- Fix a permanent reconnect stall after an ESPHome API/scanner replacement:
  slot recovery now reads allocations from the currently registered scanner
  for the same source instead of waiting on the retired backend tracker.
- Re-resolve the scanner during both initial GATT cleanup and pending slot
  recovery. Do not wait on a retired tracker's free-slot notification future.
- Keep the no-new-GATT guard when current allocation evidence is busy, missing,
  malformed or from a different source. Do not treat unknown state as a free slot.
- Add allocation snapshot status, source and cumulative recovery wait time to
  diagnostics, with existing privacy redaction preserved.
- Keep beta.106 candidate selection, Mesh login, GATT/CCCD timeouts, fixture
  controls and imported configuration unchanged. No HA Bluetooth or Proxy
  configuration changes are required.
- Pass 219 offline tests, including 11 added regression tests. Three physical
  control-switch cycles with two T2c fixtures online eventually recovered;
  two succeeded on the first attempt, while the third needed nine attempts
  and approximately six minutes before `connected` was observed.
- This fixes the stale-tracker wait, not all GATT/CCCD failures. Reconnection
  latency remains variable; multi-ESPHome-Proxy hardware is not qualified by
  these tests. See the [validation notes](docs/RELEASE-v0.5.0-beta.107.md).

## 0.5.0-beta.106 - 2026-08-13

- Define each connection round from recently reachable Mesh Proxy candidates,
  so powered-off configured fixtures can no longer prevent a failed round from
  restarting.
- Wait for ESPHome to confirm that the previous remote GATT allocation is
  reusable before opening another connection, with bounded cleanup and slot
  recovery when confirmation is delayed.
- Add fair candidate failover, bounded retries for a sole surviving proxy, and
  90-second ownership evidence so stale candidates cannot permanently block
  one-fixture recovery.
- Preserve a 30-second radio quiet window when candidate deferral temporarily
  makes a multi-proxy Mesh look like a single-candidate topology.
- Recover from eligible GATT authorization failures with a fresh client and
  retain a recent `BLEDevice` only from the same connectable scanner path.
- Re-evaluate fixture availability for every Mesh session and keep fixtures
  unavailable until they answer the current status census.
- Retain the beta.4 light, brightness, 100 K CCT, G/M, hue and saturation
  controls without changing the imported Mesh format.
- Pass 208 offline tests. Physical qualification completed 10/10 two-of-four
  acquisition cycles and 3/3 one-of-four cycles, with 92/92 Home Assistant
  commands and 184/184 Mesh writes successful.
- Keep this release marked as beta while three-of-four, four-of-four, clean
  Core cold-start and extended retention qualification remain pending.

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
