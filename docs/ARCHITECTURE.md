# Architecture

## Scope

Amaran BLE imports credentials and fixture addresses from an existing
Amaran/Sidus BLE Mesh. It does not provision fixtures and does not modify the
native Home Assistant Bluetooth stack or an ESPHome Bluetooth Proxy.

## Connection model

Every imported, powered fixture can be a Mesh Proxy candidate. Home Assistant
creates one persistent GATT connection to a selected candidate and sends
Bluetooth Mesh network PDUs through the standard Mesh Proxy service (`0x1828`).
The destination Mesh address selects the target fixture, so one proxy
connection can control the full imported Mesh.

The integration does not need one GATT connection per fixture.

## Ownership switch

The control switch is the explicit ownership boundary:

- On: scan candidates, connect, perform Mesh Proxy initialization and retain
  the selected connection.
- Off: stop the background connector and disconnect the client so Sidus Link or
  Amaran Desktop can acquire the fixture.

Only one central should own the selected fixture's GATT connection at a time.

## Candidate lifecycle

1. Gather fresh Home Assistant Bluetooth advertisements.
2. Rank candidates using visibility, connectability, previous success, health,
   cooldown and quarantine state.
3. Attempt one candidate with bounded GATT and service-discovery time.
4. Subscribe to Mesh Proxy notifications and initialize the Mesh filter.
5. Enter a 30-second operational stability window.
6. Mark the connection usable.
7. After 120 seconds, mark the candidate trusted and save it as the preferred
   proxy.

A disconnect between the operational and trusted thresholds is a probation
disconnect. The candidate is quarantined so another fixture can be tried.

## State model

Mesh status notifications update power, brightness, CCT and color state when
the fixture reports them. Periodic status requests reconcile missed
notifications and physical-control changes.

The controller keeps one shared color-state snapshot per fixture. The standard
light entity and the brightness, CCT, G/M, hue and saturation number entities
subscribe to that snapshot, so physical reports and successful commands update
all related Home Assistant entities consistently.

Availability is deliberately conservative. A single missed Mesh response does
not make a fixture unavailable because BLE Mesh packets are not acknowledged at
the GATT transport level. Repeated misses are required, which means a powered
off non-proxy fixture can take approximately 30-60 seconds to be marked
unavailable.

## Privacy boundary

Configuration entries contain Mesh keys and fixture addresses. Downloaded
diagnostics are passed through one recursive sanitizer:

- `net_key`, `app_key`, `netKey` and `appKey` values are redacted at any depth.
- Colon-separated, hyphen-separated and compact MAC addresses are assigned
  stable per-report aliases.
- MAC addresses embedded in errors and used as dictionary keys are sanitized.

The sanitizer preserves relationships between candidates without exposing the
original addresses.
