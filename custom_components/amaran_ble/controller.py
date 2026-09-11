"""Runtime controller for Amaran BLE Mesh lights."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import random
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import homeassistant.core as ha_core
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import mesh, telink
from .color_state import FixtureColorState
from .const import (
    DEFAULT_LOCAL_ADDRESS,
    CONF_CONTROL_ENABLED,
    CONF_LAST_SUCCESSFUL_PROXY,
    PROVISIONING_DATA_OUT_UUID,
    PROXY_DATA_IN_UUID,
    PROXY_DATA_OUT_UUID,
    TELINK_OPCODE,
)
from .models import AmaranConfig

_LOGGER = logging.getLogger(__name__)
EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"

# Match Amaran 1.0.71's Telink-derived AutoConnect lifecycle: scan the configured
# mesh nodes, connect one node at a time, complete Proxy login, then retain it.
CONNECT_MAX_ATTEMPTS = 1
OFFICIAL_CONNECT_DELAY = 1.0
OFFICIAL_GATT_CONNECT_TIMEOUT = 9.0
OFFICIAL_SERVICE_DISCOVERY_TIMEOUT = 9.0
OFFICIAL_SERVICE_READY_DELAY = 0.5
OFFICIAL_REQUESTED_MTU = 517
# Android's CONNECTION_PRIORITY_HIGH profile uses short connection intervals
# while the GATT client is performing its initial service and CCCD work. An
# ESPHome proxy already starts its own connection-parameter update during GATT
# open; beta.75 proved that immediately issuing a second update overlaps that
# pending command and is followed by HCI 0x08/0x22 disconnects. Keep the source
# values in diagnostics, but let the remote backend own this lifecycle.
POST_CONNECT_CONNECTION_PARAMS_ENABLED = False
POST_CONNECT_MIN_INTERVAL = 9  # 11.25 ms, units of 1.25 ms.
POST_CONNECT_MAX_INTERVAL = 12  # 15 ms, units of 1.25 ms.
POST_CONNECT_LATENCY = 0
POST_CONNECT_SUPERVISION_TIMEOUT = 2000  # 20 seconds, units of 10 ms.
POST_CONNECT_PARAMS_REQUEST_TIMEOUT = 3.0
# Android's one-second delay separates close() from a new local connectGatt().
# HA already drains the previous remote client before reaching this point, and
# beta.44-beta.45 showed that adding the delay to a fresh ESPHome allocation did
# not improve physical opens. Keep it in diagnostics as source behavior only.
ESPHOME_PRE_CONNECT_DELAY = 0.0
GATT_CLIENT_CONNECT_TIMEOUT = 45.0
# bleak-esphome owns physical GATT establishment and service discovery as one
# operation. The 0.3.39 30/30 run let that backend operation finish naturally;
# later integration-level deadlines only cancelled work the remote radio was
# still resolving. None means that the backend timeout is authoritative.
GATT_ESTABLISH_DEADLINE: float | None = None
# A passive advertisement proves that the fixture is present, but repeated
# ESP_GATT_CONN_CONN_CANCEL/status 133 failures show that it does not always
# leave the remote Proxy radio with a usable post-failure device context.
# Refresh only the candidate that just failed before its next attempt. Beta.74
# proved that switching the shared ESPHome scanner to ACTIVE adds contention
# without producing a successful waited attempt, so the refresh must remain
# passive and only wait for a packet newer than the failed attempt's baseline.
PREOPEN_ADVERTISEMENT_RESYNC = True
PREOPEN_ADVERTISEMENT_RESYNC_SCANNING_MODE = (
    bluetooth.BluetoothScanningMode.PASSIVE
)
# Beta.58 proved that forcing ESPHome V3 without-cache did not improve physical
# GATT establishment. Retain verified service data and use the normal cached
# request path, matching the successful beta.53 and historical 0.3.39 runs.
FORCE_FRESH_GATT_DISCOVERY = False
# Beta.60's per-address-only cache completed zero of two 300-second ownership
# windows. Restore the Mesh-verified cache behavior used by beta.59 so beta.61
# can isolate candidate radio quality rather than changing two variables.
SHARE_VERIFIED_GATT_CACHE_BETWEEN_CANDIDATES = True
# ESPHome V3 without-cache does not report the connection to HA until both
# service discovery and MTU exchange finish. Amaran's Telink firmware can
# complete discovery but leave MTU exchange unanswered until the BLE
# supervision timeout. Prime the standard Mesh Proxy handles observed on
# independently discovered T2c fixtures so the proxy can use V3 with-cache and
# report a physical open immediately. A later Mesh login still verifies the
# profile before it is shared with any peer candidate.
PRIME_TELINK_MESH_PROXY_BOOTSTRAP_CACHE = True
TELINK_MESH_PROXY_SERVICE_HANDLE = 49
TELINK_MESH_PROXY_DATA_IN_HANDLE = 51
TELINK_MESH_PROXY_DATA_OUT_HANDLE = 53
TELINK_MESH_PROXY_DATA_OUT_CCCD_HANDLE = 54
TELINK_MESH_PROXY_BOOTSTRAP_MTU = 23
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
# HA shares its scanner with every Bluetooth integration. Keep the normal
# callback passive and wait for a temporarily active scanner to drain before
# asking an ESPHome proxy to open a physical GATT link.
ADVERTISEMENT_CALLBACK_SCANNING_MODE = bluetooth.BluetoothScanningMode.PASSIVE
WAIT_FOR_SOURCE_SCANNER_PASSIVE = True
CONNECT_CANDIDATES_PER_ROUND = 1
CANDIDATE_FAILURE_COOLDOWN = 10.0
SINGLE_CANDIDATE_FAILURE_COOLDOWN = 2.0
# A physical-link failure happens before the fixture owns a usable GATT
# session. Keep its retry cadence close to the proven 0.3.39 baseline instead
# of exponentially sidelining an otherwise reachable Mesh proxy.
GATT_FAILURE_COOLDOWN = 10.0
POST_OPEN_FAILURE_COOLDOWN = 10.0
SHORT_CONNECTION_COOLDOWN = 10.0
PROBATION_CONNECTION_COOLDOWN = 60.0
CANDIDATE_FAILURE_COOLDOWN_MULTIPLIER = 1.5
CANDIDATE_FAILURE_COOLDOWN_MAX = 60.0
CANDIDATE_HEALTH_HISTORY_SIZE = 8
CANDIDATE_HEALTH_DECAY = 0.8
CANDIDATE_LAST_STABLE_BONUS = 1.5
PREOPEN_FALLBACK_MIN_FAILURES = 2
PREOPEN_FALLBACK_SKIP_ROUNDS = 1
# Beta.76 showed that an unproven fallback with repeated status-133 failures
# can consume a full 20-second GATT slot in every round while the last trusted
# proxy remains the only candidate that has ever opened. Defer that fallback
# for one bounded round after two pre-open failures. The helper immediately
# releases the deferral when it is the only advertising candidate, preserving
# single-fixture failover without hard-coding a MAC, model or fixture count.
PREOPEN_FALLBACK_DEFERRAL_ENABLED = True
# bleak-retry-connector 4.6.1 treats ESP_GATT_CONN_CONN_CANCEL as an ESPHome
# slot-closing race and retries the same client after a four-second backoff.
# Keep normal attempts single-shot so multi-fixture failover remains prompt,
# but restore that upstream behavior after one surviving candidate has already
# failed two complete pre-open attempts.
SINGLE_CANDIDATE_CONNECTOR_SLOT_RETRY_ENABLED = True
SINGLE_CANDIDATE_CONNECTOR_SLOT_RETRY_MIN_FAILURES = 2
# Beta.86 proved that two to four same-client attempts can absorb an ESPHome
# slot-closing race, but bleak-retry-connector's upstream nine-error allowance
# can consume roughly 200 seconds before a stale client is rebuilt. Preserve
# the useful short recovery while returning longer failure chains to the outer
# lifecycle, which disposes the GATT client and confirms slot release.
SINGLE_CANDIDATE_CONNECTOR_SLOT_RETRY_MAX_ATTEMPTS = 4
# Beta.69-beta.71 proved that enforcing the RSSI tier on the actual candidate
# list can turn harmless advertisement fluctuations into extra recovery
# rounds. Keep the bounded helper for regression coverage, but let every fresh
# visible candidate participate in each round.
RSSI_FOCUS_ENABLED = False
RSSI_FOCUS_WINDOW_DB = 6
RSSI_FOCUS_MAX_FAILED_ROUNDS = 2
CANDIDATE_HEALTH_WEIGHTS = {
    "stable": 8.0,
    "ready": 3.0,
    "gatt_connect": -1.0,
    "gatt_or_service_discovery": -2.0,
    "proxy_notification": -5.0,
    "mesh_login": -4.0,
    "post_login": -3.0,
    "short_disconnect": -5.0,
    "probation_disconnect": -6.0,
    "stable_disconnect": -3.0,
    "unknown": -1.0,
}
CANDIDATE_FAILURE_OUTCOMES = frozenset(CANDIDATE_HEALTH_WEIGHTS) - {
    "ready",
    "stable",
}
POST_OPEN_FAILURE_REASONS = frozenset(
    {
        "proxy_notification",
        "mesh_login",
        "post_login",
        "short_disconnect",
        "probation_disconnect",
        "stable_disconnect",
    }
)
STABLE_CONNECTION_SECONDS = 30.0
TRUSTED_CONNECTION_SECONDS = 120.0
DISCONNECT_TIMEOUT_CANDIDATE_COOLDOWN = 20.0
DISCONNECT_SETTLE_SECONDS = 2.0
CLIENT_CLEANUP_TIMEOUT = 5.0
CLIENT_CLEANUP_DRAIN_TIMEOUT = 25.0
ESPHOME_SLOT_RELEASE_CONFIRMATION_TIMEOUT = 25.0
ESPHOME_SLOT_RELEASE_POLL_INTERVAL = 0.25
# Once a cleanup timeout proves that ESPHome still owns the old allocation,
# recheck its authoritative slot snapshot without issuing another GATT open.
# Keep each foreground check short; the maintain loop continues polling until
# the allocation disappears and a reusable slot is reported.
ESPHOME_SLOT_RECOVERY_RECHECK_TIMEOUT = 5.0
CONTROL_RELEASE_DRAIN_TIMEOUT = 25.0
SHUTDOWN_RELEASE_DRAIN_TIMEOUT = 10.0
SERVICE_CACHE_CLEAR_TIMEOUT = 2.0
DISCOVERY_TIMEOUT = 3
# A fixture that stopped advertising is not a useful GATT candidate. Keep this
# independent from Mesh status/availability aging: it only controls which
# fixture may be selected while acquiring the shared Mesh Proxy connection.
ADVERTISEMENT_MAX_AGE = 30.0
WRITE_ONLY_ADVERTISEMENT_MAX_AGE = ADVERTISEMENT_MAX_AGE
STARTUP_CONNECT_ADVERTISEMENT_MAX_AGE = 8.0
# Clearing HA's merged advertisement history does not clear the originating
# scanner's BLEDevice cache. If a post-failure packet narrowly misses the
# three-second resync window, preserve the same scanner path only when the
# packet used to select it was very recent. This avoids a false "device not
# found" round without turning an old or cross-scanner cache entry into a GATT
# candidate.
PREOPEN_SCANNER_CACHE_FALLBACK_MAX_AGE = STARTUP_CONNECT_ADVERTISEMENT_MAX_AGE
STARTUP_SCAN_GRACE = 30.0
# Keep a recently attempted alternative as ownership evidence across one long
# ESPHome GATT attempt. Unlike the advertisement window, this is bounded so a
# fixture that was powered off just before selection cannot suppress genuine
# one-survivor recovery for the rest of the control-on cycle.
OWNERSHIP_ALTERNATIVE_EVIDENCE_SECONDS = 90.0
# Amaran 1.0.71's NetworkingController gives the complete two-step Proxy
# Filter initialization (Set Filter Type -> status -> Add Addresses -> status)
# one eight-second deadline.
FILTER_STATUS_TIMEOUT = 8.0
PROXY_READY_DELAY = 0.2
# A remote ESPHome descriptor write includes API transport and controller
# scheduling. Do not cancel a healthy Proxy-Out CCCD operation at the
# historical local-radio 12-second boundary; beta.64 isolates 30 seconds.
NOTIFICATION_SETUP_TIMEOUT = 30.0
GATT_NOTIFICATION_SETTLE_SECONDS = 3.0
GATT_NOTIFICATION_RECOVERY_SETTLE_SECONDS = 5.0
WRITE_ONLY_RECOVERY_SETTLE_SECONDS = 2.0
NOTIFICATION_RETRY_DELAY = 60.0
INITIAL_NOTIFICATION_DELAY = 30.0
GATT_AUTHORIZATION_RECONNECT_SETTLE_SECONDS = 8.0
# The historical 0.3.39 stress run recovered from a terminal CCCD
# authorization failure by rotating to another visible Mesh proxy. Reusing
# the same fixture immediately can repeat the stale remote GATT state and
# delays the candidate that may already be ready to accept ownership.
GATT_AUTHORIZATION_SAME_CANDIDATE_RETRY = False
# Keep beta.71's immediate handoff when another fresh Mesh Proxy exists. If
# only one fixture is currently reachable, however, there is nowhere useful to
# hand off: dispose the rejected GATT object and give that fixture one bounded
# official-style reconnect with a fresh client.
SINGLE_CANDIDATE_GATT_AUTHORIZATION_RETRY = True
AUTO_CONNECT_DELAY = 0.5
# ESPHome may report client.disconnect() complete before its physical BLE slot
# is reusable. Keep fast Off/On cycles behind a bounded release window so the
# first reconnect does not start a self-sustaining CONN_CANCEL cleanup storm.
RELEASE_RECONNECT_COOLDOWN = 30.0
SHORT_RELEASE_RECONNECT_COOLDOWN = 45.0
MAINTAIN_RETRY_DELAYS = (1, 2, 5, 10)
OUT_OF_SLOTS_RETRY_DELAY = 4
GATT_ESTABLISH_RETRY_DELAY = 2
RECOVERY_RETRY_DELAY = 12
SINGLE_CANDIDATE_ADVERTISEMENT_RESYNC_RETRY_DELAY = 8
PREFERRED_PROXY_GATT_RECOVERY_DELAY = 30
PREOPEN_STORM_ROUNDS = 2
# Preserve the 0.3.34 fixed recovery timing, which had the best 30-cycle
# connection latency of the comparable long-running tests.
PREOPEN_STORM_RECOVERY_DELAY = 30
# A bounded deferral can temporarily make a multi-proxy Mesh look like a
# single-candidate round. Do not apply the true one-survivor eight-second
# cadence in that case: both radios have just participated in recovery and the
# ESPHome controller benefits from the same quiet window used for a pre-open
# failure storm.
DEFERRED_MULTI_CANDIDATE_RECOVERY_DELAY = PREOPEN_STORM_RECOVERY_DELAY
NO_FRESH_ADVERTISEMENT_RETRY_DELAY = 2
ADVERTISEMENT_RESYNC_CONNECT_SETTLE_SECONDS = 0.1
SCANNER_PASSIVE_WAIT_TIMEOUT = 20.0
SCANNER_MODE_POLL_INTERVAL = 0.25
POST_ACTIVE_SCAN_SETTLE_SECONDS = 1.0
PROXY_HEALTH_CHECK_INTERVAL = 5
STATUS_REFRESH_INTERVAL = 10
STATUS_MISS_LIMIT = 4
INITIAL_STATUS_REFRESH_ATTEMPTS = 4
INITIAL_STATUS_REFRESH_DELAY = 2
INITIAL_STATUS_WAIT = 1.5
LIGHT_STATUS_STALE_SECONDS = 25
LIGHT_STATUS_UNAVAILABLE_SECONDS = LIGHT_STATUS_STALE_SECONDS * STATUS_MISS_LIMIT
COMMAND_CONNECT_TIMEOUT = 60.0
COMMAND_CONNECT_RETRY_DELAY = 1.0
COMMAND_STATUS_TIMEOUT = 5.0
COMMAND_LOCK_TIMEOUT = 3.0
COMMAND_TRACE_HISTORY_SIZE = 32
COMPACT_COMMAND_TRACE_HISTORY_SIZE = 6
COMPACT_CONNECT_ATTEMPT_HISTORY_SIZE = 4
MESH_PROXY_SERVICE_UUID = "00001828-0000-1000-8000-00805f9b34fb"
MESH_PROVISIONING_SERVICE_UUID = "00001827-0000-1000-8000-00805f9b34fb"


@dataclass
class _ProxySession:
    """One retained BLE Mesh Proxy connection."""

    address: str
    client: object | None = None
    provisioning_data_out: object | None = None
    proxy_data_in: object | None = None
    proxy_data_out: object | None = None
    iv_index: int = 0
    notify_started: bool = False
    provisioning_notify_started: bool = False
    status_notifications_supported: bool = True
    notification_callback: Callable[[int, bytearray], None] | None = None
    filter_status_event: asyncio.Event | None = None
    mesh_ready: bool = False
    connected_at: float | None = None
    stable: bool = False
    trusted: bool = False


class _AmaranBleakClient(BleakClientWithServiceCache):
    """Bleak client that exposes its instance for cancellation cleanup."""

    def __init__(
        self,
        *args: object,
        amaran_client_created: Callable[[object], None] | None = None,
        amaran_backend_created: Callable[[object], None] | None = None,
        amaran_connect_attempt_started: Callable[[], None] | None = None,
        amaran_retry_transient_slot_errors: bool = False,
        amaran_retry_transient_slot_errors_max_attempts: int = (
            SINGLE_CANDIDATE_CONNECTOR_SLOT_RETRY_MAX_ATTEMPTS
        ),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._amaran_backend_created = amaran_backend_created
        self._amaran_connect_attempt_started = amaran_connect_attempt_started
        self._amaran_retry_transient_slot_errors = (
            amaran_retry_transient_slot_errors
        )
        self._amaran_retry_transient_slot_errors_max_attempts = max(
            1,
            int(amaran_retry_transient_slot_errors_max_attempts),
        )
        self._amaran_connect_attempts = 0
        self._amaran_backend: object | None = None
        if amaran_client_created is not None:
            amaran_client_created(self)

    def _remember_amaran_backend(self, backend: object) -> None:
        """Retain the selected backend for bounded ESPHome slot cleanup."""
        self._amaran_backend = backend
        if self._amaran_backend_created is not None:
            self._amaran_backend_created(backend)

    @staticmethod
    def _wrap_selected_backend_factory(
        wrapped_backend: object,
        callback: Callable[[object], None],
    ) -> bool:
        """Invoke a callback after HA creates the selected ESPHome backend."""
        backend_factory = getattr(wrapped_backend, "client", None)
        if not callable(backend_factory):
            return False

        _LOGGER.debug(
            "Amaran BLE selected backend factory type=%s module=%s",
            getattr(backend_factory, "__qualname__", type(backend_factory).__name__),
            getattr(backend_factory, "__module__", ""),
        )

        def create_backend(
            *factory_args: object,
            **factory_kwargs: object,
        ) -> object:
            backend = backend_factory(*factory_args, **factory_kwargs)
            callback(backend)
            return backend

        setattr(wrapped_backend, "client", create_backend)
        return True

    def _async_get_best_available_backend_and_device(
        self,
        *args: object,
        **kwargs: object,
    ) -> object:
        """Hook the selected HA backend factory before its connect call."""
        wrapped_backend = super()._async_get_best_available_backend_and_device(
            *args,
            **kwargs,
        )
        self._wrap_selected_backend_factory(
            wrapped_backend,
            self._remember_amaran_backend,
        )
        return wrapped_backend

    async def connect(self, *args: object, **kwargs: object) -> object:
        """Run one GATT attempt and optionally expose slot races upstream."""
        self._amaran_connect_attempts += 1
        if self._amaran_connect_attempt_started is not None:
            self._amaran_connect_attempt_started()
        kwargs["timeout"] = GATT_CLIENT_CONNECT_TIMEOUT
        try:
            return await super().connect(*args, **kwargs)
        except BleakError as err:
            if (
                self._amaran_retry_transient_slot_errors
                and "esp_gatt_conn_conn_cancel" in str(err).lower()
                and self._amaran_connect_attempts
                < self._amaran_retry_transient_slot_errors_max_attempts
            ):
                # Let bleak-retry-connector reuse this client and apply its
                # authoritative four-second ESPHome slot-closing backoff.
                raise
            # bleak-retry-connector retries ESP status 133 up to nine times even
            # when max_attempts=1.  That keeps HA on one fixture for minutes.
            # Convert the completed backend attempt to a non-Bleak exception so
            # the integration can rotate mesh proxy candidates immediately.
            if (
                self._amaran_retry_transient_slot_errors
                and "esp_gatt_conn_conn_cancel" in str(err).lower()
            ):
                raise RuntimeError(
                    "Single Amaran GATT retry budget exhausted after "
                    f"{self._amaran_connect_attempts} attempts: "
                    f"{type(err).__name__}: {err}"
                ) from err
            raise RuntimeError(
                f"Single Amaran GATT attempt failed: {type(err).__name__}: {err}"
            ) from err


class AmaranBleController:
    """Send Telink 0x26 commands through a BLE Mesh Proxy connection."""

    def __init__(self, hass: HomeAssistant, config: AmaranConfig) -> None:
        """Initialize the controller."""
        self.hass = hass
        self.config = config
        self.keys = mesh.derive_keys(config.net_key, config.app_key)
        self._sequence = random.randint(12_000_000, 16_000_000)
        self._operation_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, _ProxySession] = {}
        self._pending_clients: dict[str, object] = {}
        self._notification_upgrade_tasks: dict[str, asyncio.Task[None]] = {}
        self._candidate_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._maintain_task: asyncio.Task[None] | None = None
        self._connect_task: asyncio.Task[_ProxySession] | None = None
        self._active_proxy_address: str | None = None
        self._candidate_cursor = 0
        self._ignored_stored_proxy: str | None = None
        self._candidate_cooldown_until: dict[str, float] = {}
        self._candidate_post_open_quarantine_until: dict[str, float] = {}
        self._candidate_failure_reason: dict[str, str] = {}
        self._candidate_failure_streak: dict[str, int] = {}
        self._candidate_probe_after_round: dict[str, int] = {}
        self._round_failed_candidates: set[str] = set()
        self._preferred_ownership_first_attempt_pending = True
        self._candidate_advertisement_seen_at: dict[str, float] = {}
        self._consecutive_preopen_failures = 0
        self._notification_backoff_until: dict[str, float] = {}
        self._service_cache_bypass: set[str] = set()
        self._cccd_reconnect_pending: set[str] = set()
        self._cached_cccd_retry_attempted: set[str] = set()
        self._uncached_recovery_attempted: set[str] = set()
        self._authorization_recovery_attempted: set[str] = set()
        self._authorization_reconnect_after = 0.0
        self._verified_gatt_cache_template: tuple[object, int, str] | None = None
        self._shared_gatt_cache_sources: dict[str, str] = {}
        self._bootstrap_gatt_cache_sources: set[str] = set()
        self._shared_gatt_cache_incompatible: set[str] = set()
        self._current_connect_trace: dict[str, object] | None = None
        self._connect_attempt_stats: dict[str, int] = {}
        self._last_esphome_slot_release: dict[str, object] | None = None
        self._pending_esphome_slot_releases: dict[str, object] = {}
        self._esphome_slot_recovery_started: float | None = None
        self._esphome_slot_recovery_resync_pending = False
        self._candidate_health_history: dict[str, deque[str]] = {}
        self._connect_attempt_sequence = 0
        self._ownership_cycle = 1
        self._ownership_attempted_candidates: set[str] = set()
        self._ownership_candidate_attempted_at: dict[str, float] = {}
        self._candidate_round = 1
        self._rssi_focus_failed_rounds = 0
        self._command_trace_sequence = 0
        self._command_trace_stats: dict[str, int] = {}
        self._recent_command_traces: deque[dict[str, object]] = deque(
            maxlen=COMMAND_TRACE_HISTORY_SIZE
        )
        self._connect_failure_count = 0
        self._mesh_available = False
        self._started_at = hass.loop.time()
        self._next_connect_after = self._started_at + AUTO_CONNECT_DELAY
        self._release_guard_until = 0.0
        self._last_release_uptime: float | None = None
        self._last_release_was_short = False
        self._last_release_in_progress = False
        self._startup_scan_until = self._started_at + STARTUP_SCAN_GRACE
        self._last_maintain_error: str | None = None
        self._entry: object | None = None
        self._control_enabled = True
        self._control_enabled_event = asyncio.Event()
        self._control_enabled_event.set()
        self._advertisement_event = asyncio.Event()
        self._homeassistant_started = self._hass_has_started()
        self._startup_unsubscribe: Callable[[], None] | None = None
        self._shutdown_unsubscribe: Callable[[], None] | None = None
        self._advertisement_unsubscribe: Callable[[], None] | None = None
        self._availability_callbacks: dict[str, set[Callable[[bool], None]]] = {}
        self._status_callbacks: dict[int, set[Callable[[telink.TelinkStatus], None]]] = {}
        self._fixture_state_callbacks: dict[
            int,
            set[Callable[[FixtureColorState], None]],
        ] = {}
        self._control_callbacks: set[Callable[[bool], None]] = set()
        self._mesh_callbacks: set[Callable[[bool], None]] = set()
        self._diagnostic_callbacks: set[Callable[[], None]] = set()
        self._light_available: dict[str, bool] = {
            light.mac: False for light in config.lights
        }
        self._last_status_seen: dict[int, float] = {}
        self._last_status: dict[int, telink.TelinkStatus] = {}
        self._fixture_states: dict[int, FixtureColorState] = {
            light.mesh_address: FixtureColorState.from_config(light)
            for light in config.lights
        }
        self._status_miss_count: dict[int, int] = {}
        self._status_seen_in_session: set[int] = set()
        self._initial_status_census_complete = False
        self._mesh_session_sequence = 0
        self._mesh_session_started_at: float | None = None
        self._connect_attempt_history: deque[dict[str, object]] = deque(maxlen=12)
        self._stopping = False

    async def async_start(self, entry: object | None = None) -> None:
        """Start a delayed single-proxy connector."""
        self._stopping = False
        self._entry = entry
        self._register_shutdown_listener()
        options = getattr(entry, "options", None)
        if isinstance(options, Mapping):
            self._control_enabled = bool(options.get(CONF_CONTROL_ENABLED, True))
            if self._control_enabled:
                self._ownership_cycle = max(1, self._ownership_cycle)
                self._control_enabled_event.set()
            else:
                self._ownership_cycle = 0
                self._control_enabled_event.clear()
        if self._homeassistant_started:
            self._startup_scan_until = self.hass.loop.time() + STARTUP_SCAN_GRACE
            self._register_advertisement_callback()
            self._ensure_maintain_task()
        else:
            bus = getattr(self.hass, "bus", None)
            listen_once = getattr(bus, "async_listen_once", None)
            if listen_once is None:
                self._homeassistant_started = True
                self._startup_scan_until = self.hass.loop.time() + STARTUP_SCAN_GRACE
                self._register_advertisement_callback()
                self._ensure_maintain_task()
            else:
                self._startup_unsubscribe = listen_once(
                    EVENT_HOMEASSISTANT_STARTED,
                    self._handle_homeassistant_started,
                )

    def _register_shutdown_listener(self) -> None:
        """Release the retained GATT session before Home Assistant exits."""
        if self._shutdown_unsubscribe is not None:
            return
        add_shutdown_job = getattr(self.hass, "async_add_shutdown_job", None)
        hass_job_type = getattr(ha_core, "HassJob", None)
        if add_shutdown_job is not None and hass_job_type is not None:
            self._shutdown_unsubscribe = add_shutdown_job(
                hass_job_type(self._handle_homeassistant_shutdown)
            )
            return
        bus = getattr(self.hass, "bus", None)
        listen_once = getattr(bus, "async_listen_once", None)
        if listen_once is None:
            return
        self._shutdown_unsubscribe = listen_once(
            EVENT_HOMEASSISTANT_STOP,
            self._handle_homeassistant_stop,
        )

    async def _handle_homeassistant_stop(self, event: object) -> None:
        """Fallback stop-event bridge for Home Assistant without shutdown jobs."""
        _ = event
        await self._handle_homeassistant_shutdown()

    async def _handle_homeassistant_shutdown(self) -> None:
        """Cleanly return the ESPHome GATT slot during Core shutdown."""
        # A one-time event listener is removed before its target runs. A
        # shutdown job is already being iterated by Core. Neither callback
        # should remove itself while the shutdown sequence is executing.
        self._shutdown_unsubscribe = None
        _LOGGER.info(
            "Home Assistant is stopping; releasing the Amaran BLE proxy session"
        )
        await self.async_stop(
            cleanup_timeout=SHUTDOWN_RELEASE_DRAIN_TIMEOUT,
            fast_session_release=True,
        )

    def _ensure_maintain_task(self) -> None:
        """Ensure the background connector exists."""
        if not self._homeassistant_started:
            return
        if self._maintain_task is None or self._maintain_task.done():
            self._maintain_task = self._create_background_task(
                self._maintain_proxy_loop(),
                "amaran_ble_maintain_proxy",
            )

    def _handle_homeassistant_started(self, event: object) -> None:
        """Begin BLE ownership only after Home Assistant finished startup."""
        _ = event
        self.hass.loop.call_soon_threadsafe(self._handle_homeassistant_started_on_loop)

    def _handle_homeassistant_started_on_loop(self) -> None:
        """Start the background connector on the Home Assistant event loop."""
        self._startup_unsubscribe = None
        self._homeassistant_started = True
        self._next_connect_after = self.hass.loop.time() + AUTO_CONNECT_DELAY
        self._startup_scan_until = self.hass.loop.time() + STARTUP_SCAN_GRACE
        self._register_advertisement_callback()
        if self._control_enabled and not self._stopping:
            self._ensure_maintain_task()

    def _register_advertisement_callback(self) -> None:
        """Wake the connector as soon as a mesh proxy advertisement arrives."""
        if self._advertisement_unsubscribe is not None:
            return
        register_callback = getattr(bluetooth, "async_register_callback", None)
        if register_callback is None:
            return
        try:
            self._advertisement_unsubscribe = register_callback(
                self.hass,
                self._handle_bluetooth_advertisement,
                {"service_uuid": MESH_PROXY_SERVICE_UUID},
                ADVERTISEMENT_CALLBACK_SCANNING_MODE,
            )
        except Exception:
            _LOGGER.debug(
                "Unable to register Amaran BLE advertisement wake callback",
                exc_info=True,
            )

    def _handle_bluetooth_advertisement(
        self,
        service_info: object,
        _change: object | None = None,
    ) -> None:
        """Wake the connector when Home Assistant sees one configured proxy."""
        address = str(getattr(service_info, "address", "")).upper()
        if address not in self._proxy_addresses():
            return
        self.hass.loop.call_soon_threadsafe(
            self._wake_for_advertisement,
            address,
            getattr(service_info, "time", None),
        )

    def _wake_for_advertisement(
        self,
        address: str,
        seen_at: object | None = None,
    ) -> None:
        """Trigger a quick reconnect after a fresh proxy advertisement."""
        if address not in self._proxy_addresses():
            return
        now = self.hass.loop.time()
        observed_at = now
        with contextlib.suppress(TypeError, ValueError):
            observed_at = float(seen_at)
        self._candidate_advertisement_seen_at[address] = max(
            self._candidate_advertisement_seen_at.get(address, 0.0),
            observed_at,
        )
        if self._stopping or not self._control_enabled:
            return
        self._next_connect_after = max(
            self._release_guard_until,
            self._authorization_reconnect_after,
            min(self._next_connect_after, now),
        )
        self._advertisement_event.set()
        self._ensure_maintain_task()

    def _hass_has_started(self) -> bool:
        """Return whether Home Assistant is past startup."""
        state = getattr(self.hass, "state", None)
        if state is not None:
            value = getattr(state, "value", state)
            return str(value).lower() == "running"
        is_running = getattr(self.hass, "is_running", None)
        if isinstance(is_running, bool):
            return is_running
        if callable(is_running):
            with contextlib.suppress(Exception):
                return bool(is_running())
        return True

    @property
    def control_enabled(self) -> bool:
        """Return whether Home Assistant should hold the Amaran BLE connection."""
        return self._control_enabled

    def async_track_control_enabled(
        self,
        callback: Callable[[bool], None],
    ) -> Callable[[], None]:
        """Track changes to the control ownership switch."""
        self._control_callbacks.add(callback)

        def unsubscribe() -> None:
            self._control_callbacks.discard(callback)

        return unsubscribe

    def async_track_mesh_available(
        self,
        callback: Callable[[bool], None],
    ) -> Callable[[], None]:
        """Track whether the mesh proxy is currently connected."""
        self._mesh_callbacks.add(callback)

        def unsubscribe() -> None:
            self._mesh_callbacks.discard(callback)

        return unsubscribe

    def async_track_diagnostics(
        self,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        """Track runtime diagnostic state changes."""
        self._diagnostic_callbacks.add(callback)

        def unsubscribe() -> None:
            self._diagnostic_callbacks.discard(callback)

        return unsubscribe

    def _notify_diagnostics(self) -> None:
        """Publish that diagnostic attributes changed."""
        for callback in list(self._diagnostic_callbacks):
            callback()

    async def async_set_control_enabled(self, enabled: bool) -> None:
        """Enable or release Home Assistant control of the Amaran BLE mesh."""
        enabled = bool(enabled)
        if self._control_enabled == enabled:
            return
        now = self.hass.loop.time()
        if not enabled:
            release_cooldown = self._record_release_guard(now)
            self._release_guard_until = max(
                self._release_guard_until,
                now + release_cooldown,
            )
        self._control_enabled = enabled
        if enabled:
            self._ownership_cycle += 1
            self._candidate_round = 1
            await self._wait_candidate_cleanup_tasks(
                timeout=CONTROL_RELEASE_DRAIN_TIMEOUT,
            )
            cleanup_still_running = any(
                not task.done() for task in self._candidate_cleanup_tasks
            )
            self._control_enabled_event.set()
            now = self.hass.loop.time()
            cleanup_guard_until = now + (
                RELEASE_RECONNECT_COOLDOWN if cleanup_still_running else 0.0
            )
            self._next_connect_after = max(
                self._next_connect_after,
                self._release_guard_until,
                cleanup_guard_until,
            )
            self._startup_scan_until = now + STARTUP_SCAN_GRACE
            self._connect_failure_count = 0
            self._last_maintain_error = None
            self._ignored_stored_proxy = None
            self._notification_backoff_until.clear()
            self._reset_connection_recovery_state()
            self._store_option(CONF_CONTROL_ENABLED, True)
            self._ensure_maintain_task()
        else:
            self._control_enabled_event.clear()
            self._next_connect_after = max(
                self._next_connect_after,
                self._release_guard_until,
            )
            self._connect_failure_count = 0
            self._last_maintain_error = None
            self._ignored_stored_proxy = None
            self._status_miss_count.clear()
            self._store_option(CONF_CONTROL_ENABLED, False)
            if (
                self._maintain_task is not None
                and self._maintain_task is not asyncio.current_task()
            ):
                self._maintain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._maintain_task
                self._maintain_task = None
            if self._connect_task is not None:
                self._connect_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._connect_task
                self._connect_task = None
            cleanup_complete = await self._wait_candidate_cleanup_tasks(
                timeout=CONTROL_RELEASE_DRAIN_TIMEOUT,
            )
            if not cleanup_complete:
                _LOGGER.info(
                    "Amaran BLE control is off, but ESPHome GATT cleanup is "
                    "still draining in the background"
                )
            for proxy_address in list(self._sessions):
                # The official SDK disposes its GATT connection directly. It
                # does not write CCCD notification-disable values before the
                # disconnect, so mirror that release order for remote ESPHome
                # clients as well.
                await self._reset_proxy(proxy_address, fast=True)
            self._reset_connection_recovery_state()
            self._set_mesh_available(False)
        for callback in list(self._control_callbacks):
            callback(enabled)
        self._notify_diagnostics()

    def _reset_connection_recovery_state(self) -> None:
        """Start the next ownership cycle without stale GATT recovery flags."""
        self._service_cache_bypass.clear()
        self._cccd_reconnect_pending.clear()
        self._cached_cccd_retry_attempted.clear()
        self._uncached_recovery_attempted.clear()
        self._authorization_recovery_attempted.clear()
        self._authorization_reconnect_after = 0.0
        self._ownership_attempted_candidates.clear()
        self._ownership_candidate_attempted_at.clear()
        self._round_failed_candidates.clear()
        self._candidate_probe_after_round.clear()
        self._rssi_focus_failed_rounds = 0
        self._preferred_ownership_first_attempt_pending = True
        self._consecutive_preopen_failures = 0
        reset_preopen: list[str] = []
        for address, reason in list(self._candidate_failure_reason.items()):
            if reason not in {"gatt_connect", "gatt_or_service_discovery"}:
                continue
            # A deliberate control Off/On starts a new ownership cycle. Do not
            # inherit the previous cycle's exponential pre-open cooldown, or a
            # reachable fixture can remain filtered for up to five minutes.
            # Post-open quarantine is separate and remains intact.
            if address in self._candidate_post_open_quarantine_until:
                continue
            self._candidate_cooldown_until.pop(address, None)
            self._candidate_failure_streak.pop(address, None)
            self._candidate_failure_reason.pop(address, None)
            reset_preopen.append(address)
        if reset_preopen:
            key = "ownership_preopen_backoff_resets"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + len(reset_preopen)
            )
            _LOGGER.info(
                "Starting a new Amaran BLE ownership cycle with %s stale "
                "pre-open candidate backoff(s) cleared",
                len(reset_preopen),
            )

    def _record_release_guard(self, now: float) -> float:
        """Record a safe reacquire delay for an intentional control release."""
        session = self._ready_session()
        if session is None and self._active_proxy_address is not None:
            session = self._sessions.get(self._active_proxy_address)

        uptime: float | None = None
        if session is not None and session.connected_at is not None:
            uptime = max(0.0, now - session.connected_at)

        connection_in_progress = (
            (
                self._connect_task is not None
                and not self._connect_task.done()
            )
            or bool(self._pending_clients)
            or any(
                not task.done() for task in self._candidate_cleanup_tasks
            )
        )
        short_release = (
            uptime is not None and uptime < STABLE_CONNECTION_SECONDS
        ) or (uptime is None and connection_in_progress)

        self._last_release_uptime = uptime
        self._last_release_was_short = short_release
        self._last_release_in_progress = connection_in_progress

        cooldown = (
            SHORT_RELEASE_RECONNECT_COOLDOWN
            if short_release
            else float(RELEASE_RECONNECT_COOLDOWN)
        )
        _LOGGER.info(
            "Amaran BLE control released after %s; guarding reacquisition for %.0fs",
            (
                f"{uptime:.1f}s connected"
                if uptime is not None
                else "a connection attempt"
                if connection_in_progress
                else "no active connection"
            ),
            cooldown,
        )
        return cooldown

    def _release_guard_remaining(self) -> float:
        """Return how long a new BLE ownership attempt must still wait."""
        return max(0.0, self._release_guard_until - self.hass.loop.time())

    def _create_background_task(
        self,
        coro: object,
        name: str,
    ) -> asyncio.Task:
        """Create a small internal task."""
        create_hass_task = getattr(self.hass, "async_create_background_task", None)
        if create_hass_task is not None:
            return create_hass_task(coro, name)

        create_task = getattr(self.hass, "async_create_task", None)
        if create_task is not None:
            return create_task(coro, name)

        return asyncio.create_task(coro, name=name)

    async def _wait_candidate_cleanup_tasks(
        self,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Give in-flight disconnect cleanup time to release its proxy slot."""
        tasks = list(self._candidate_cleanup_tasks)
        if not tasks:
            return True
        if timeout is None:
            timeout = CLIENT_CLEANUP_TIMEOUT
        done, _pending = await asyncio.wait(
            tasks,
            timeout=timeout,
        )
        for task in done:
            self._candidate_cleanup_tasks.discard(task)
            if not task.cancelled():
                with contextlib.suppress(Exception):
                    task.exception()
        return not any(not task.done() for task in self._candidate_cleanup_tasks)

    def _connection_cleanup_in_progress(self) -> bool:
        """Return whether a cancelled GATT connection is still being released."""
        return (
            bool(self._pending_clients)
            or bool(self._pending_esphome_slot_releases)
            or any(not task.done() for task in self._candidate_cleanup_tasks)
        )

    async def async_stop(
        self,
        *,
        cleanup_timeout: float = CONTROL_RELEASE_DRAIN_TIMEOUT,
        fast_session_release: bool = False,
    ) -> None:
        """Stop work and release all BLE connections."""
        self._stopping = True
        if self._shutdown_unsubscribe is not None:
            self._shutdown_unsubscribe()
            self._shutdown_unsubscribe = None
        if self._startup_unsubscribe is not None:
            self._startup_unsubscribe()
            self._startup_unsubscribe = None
        if self._advertisement_unsubscribe is not None:
            self._advertisement_unsubscribe()
            self._advertisement_unsubscribe = None
        if self._maintain_task is not None:
            self._maintain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintain_task
            self._maintain_task = None
        if self._connect_task is not None:
            self._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
            self._connect_task = None

        cleanup_complete = await self._wait_candidate_cleanup_tasks(
            timeout=cleanup_timeout,
        )
        if not cleanup_complete:
            _LOGGER.warning(
                "Amaran BLE GATT cleanup is still draining while the controller stops"
            )

        for task in self._notification_upgrade_tasks.values():
            task.cancel()
        if self._notification_upgrade_tasks:
            await asyncio.gather(
                *self._notification_upgrade_tasks.values(),
                return_exceptions=True,
            )
        self._notification_upgrade_tasks.clear()

        for proxy_address in list(self._sessions):
            await self._reset_proxy(
                proxy_address,
                fast=fast_session_release,
            )
        _LOGGER.info("Amaran BLE proxy sessions are released")

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFFFFFF
        if self._sequence == 0:
            self._sequence = 1
        return self._sequence

    def _next_command_trace_id(self) -> int:
        """Return a runtime-local identifier for one user command."""
        self._command_trace_sequence += 1
        return self._command_trace_sequence

    def _record_command_trace(
        self,
        *,
        command_id: int,
        command_name: str,
        dst: int,
        sequence: int | None,
        writes: int,
        elapsed: float,
        result: str,
        active_proxy: str | None,
        error: BaseException | None = None,
    ) -> None:
        """Record command timing without payloads, keys, or mesh ciphertext."""
        item: dict[str, object] = {
            "command_id": command_id,
            "ownership_cycle": self._ownership_cycle,
            "command": command_name,
            "mesh_address": f"0x{dst:04x}",
            "sequence": sequence,
            "writes": writes,
            "result": result,
            "elapsed": round(elapsed, 3),
            "active_proxy": active_proxy,
        }
        if error is not None:
            item["error_type"] = type(error).__name__
        self._recent_command_traces.append(item)
        for key in (
            "commands_total",
            f"result_{result}",
            f"command_{command_name}",
        ):
            self._command_trace_stats[key] = self._command_trace_stats.get(key, 0) + 1
        self._command_trace_stats["writes_total"] = (
            self._command_trace_stats.get("writes_total", 0) + writes
        )
        _LOGGER.debug(
            "Amaran BLE command=%s id=%s ownership=%s dst=0x%04x "
            "sequence=%s proxy=%s writes=%s result=%s elapsed=%.3fs",
            command_name,
            command_id,
            self._ownership_cycle,
            dst,
            sequence,
            active_proxy,
            writes,
            result,
            elapsed,
        )
        self._notify_diagnostics()

    def _proxy_addresses(self) -> list[str]:
        """Return proxy candidates in failover order."""
        addresses: list[str] = []
        if self.config.relay_hub:
            addresses.append(self.config.relay_hub)
        addresses.extend(self.config.proxy_candidates)
        addresses.extend(light.mac for light in self.config.lights)
        return list(dict.fromkeys(addresses))

    def _rotated_proxy_addresses(self, addresses: list[str]) -> list[str]:
        """Return proxy candidates rotated after the last failed address."""
        if not addresses:
            return []

        if self._active_proxy_address in addresses:
            return [
                self._active_proxy_address,
                *(address for address in addresses if address != self._active_proxy_address),
            ]

        last_successful_proxy = self._last_successful_proxy()
        if (
            self._connect_failure_count == 0
            and last_successful_proxy in addresses
            and last_successful_proxy != self._ignored_stored_proxy
        ):
            return [
                last_successful_proxy,
                *(address for address in addresses if address != last_successful_proxy),
            ]

        cursor = self._candidate_cursor % len(addresses)
        return [*addresses[cursor:], *addresses[:cursor]]

    def _last_successful_proxy(self) -> str | None:
        """Return the last proxy that connected in a previous HA run."""
        entry = self._entry
        options = getattr(entry, "options", None)
        if not isinstance(options, Mapping):
            return None
        value = options.get(CONF_LAST_SUCCESSFUL_PROXY)
        if value is None:
            return None
        candidate = str(value).upper()
        return candidate if candidate in self._proxy_addresses() else None

    def _advance_candidate_cursor(
        self,
        failed_address: str,
        err: BaseException | None = None,
        *,
        failure_reason: str | None = None,
    ) -> None:
        """Start the next connect round after a failed candidate."""
        addresses = self._proxy_addresses()
        if failed_address not in addresses:
            self._candidate_cursor += 1
            return
        detail = self._format_error(err).lower()
        broad_round_members = self._candidate_round_members(
            include_deferred=True,
            include_rssi_fallback=True,
        )
        attempted_alternatives, _expired_alternatives = (
            self._ownership_alternative_evidence(failed_address)
        )
        single_candidate_authorization_retry = bool(
            SINGLE_CANDIDATE_GATT_AUTHORIZATION_RETRY
            and broad_round_members == {failed_address}
            and not attempted_alternatives
        )
        if (
            (
                GATT_AUTHORIZATION_SAME_CANDIDATE_RETRY
                or single_candidate_authorization_retry
            )
            and "insufficient authorization" in detail
            and failed_address not in self._authorization_recovery_attempted
        ):
            # The official client disposes the invalid GATT object and creates
            # a fresh one. With alternatives, beta.71 still hands off instead;
            # this bounded same-candidate retry is for a one-survivor Mesh.
            self._authorization_recovery_attempted.add(failed_address)
            self._candidate_cursor = addresses.index(failed_address)
            self._candidate_cooldown_until.pop(failed_address, None)
            self._candidate_post_open_quarantine_until.pop(failed_address, None)
            self._round_failed_candidates.discard(failed_address)
            self._authorization_reconnect_after = max(
                self._authorization_reconnect_after,
                self.hass.loop.time()
                + GATT_AUTHORIZATION_RECONNECT_SETTLE_SECONDS,
            )
            self._next_connect_after = max(
                self._next_connect_after,
                self._authorization_reconnect_after,
            )
            key = "gatt_authorization_reconnects_scheduled"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            if single_candidate_authorization_retry:
                key = "single_candidate_gatt_authorization_reconnects_scheduled"
                self._connect_attempt_stats[key] = (
                    self._connect_attempt_stats.get(key, 0) + 1
                )
            _LOGGER.info(
                "Amaran BLE proxy %s reported insufficient GATT authorization; "
                "disposing GATT and retrying the %s candidate once after %.0fs",
                failed_address,
                "only reachable" if single_candidate_authorization_retry else "same",
                GATT_AUTHORIZATION_RECONNECT_SETTLE_SECONDS,
            )
            return
        if failed_address in self._cccd_reconnect_pending:
            self._cccd_reconnect_pending.discard(failed_address)
            handoff_address = self._fresh_cccd_handoff_candidate(
                failed_address,
                addresses,
            )
            if handoff_address is not None:
                self._round_failed_candidates.add(failed_address)
                self._candidate_cursor = addresses.index(handoff_address)
                _LOGGER.info(
                    "Amaran BLE proxy %s CCCD setup failed; handing off to "
                    "fresh proxy %s before uncached recovery",
                    failed_address,
                    handoff_address,
                )
                return
            self._candidate_cursor = addresses.index(failed_address)
            self._candidate_cooldown_until.pop(failed_address, None)
            cache_mode = (
                "uncached"
                if failed_address in self._service_cache_bypass
                else "cached"
            )
            _LOGGER.info(
                "Retrying Amaran BLE proxy %s once with %s GATT services "
                "after CCCD setup timeout",
                failed_address,
                cache_mode,
            )
            return
        if failed_address in self._uncached_recovery_attempted:
            self._uncached_recovery_attempted.discard(failed_address)
            self._service_cache_bypass.discard(failed_address)
            _LOGGER.info(
                "Amaran BLE proxy %s uncached GATT recovery failed; "
                "ending its eligibility for this candidate round",
                failed_address,
            )
        if failed_address == self._last_successful_proxy():
            self._ignored_stored_proxy = failed_address
        if self._should_cooldown_candidate_after_failure(err):
            self._cooldown_failed_candidate(
                failed_address,
                err,
                failure_reason=failure_reason,
            )
        self._schedule_preopen_candidate_reprobe(
            failed_address,
            failure_reason=failure_reason,
        )
        self._round_failed_candidates.add(failed_address)
        self._candidate_cursor = (addresses.index(failed_address) + 1) % len(addresses)

    def _fresh_cccd_handoff_candidate(
        self,
        failed_address: str,
        addresses: list[str],
    ) -> str | None:
        """Return a fresh unused candidate that can take over after CCCD failure."""
        now = self.hass.loop.time()
        for address in addresses:
            if address == failed_address or address in self._round_failed_candidates:
                continue
            if self._candidate_cooldown_until.get(address, 0.0) > now:
                continue
            if self._candidate_post_open_quarantine_until.get(address, 0.0) > now:
                continue
            if self._has_fresh_ble_device(address):
                return address
        return None

    def _should_cooldown_candidate_after_failure(
        self,
        err: BaseException | None,
    ) -> bool:
        """Return whether one failed proxy should briefly yield to alternatives."""
        if len(self._candidate_round_members()) > 1:
            return True
        detail = self._format_error(err).lower()
        return (
            "timeout waiting for connect response" in detail
            or "timeouterror" in detail
            or "out of connection slots" in detail
            or "esp_gatt_conn_conn_cancel" in detail
            or "esp_gatt_conn_fail_establish" in detail
            or "disconnected after" in detail
            or "is not connected" in detail
            or "allocation is still assigned" in detail
        )

    def _candidate_cooldown_seconds(self, err: BaseException | None) -> float:
        """Return how long one failed proxy candidate should rest."""
        detail = self._format_error(err).lower()
        if (
            "out of connection slots" in detail
            or "allocation is still assigned" in detail
        ):
            return OUT_OF_SLOTS_RETRY_DELAY
        if "disconnect timed out: true" in detail:
            return max(
                DISCONNECT_TIMEOUT_CANDIDATE_COOLDOWN,
                GATT_FAILURE_COOLDOWN
                if len(self._candidate_round_members()) > 1
                else SINGLE_CANDIDATE_FAILURE_COOLDOWN,
            )
        if len(self._candidate_round_members()) <= 1:
            return SINGLE_CANDIDATE_FAILURE_COOLDOWN
        if (
            "timeout waiting for connect response" in detail
            or "timeouterror" in detail
            or "esp_gatt_conn_conn_cancel" in detail
            or "esp_gatt_conn_fail_establish" in detail
            or "connection failed to establish" in detail
        ):
            return GATT_FAILURE_COOLDOWN
        if (
            "cccd" in detail
            or "notification setup" in detail
            or "mesh login" in detail
            or "proxy filter" in detail
            or "disconnected after" in detail
        ):
            return POST_OPEN_FAILURE_COOLDOWN
        return CANDIDATE_FAILURE_COOLDOWN

    def _candidate_failure_reason_for_error(
        self,
        err: BaseException | None,
    ) -> str:
        """Classify one failure without exposing payload or key material."""
        detail = self._format_error(err).lower()
        if "disconnected after" in detail:
            return "stable_disconnect"
        if "cccd" in detail or "notification setup" in detail:
            return "proxy_notification"
        if "mesh login" in detail or "proxy filter" in detail:
            return "mesh_login"
        if (
            "service discovery" in detail
            or "insufficient authorization" in detail
            or "gatt service" in detail
        ):
            return "gatt_or_service_discovery"
        if (
            "timeout waiting for connect response" in detail
            or "timeouterror" in detail
            or "esp_gatt_conn_conn_cancel" in detail
            or "esp_gatt_conn_fail_establish" in detail
            or "connection failed to establish" in detail
            or "gatt establish/service discovery exceeded" in detail
        ):
            return "gatt_connect"
        return "unknown"

    def _cooldown_failed_candidate(
        self,
        address: str,
        err: BaseException | None,
        *,
        failure_reason: str | None = None,
    ) -> float:
        """Apply a bounded failure-streak cooldown and return its duration."""
        reason = failure_reason or self._candidate_failure_reason_for_error(err)
        previous_reason = self._candidate_failure_reason.get(address)
        # Reaching a later connection stage is real progress. Do not turn two
        # physical-open failures plus one CCCD failure into a third post-open
        # failure and quarantine the best candidate for minutes. The official
        # client retries that new stage as a fresh recovery class.
        streak = (
            self._candidate_failure_streak.get(address, 0) + 1
            if previous_reason == reason
            else 1
        )
        self._candidate_failure_streak[address] = streak
        self._candidate_failure_reason[address] = reason
        base = self._candidate_cooldown_seconds(err)
        if reason == "probation_disconnect":
            base = max(base, PROBATION_CONNECTION_COOLDOWN)
        preopen_failure = reason in {
            "gatt_connect",
            "gatt_or_service_discovery",
        }
        if len(self._candidate_round_members()) > 1 and not preopen_failure:
            duration = min(
                base * (CANDIDATE_FAILURE_COOLDOWN_MULTIPLIER ** (streak - 1)),
                CANDIDATE_FAILURE_COOLDOWN_MAX,
            )
        else:
            duration = base
        self._candidate_cooldown_until[address] = max(
            self._candidate_cooldown_until.get(address, 0.0),
            self.hass.loop.time() + duration,
        )
        if reason in POST_OPEN_FAILURE_REASONS:
            self._candidate_post_open_quarantine_until[address] = max(
                self._candidate_post_open_quarantine_until.get(address, 0.0),
                self.hass.loop.time() + duration,
            )
        _LOGGER.info(
            "Cooling failed Amaran BLE proxy %s for %.0fs after failure "
            "streak %s (%s)",
            address,
            duration,
            streak,
            reason,
        )
        return duration

    def _mark_candidate_connected(self, address: str) -> None:
        """Prepare failover order without erasing failures before stability."""
        addresses = self._proxy_addresses()
        if address in addresses:
            self._candidate_cursor = (addresses.index(address) + 1) % len(addresses)
        self._candidate_cooldown_until.pop(address, None)
        self._candidate_post_open_quarantine_until.pop(address, None)
        self._candidate_probe_after_round.pop(address, None)
        self._preferred_ownership_first_attempt_pending = False
        self._authorization_recovery_attempted.clear()
        self._authorization_reconnect_after = 0.0

    def _schedule_preopen_candidate_reprobe(
        self,
        address: str,
        *,
        failure_reason: str | None,
    ) -> None:
        """Skip one round for a repeatedly failing non-preferred candidate."""
        if not PREOPEN_FALLBACK_DEFERRAL_ENABLED:
            return
        if failure_reason not in {"gatt_connect", "gatt_or_service_discovery"}:
            return
        preferred = self._last_successful_proxy()
        if preferred is None or preferred == address:
            return
        preferred_visible = self._has_fresh_ble_device(preferred)
        if (
            not preferred_visible
            and preferred not in self._round_failed_candidates
        ):
            return
        health = self._candidate_health_summary(address)
        if int(health["gatt_failures"]) < PREOPEN_FALLBACK_MIN_FAILURES:
            return

        probe_after = (
            self._candidate_round + PREOPEN_FALLBACK_SKIP_ROUNDS + 1
        )
        previous = self._candidate_probe_after_round.get(address, 0)
        if previous >= probe_after:
            return
        self._candidate_probe_after_round[address] = probe_after
        key = "preopen_fallback_round_deferrals"
        self._connect_attempt_stats[key] = (
            self._connect_attempt_stats.get(key, 0) + 1
        )
        if not preferred_visible:
            key = "preopen_fallback_deferrals_after_preferred_attempt"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
        _LOGGER.info(
            "Deferring repeatedly failing Amaran BLE proxy %s until candidate "
            "round %s while last trusted proxy %s remains eligible",
            address,
            probe_after,
            preferred,
        )

    def _record_candidate_health_outcome(self, address: str, outcome: str) -> None:
        """Keep a bounded, in-memory history of complete connection outcomes."""
        if outcome not in CANDIDATE_HEALTH_WEIGHTS:
            outcome = "unknown"
        history = self._candidate_health_history.get(address)
        if history is None:
            history = deque(maxlen=CANDIDATE_HEALTH_HISTORY_SIZE)
            self._candidate_health_history[address] = history
        history.append(outcome)

    def _promote_candidate_health_to_stable(self, address: str) -> None:
        """Promote the latest ready sample after it survives the stability window."""
        history = self._candidate_health_history.get(address)
        if history and history[-1] == "ready":
            history[-1] = "stable"
            return
        self._record_candidate_health_outcome(address, "stable")

    def _replace_candidate_ready_outcome(self, address: str, outcome: str) -> None:
        """Replace a short-lived ready sample instead of counting it as a success."""
        history = self._candidate_health_history.get(address)
        if history and history[-1] == "ready":
            history[-1] = outcome
            return
        self._record_candidate_health_outcome(address, outcome)

    def _candidate_health_score(self, address: str) -> float:
        """Return a recency-weighted score for reaching a stable Mesh session."""
        history = self._candidate_health_history.get(address, ())
        score = 0.0
        for age, outcome in enumerate(reversed(history)):
            score += CANDIDATE_HEALTH_WEIGHTS.get(outcome, -1.0) * (
                CANDIDATE_HEALTH_DECAY**age
            )
        return score

    def _candidate_health_summary(self, address: str) -> dict[str, object]:
        """Return non-sensitive candidate scoring details for diagnostics."""
        history = list(self._candidate_health_history.get(address, ()))
        latest_outcome = history[-1] if history else None
        post_open_failures = sum(
            outcome in {"proxy_notification", "mesh_login", "post_login"}
            for outcome in history
        )
        return {
            "score": round(self._candidate_health_score(address), 3),
            "samples": len(history),
            "stable": history.count("stable"),
            "ready_unproven": history.count("ready"),
            "gatt_failures": sum(
                outcome in {"gatt_connect", "gatt_or_service_discovery"}
                for outcome in history
            ),
            "post_open_failures": post_open_failures,
            "short_disconnects": history.count("short_disconnect"),
            "probation_disconnects": history.count("probation_disconnect"),
            "stable_disconnects": history.count("stable_disconnect"),
            "latest_outcome": latest_outcome,
            "recent_failure": latest_outcome in CANDIDATE_FAILURE_OUTCOMES,
            "failure_reason": self._candidate_failure_reason.get(address),
            "failure_streak": self._candidate_failure_streak.get(address, 0),
            "cooldown_seconds": self._candidate_cooldown_remaining(address),
            "quarantine_seconds": self._candidate_quarantine_remaining(address),
            "recent_outcomes": history,
        }

    def _restart_candidate_round(self) -> None:
        """Restart a failed round from the last proxy proven stable."""
        round_members = self._candidate_round_members()
        broad_members = self._candidate_round_members(
            include_deferred=True,
            include_rssi_fallback=True,
        )
        focused_round = bool(
            round_members
            and broad_members
            and round_members != broad_members
        )
        if focused_round and round_members.issubset(
            self._round_failed_candidates
        ):
            self._rssi_focus_failed_rounds += 1
            key = "rssi_focus_failed_rounds"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
        elif not focused_round and self._rssi_focus_failed_rounds:
            self._rssi_focus_failed_rounds = 0
            key = "rssi_full_fallback_rounds"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
        self._candidate_round += 1
        self._expire_candidate_probe_rounds()
        self._connect_failure_count = 0
        self._round_failed_candidates.clear()
        self._ignored_stored_proxy = None
        addresses = self._proxy_addresses()
        preferred = self._last_successful_proxy()
        if preferred in addresses:
            self._candidate_cursor = addresses.index(preferred)
            key = "preferred_proxy_round_restarts"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            preferred_cooldown = self._preferred_proxy_preopen_cooldown()
            if preferred_cooldown is not None:
                _, cooldown = preferred_cooldown
                self._candidate_cooldown_until.pop(preferred, None)
                key = "preferred_proxy_cooldown_overrides"
                self._connect_attempt_stats[key] = (
                    self._connect_attempt_stats.get(key, 0) + 1
                )
                _LOGGER.info(
                    "Retrying last stable Amaran BLE proxy %s after the "
                    "full-round GATT recovery pause instead of waiting "
                    "another %.1fs of pre-open cooldown",
                    preferred,
                    cooldown,
                )
            _LOGGER.info(
                "Restarting Amaran BLE candidate round from last stable proxy %s",
                preferred,
            )

        cleared_reachable_cooldowns: list[str] = []
        for candidate in round_members:
            if self._candidate_quarantine_remaining(candidate) is not None:
                continue
            self._candidate_post_open_quarantine_until.pop(candidate, None)
            if self._candidate_failure_reason.get(candidate) not in {
                "gatt_connect",
                "gatt_or_service_discovery",
            }:
                continue
            if self._candidate_cooldown_remaining(candidate) is None:
                continue
            self._candidate_cooldown_until.pop(candidate, None)
            cleared_reachable_cooldowns.append(candidate)
        if cleared_reachable_cooldowns:
            key = "reachable_preopen_cooldown_overrides"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0)
                + len(cleared_reachable_cooldowns)
            )
            _LOGGER.info(
                "Restarting Amaran BLE candidate round with %s reachable "
                "pre-open cooldown(s) cleared after the recovery pause",
                len(cleared_reachable_cooldowns),
            )
        self._notify_diagnostics()

    def _expire_candidate_probe_rounds(self) -> None:
        """Release candidates whose bounded skipped round has elapsed."""
        released = [
            address
            for address, probe_after in self._candidate_probe_after_round.items()
            if probe_after <= self._candidate_round
        ]
        for address in released:
            self._candidate_probe_after_round.pop(address, None)
        if released:
            key = "preopen_fallback_reprobes_released"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + len(released)
            )

    def _candidate_round_members(
        self,
        *,
        include_deferred: bool = False,
        include_rssi_fallback: bool = False,
    ) -> set[str]:
        """Return candidates that can participate in the current connect round."""
        configured = set(self._proxy_addresses())
        now = self.hass.loop.time()
        for address, seen_at in list(self._candidate_advertisement_seen_at.items()):
            if address not in configured or now - seen_at > ADVERTISEMENT_MAX_AGE:
                self._candidate_advertisement_seen_at.pop(address, None)
        fresh = {
            address
            for address in configured
            if self._has_fresh_ble_device(address)
        } | set(self._candidate_advertisement_seen_at)
        fresh_without_quarantine = {
            address
            for address in fresh
            if self._candidate_post_open_quarantine_until.get(address, 0.0)
            <= now
        }
        # Powered-off fixtures never advertise and therefore cannot record a
        # connection failure. Once at least one fresh proxy exists, limit the
        # round to those reachable candidates so offline fixtures cannot keep
        # round open forever. Likewise, when at least one fresh candidate is
        # eligible, do not let another candidate's post-open quarantine hold
        # that round open. If every fresh candidate is quarantined, keep them
        # as members so the connector waits instead of overriding quarantine.
        # The local last-seen memory survives HA advertisement-history clearing,
        # so a GATT resync cannot temporarily expand the round to powered-off
        # configured fixtures. With no real recent advertisement at all, retain
        # the configured set and wait for a fixture to reappear.
        members = fresh_without_quarantine or fresh or configured
        selected = members
        if not include_deferred and fresh:
            eligible = {
                address
                for address in members
                if self._candidate_probe_after_round.get(address, 0)
                <= self._candidate_round
            }
            # If every currently advertising fixture is deferred, failover
            # must win over the skipped round because the preferred fixture
            # disappeared.
            selected = eligible or members
        if (
            not RSSI_FOCUS_ENABLED
            or not fresh
            or include_rssi_fallback
            or self._rssi_focus_failed_rounds
            >= RSSI_FOCUS_MAX_FAILED_ROUNDS
        ):
            return selected
        return self._rssi_focus_members(selected) or selected

    def _ownership_alternative_evidence(
        self,
        address: str,
    ) -> tuple[set[str], set[str]]:
        """Return active and expired alternatives attempted this ownership cycle."""
        now = self.hass.loop.time()
        active: set[str] = set()
        expired: set[str] = set()
        for alternative in self._ownership_attempted_candidates - {address}:
            attempted_at = self._ownership_candidate_attempted_at.get(alternative)
            # Missing timestamps can only come from pre-beta.92 test/runtime
            # state. Fail closed rather than accidentally enabling a retry.
            if (
                attempted_at is None
                or now - attempted_at
                <= OWNERSHIP_ALTERNATIVE_EVIDENCE_SECONDS
            ):
                active.add(alternative)
            else:
                expired.add(alternative)
        return active, expired

    def _rssi_focus_members(self, members: set[str]) -> set[str] | None:
        """Return a clearly stronger visible signal tier, when one exists."""
        readings = {
            address: rssi
            for address in members
            if isinstance((rssi := self._candidate_rssi(address)), int)
        }
        if len(readings) < 2:
            return None
        strongest = max(readings.values())
        focused = {
            address
            for address, rssi in readings.items()
            if rssi >= strongest - RSSI_FOCUS_WINDOW_DB
        }
        if not focused or focused == members:
            return None
        return focused

    def _preferred_proxy_preopen_cooldown(self) -> tuple[str, float] | None:
        """Return a last-stable proxy still cooling after a pre-open failure."""
        preferred = self._last_successful_proxy()
        if preferred not in self._proxy_addresses():
            return None
        latest_outcome = self._candidate_health_summary(preferred)[
            "latest_outcome"
        ]
        if latest_outcome not in {
            "gatt_connect",
            "gatt_or_service_discovery",
        }:
            return None
        cooldown = self._candidate_cooldown_remaining(preferred)
        if cooldown is None:
            return None
        return preferred, cooldown

    def _remember_operational_candidate(self, address: str) -> None:
        """Accept an operational proxy without awarding trusted health yet."""
        self._mark_candidate_connected(address)
        now = self.hass.loop.time()
        for candidate, until in list(
            self._candidate_post_open_quarantine_until.items()
        ):
            if until <= now:
                self._candidate_post_open_quarantine_until.pop(candidate, None)

        protected = set(self._candidate_post_open_quarantine_until)
        protected.discard(address)
        stale_backoffs = (
            set(self._candidate_failure_streak)
            | set(self._candidate_cooldown_until)
            | set(self._candidate_failure_reason)
        )
        cleared_backoffs = stale_backoffs - protected
        for candidate in cleared_backoffs:
            self._candidate_failure_streak.pop(candidate, None)
            self._candidate_cooldown_until.pop(candidate, None)
            self._candidate_failure_reason.pop(candidate, None)
        self._candidate_failure_streak.pop(address, None)
        self._candidate_failure_reason.pop(address, None)
        self._candidate_probe_after_round.clear()
        self._rssi_focus_failed_rounds = 0

        if cleared_backoffs:
            key = "stable_mesh_backoff_resets"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            _LOGGER.info(
                "Amaran BLE proxy %s is stable; cleared %s stale pre-open "
                "failure backoff(s)",
                address,
                len(cleared_backoffs),
            )
        if protected:
            key = "stable_mesh_quarantines_preserved"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            _LOGGER.info(
                "Amaran BLE proxy %s is stable; preserving post-open "
                "quarantine for %s candidate(s)",
                address,
                len(protected),
            )
        self._round_failed_candidates.clear()
        self._ignored_stored_proxy = None

    def _remember_successful_candidate(self, address: str) -> None:
        """Persist a trusted proxy after the extended probation window."""
        self._promote_candidate_health_to_stable(address)
        self._remember_operational_candidate(address)
        self._store_last_successful_proxy(address)

    def _update_session_maturity(
        self,
        session: _ProxySession,
        now: float,
    ) -> None:
        """Advance one ready session from operational to trusted."""
        if session.connected_at is None:
            return
        uptime = now - session.connected_at
        if not session.stable and uptime >= STABLE_CONNECTION_SECONDS:
            session.stable = True
            self._remember_operational_candidate(session.address)
            self._notify_diagnostics()
            _LOGGER.info(
                "Amaran BLE mesh proxy %s passed the %.0fs operational "
                "stability window",
                session.address,
                STABLE_CONNECTION_SECONDS,
            )
        if (
            session.stable
            and not session.trusted
            and uptime >= TRUSTED_CONNECTION_SECONDS
        ):
            session.trusted = True
            self._remember_successful_candidate(session.address)
            key = "trusted_mesh_connections"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            self._notify_diagnostics()
            _LOGGER.info(
                "Amaran BLE mesh proxy %s passed the %.0fs trusted "
                "stability window",
                session.address,
                TRUSTED_CONNECTION_SECONDS,
            )

    def _store_last_successful_proxy(self, address: str) -> None:
        """Persist the last working proxy so the next restart starts there."""
        self._store_option(CONF_LAST_SUCCESSFUL_PROXY, address)

    def _store_option(self, key: str, value: object) -> None:
        """Persist one integration runtime option."""
        entry = self._entry
        config_entries = getattr(self.hass, "config_entries", None)
        update_entry = getattr(config_entries, "async_update_entry", None)
        if entry is None or update_entry is None:
            return
        options = dict(getattr(entry, "options", {}) or {})
        if options.get(key) == value:
            return
        options[key] = value
        update_entry(entry, options=options)

    async def _connect_candidate_order(self, addresses: list[str]) -> list[str]:
        """Prefer candidates that Home Assistant can currently see advertising."""
        round_members = self._candidate_round_members()
        round_addresses = [
            address for address in addresses if address in round_members
        ]
        if len(round_addresses) != len(addresses):
            key = "candidate_round_member_filters"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            _LOGGER.debug(
                "Amaran BLE candidate round %s restricted connect selection "
                "from %s configured proxies to %s eligible members",
                self._candidate_round,
                len(addresses),
                len(round_addresses),
            )
        addresses = round_addresses
        if not addresses:
            return []
        ordered = self._rotated_proxy_addresses(addresses)
        ordered = self._filter_candidate_cooldowns(ordered)
        ordered = self._filter_candidate_probe_rounds(ordered)
        ordered = [
            address
            for address in ordered
            if address not in self._round_failed_candidates
        ]
        if not ordered:
            return []
        visible = [address for address in ordered if self._get_ble_device(address) is not None]
        startup_scan_active = self._startup_scan_active()
        if startup_scan_active:
            startup_fresh = [
                address
                for address in visible
                if self._has_fresh_ble_device(
                    address,
                    max_age=STARTUP_CONNECT_ADVERTISEMENT_MAX_AGE,
                )
            ]
            recent_cached = [
                address
                for address in visible
                if address not in startup_fresh
                and self._has_fresh_ble_device(
                    address,
                    max_age=ADVERTISEMENT_MAX_AGE,
                )
            ]
            if startup_fresh:
                fresh_order = self._sort_visible_candidates(startup_fresh)
                cached_order = self._sort_visible_candidates(recent_cached)
                return [*fresh_order, *cached_order][
                    :CONNECT_CANDIDATES_PER_ROUND
                ]
            seen_address = await self._wait_for_any_proxy_advertisement(
                ordered,
                max_age=STARTUP_CONNECT_ADVERTISEMENT_MAX_AGE,
            )
            if seen_address is not None:
                return [seen_address]
            if recent_cached:
                _LOGGER.debug(
                    "Using recent cached Amaran BLE proxy advertisements after "
                    "the startup scan found no newer candidate"
                )
                return self._sort_visible_candidates(recent_cached)[
                    :CONNECT_CANDIDATES_PER_ROUND
                ]
            if visible:
                _LOGGER.debug(
                    "Waiting for a recent Amaran BLE proxy advertisement before startup GATT connect"
                )
            return []

        fresh = [address for address in visible if self._has_fresh_ble_device(address)]
        if fresh:
            return self._sort_visible_candidates(fresh)[:CONNECT_CANDIDATES_PER_ROUND]

        seen_address = await self._wait_for_any_proxy_advertisement(ordered)
        if seen_address is not None:
            ordered = [
                seen_address,
                *(address for address in ordered if address != seen_address),
            ]
            visible = [
                address for address in ordered if self._has_fresh_ble_device(address)
            ]
            if visible:
                return self._sort_visible_candidates(visible)[:CONNECT_CANDIDATES_PER_ROUND]
            return [seen_address]

        if visible:
            _LOGGER.debug(
                "No fresh Amaran BLE proxy advertisements; waiting before GATT connect"
            )
        return []

    def _preferred_ownership_first_candidate(
        self,
        addresses: list[str],
    ) -> str | None:
        """Use a fresh trusted proxy once at the start of an ownership cycle."""
        if not self._preferred_ownership_first_attempt_pending:
            return None
        self._preferred_ownership_first_attempt_pending = False
        preferred = self._last_successful_proxy()
        if preferred is None or preferred not in addresses:
            return None
        now = self.hass.loop.time()
        max_age = (
            STARTUP_CONNECT_ADVERTISEMENT_MAX_AGE
            if self._startup_scan_active()
            else ADVERTISEMENT_MAX_AGE
        )
        if (
            preferred in self._round_failed_candidates
            or self._candidate_cooldown_until.get(preferred, 0.0) > now
            or self._candidate_post_open_quarantine_until.get(preferred, 0.0) > now
            or self._candidate_probe_after_round.get(preferred, 0)
            > self._candidate_round
            or not self._has_fresh_ble_device(preferred, max_age=max_age)
        ):
            return None
        key = "preferred_proxy_ownership_first_attempts"
        self._connect_attempt_stats[key] = (
            self._connect_attempt_stats.get(key, 0) + 1
        )
        _LOGGER.info(
            "Starting Amaran BLE ownership cycle with fresh last stable proxy %s",
            preferred,
        )
        return preferred

    def _filter_candidate_probe_rounds(self, addresses: list[str]) -> list[str]:
        """Apply a bounded round skip without blocking the only visible node."""
        eligible = [
            address
            for address in addresses
            if self._candidate_probe_after_round.get(address, 0)
            <= self._candidate_round
        ]
        if len(eligible) == len(addresses):
            return addresses
        if any(self._has_fresh_ble_device(address) for address in eligible):
            return eligible
        return addresses

    def _filter_candidate_cooldowns(self, addresses: list[str]) -> list[str]:
        """Return candidates that are not in a short failure cooldown."""
        now = self.hass.loop.time()
        for address, until in list(self._candidate_cooldown_until.items()):
            if until <= now:
                self._candidate_cooldown_until.pop(address, None)
        for address, until in list(
            self._candidate_post_open_quarantine_until.items()
        ):
            if until <= now:
                self._candidate_post_open_quarantine_until.pop(address, None)
        available = [
            address
            for address in addresses
            if self._candidate_cooldown_until.get(address, 0.0) <= now
            and self._candidate_post_open_quarantine_until.get(address, 0.0) <= now
        ]
        if available:
            return available
        if addresses:
            _LOGGER.debug(
                "All Amaran BLE proxy candidates are cooling down after failed connects"
            )
        return []

    def _startup_scan_active(self) -> bool:
        """Return whether startup should wait for a very recent advertisement."""
        return self.hass.loop.time() < self._startup_scan_until

    def _has_fresh_ble_device(
        self,
        address: str,
        *,
        max_age: float = ADVERTISEMENT_MAX_AGE,
    ) -> bool:
        """Return whether a candidate has a recent connectable advertisement."""
        if self._get_ble_device(address) is None:
            return False
        age = self._last_advertisement_age(address)
        if age is None:
            return True
        if age <= max_age:
            return True
        _LOGGER.debug(
            "Ignoring stale Amaran BLE proxy advertisement for %s: age %.1fs",
            address,
            age,
        )
        return False

    def _last_advertisement_age(self, address: str) -> float | None:
        """Return age in seconds of Home Assistant's latest service info."""
        service_info = self._last_service_info(address)
        return self._service_info_age(service_info)

    def _service_info_age(self, service_info: object | None) -> float | None:
        """Return age in seconds for one Bluetooth service info object."""
        if service_info is None:
            return None
        seen_time = getattr(service_info, "time", None)
        if seen_time is None:
            return None
        with contextlib.suppress(TypeError, ValueError):
            age = self.hass.loop.time() - float(seen_time)
            return max(0.0, age)
        return None

    def _service_info_is_fresh(
        self,
        service_info: object | None,
        *,
        max_age: float = ADVERTISEMENT_MAX_AGE,
    ) -> bool:
        """Return whether one Bluetooth service info item is fresh enough to connect."""
        age = self._service_info_age(service_info)
        return age is None or age <= max_age

    def _sort_visible_candidates(self, addresses: list[str]) -> list[str]:
        """Prefer candidates that recently reached a complete stable Mesh session."""
        last_successful_proxy = self._last_successful_proxy()
        if self._active_proxy_address in addresses:
            return [
                self._active_proxy_address,
                *(address for address in addresses if address != self._active_proxy_address),
            ]
        original_index = {address: index for index, address in enumerate(addresses)}

        def sort_key(address: str) -> tuple[int, float, int, int, int, float, int]:
            service_info = self._last_service_info(address)
            rssi = getattr(service_info, "rssi", None)
            if rssi is None and (ble_device := self._get_ble_device(address)) is not None:
                rssi = getattr(ble_device, "rssi", None)
            seen_time = getattr(service_info, "time", 0.0) or 0.0
            with contextlib.suppress(TypeError, ValueError):
                seen_time = float(seen_time)
            health = self._candidate_health_summary(address)
            stable_bonus = (
                CANDIDATE_LAST_STABLE_BONUS
                if address == last_successful_proxy
                and address != self._ignored_stored_proxy
                else 0.0
            )
            return (
                0 if bool(health["recent_failure"]) else 1,
                float(health["score"]) + stable_bonus,
                int(health["stable"]),
                -int(health["post_open_failures"]),
                int(rssi) if isinstance(rssi, (int, float)) else -999,
                seen_time if isinstance(seen_time, float) else 0.0,
                -original_index[address],
            )

        sorted_addresses = sorted(addresses, key=sort_key, reverse=True)
        _LOGGER.debug(
            "Amaran BLE visible proxy order: %s",
            ", ".join(
                f"{address}(health={self._candidate_health_score(address):.2f},"
                f"samples={len(self._candidate_health_history.get(address, ()))},"
                f"latest={self._candidate_health_summary(address)['latest_outcome']},"
                f"streak={self._candidate_failure_streak.get(address, 0)},"
                f"cooldown={self._candidate_cooldown_remaining(address)},"
                f"rssi={self._candidate_rssi(address)})"
                for address in sorted_addresses
            ),
        )
        return sorted_addresses

    def _candidate_rssi(self, address: str) -> int | None:
        """Return the last RSSI Home Assistant saw for one candidate."""
        service_info = self._last_service_info(address)
        rssi = getattr(service_info, "rssi", None)
        if rssi is None and (ble_device := self._get_ble_device(address)) is not None:
            rssi = getattr(ble_device, "rssi", None)
        return int(rssi) if isinstance(rssi, (int, float)) else None

    def _advertisement_snapshot(self, address: str) -> dict[str, object]:
        """Return non-sensitive advertisement details for one connect attempt."""
        service_info = self._last_service_info(address)
        age = self._service_info_age(service_info)
        source = getattr(service_info, "source", None)
        return {
            "age_seconds": round(age, 3) if age is not None else None,
            "rssi": self._candidate_rssi(address),
            "source": str(source) if source is not None else None,
            "connectable": (
                self._service_info_is_connectable(service_info)
                if service_info is not None
                else None
            ),
        }

    def _last_service_info(self, address: str) -> object | None:
        """Return Home Assistant's most recent Bluetooth service info."""
        last_service_info = getattr(bluetooth, "async_last_service_info", None)
        if last_service_info is None:
            return None
        try:
            return last_service_info(self.hass, address, connectable=True)
        except TypeError:
            return last_service_info(self.hass, address)
    async def _wait_for_any_proxy_advertisement(
        self,
        addresses: list[str],
        *,
        max_age: float = ADVERTISEMENT_MAX_AGE,
    ) -> str | None:
        """Wait briefly for any configured proxy candidate advertisement."""
        candidates = {address.upper() for address in addresses}
        if not candidates:
            return None

        _LOGGER.debug(
            "Waiting up to %ss for any Amaran BLE proxy advertisement",
            DISCOVERY_TIMEOUT,
        )
        try:
            service_info = await bluetooth.async_process_advertisements(
                self.hass,
                lambda service_info: (
                    str(service_info.address).upper() in candidates
                    and self._service_info_is_connectable(service_info)
                    and self._service_info_is_fresh(service_info, max_age=max_age)
                ),
                {"addresses": tuple(sorted(candidates))},
                bluetooth.BluetoothScanningMode.PASSIVE,
                DISCOVERY_TIMEOUT,
            )
        except TimeoutError:
            return None
        except Exception:
            _LOGGER.debug(
                "Unable to process passive advertisements for Amaran BLE proxy candidates",
                exc_info=True,
            )
            return None
        address = str(service_info.address).upper()
        return address if address in candidates else None

    @staticmethod
    def _service_info_is_connectable(service_info: object) -> bool:
        """Return whether a Bluetooth service info item can be connected to."""
        return getattr(service_info, "connectable", True) is not False

    def _session_lock(self, proxy_address: str) -> asyncio.Lock:
        """Return the per-proxy lock."""
        lock = self._session_locks.get(proxy_address)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[proxy_address] = lock
        return lock

    @staticmethod
    def _client_is_connected(client: object | None) -> bool:
        """Return whether a Bleak client still reports an active connection."""
        if client is None:
            return False
        is_connected = getattr(client, "is_connected", False)
        if callable(is_connected):
            return bool(is_connected())
        return bool(is_connected)

    @staticmethod
    def _format_error(err: BaseException | None) -> str:
        """Return a useful one-line exception description."""
        if err is None:
            return "unknown error"
        detail = str(err).strip()
        if detail:
            return f"{type(err).__name__}: {detail}"
        return type(err).__name__

    def _filter_addresses(self) -> list[int]:
        # This is the whitelist emitted by Amaran 1.0.71's libmesh SDK: the
        # controller's local unicast address and All Nodes.
        return [DEFAULT_LOCAL_ADDRESS, 0xFFFF]

    def is_light_available(self, address: str) -> bool:
        """Return whether one fixture is currently reachable through the mesh."""
        return self._light_available.get(address.upper(), False)

    def status_notifications_supported(self) -> bool:
        """Return whether the active proxy can report fixture status."""
        session = self._ready_session()
        if session is None:
            return True
        return session.status_notifications_supported

    def _notification_backoff_remaining(self, address: str) -> float | None:
        """Return how long notification setup should remain in write-only mode."""
        remaining = (
            self._notification_backoff_until.get(address, 0.0)
            - self.hass.loop.time()
        )
        if remaining <= 0:
            self._notification_backoff_until.pop(address, None)
            return None
        return round(remaining, 1)

    def async_track_availability(
        self,
        address: str,
        callback: Callable[[bool], None],
    ) -> Callable[[], None]:
        """Track whether this controller currently has a mesh proxy connection."""
        mac = address.upper()
        callbacks = self._availability_callbacks.setdefault(mac, set())
        callbacks.add(callback)

        def unsubscribe() -> None:
            callbacks.discard(callback)
            if not callbacks:
                self._availability_callbacks.pop(mac, None)

        return unsubscribe

    def async_track_status(
        self,
        mesh_address: int,
        callback: Callable[[telink.TelinkStatus], None],
    ) -> Callable[[], None]:
        """Track decoded Telink status reports for a fixture."""
        callbacks = self._status_callbacks.setdefault(mesh_address, set())
        callbacks.add(callback)

        def unsubscribe() -> None:
            callbacks.discard(callback)
            if not callbacks:
                self._status_callbacks.pop(mesh_address, None)

        return unsubscribe

    def async_track_fixture_state(
        self,
        mesh_address: int,
        callback: Callable[[FixtureColorState], None],
    ) -> Callable[[], None]:
        """Track shared color state changes for one fixture."""
        callbacks = self._fixture_state_callbacks.setdefault(
            mesh_address,
            set(),
        )
        callbacks.add(callback)

        def unsubscribe() -> None:
            callbacks.discard(callback)
            if not callbacks:
                self._fixture_state_callbacks.pop(mesh_address, None)

        return unsubscribe

    def fixture_state(self, mesh_address: int) -> FixtureColorState:
        """Return a detached snapshot of one fixture's shared color state."""
        return self._fixture_states[mesh_address].snapshot()

    def seed_fixture_state(
        self,
        mesh_address: int,
        state: FixtureColorState,
    ) -> None:
        """Seed restored state until the first physical status is received."""
        if mesh_address in self._last_status:
            return
        self._fixture_states[mesh_address] = state.snapshot()
        self._notify_fixture_state(mesh_address)

    def _notify_fixture_state(self, mesh_address: int) -> None:
        """Publish one fixture's latest shared color state."""
        snapshot = self.fixture_state(mesh_address)
        for callback in list(
            self._fixture_state_callbacks.get(mesh_address, ())
        ):
            callback(snapshot)

    def _handle_status(self, src: int, status: telink.TelinkStatus) -> None:
        """Dispatch one decoded status report."""
        _LOGGER.debug("Amaran BLE status from 0x%04x: %s", src, status)
        self._last_status[src] = status
        self._last_status_seen[src] = self.hass.loop.time()
        self._status_miss_count[src] = 0
        if self._mesh_available and self._control_enabled:
            self._status_seen_in_session.add(src)
        if (fixture_state := self._fixture_states.get(src)) is not None:
            fixture_state.apply_status(status)
            self._notify_fixture_state(src)
        if (mac := self._mac_for_mesh_address(src)) is not None:
            self._set_light_available(mac, True)
        for callback in list(self._status_callbacks.get(src, ())):
            callback(status)

    def last_status(self, mesh_address: int) -> telink.TelinkStatus | None:
        """Return the latest decoded status for one fixture."""
        return self._last_status.get(mesh_address)

    def _mac_for_mesh_address(self, mesh_address: int) -> str | None:
        """Return the configured MAC for a mesh unicast address."""
        for light in self.config.lights:
            if light.mesh_address == mesh_address:
                return light.mac
        return None

    def _set_light_available(self, address: str, available: bool) -> None:
        """Publish availability for one fixture."""
        mac = address.upper()
        if not self._control_enabled or not self._mesh_available:
            available = False
        if self._light_available.get(mac, False) == available:
            return
        self._light_available[mac] = available
        for callback in list(self._availability_callbacks.get(mac, ())):
            callback(available)
        self._notify_diagnostics()

    def _notify_all_light_availability(self) -> None:
        """Republish current availability for every fixture."""
        for light in self.config.lights:
            self._set_light_available(light.mac, self._light_available.get(light.mac, False))

    def _set_mesh_available(self, available: bool) -> None:
        """Publish mesh availability to all light entities."""
        available = bool(available and self._control_enabled)
        if self._mesh_available == available:
            if not available:
                self._notify_all_light_availability()
                self._notify_diagnostics()
            elif not self.status_notifications_supported():
                self._apply_write_only_availability()
            return
        self._mesh_available = available
        if available:
            self._mesh_session_sequence += 1
            self._mesh_session_started_at = self.hass.loop.time()
            self._status_seen_in_session.clear()
            self._status_miss_count.clear()
            self._initial_status_census_complete = False
        else:
            self._mesh_session_started_at = None
            self._status_seen_in_session.clear()
            self._initial_status_census_complete = False
        for callback in list(self._mesh_callbacks):
            callback(available)
        self._notify_diagnostics()
        if not available:
            for light in self.config.lights:
                self._set_light_available(light.mac, False)
        else:
            for light in self.config.lights:
                self._set_light_available(light.mac, False)
            if not self.status_notifications_supported():
                self._apply_write_only_availability()

    def _handle_proxy_disconnect(
        self,
        proxy_address: str,
        disconnected_client: object | None = None,
    ) -> None:
        """Publish a disconnect only when it belongs to the active proxy."""
        _LOGGER.debug("Amaran BLE proxy %s disconnected", proxy_address)
        if self._active_proxy_address != proxy_address:
            return
        session = self._sessions.get(proxy_address)
        if (
            disconnected_client is not None
            and (session is None or session.client is not disconnected_client)
        ):
            _LOGGER.debug(
                "Ignoring delayed disconnect callback from an obsolete Amaran BLE "
                "client for %s",
                proxy_address,
            )
            return
        uptime = 0.0
        if session is not None and session.connected_at is not None:
            uptime = max(0.0, self.hass.loop.time() - session.connected_at)
        self._active_proxy_address = None
        self._set_mesh_available(False)
        if self._stopping or not self._control_enabled:
            return

        if session is not None and session.trusted:
            self._record_candidate_health_outcome(proxy_address, "stable_disconnect")
        elif session is not None and session.stable:
            self._replace_candidate_ready_outcome(
                proxy_address,
                "probation_disconnect",
            )
        else:
            self._replace_candidate_ready_outcome(
                proxy_address,
                "short_disconnect",
            )

        disconnect_error = RuntimeError(
            f"Proxy disconnected after {uptime:.1f}s"
        )
        failure_reason = (
            "stable_disconnect"
            if session is not None and session.trusted
            else (
                "probation_disconnect"
                if session is not None and session.stable
                else "short_disconnect"
            )
        )
        self._advance_candidate_cursor(
            proxy_address,
            disconnect_error,
            failure_reason=failure_reason,
        )
        if uptime < STABLE_CONNECTION_SECONDS:
            quarantine_until = self.hass.loop.time() + SHORT_CONNECTION_COOLDOWN
            self._candidate_cooldown_until[proxy_address] = max(
                self._candidate_cooldown_until.get(proxy_address, 0.0),
                quarantine_until,
            )
            self._candidate_post_open_quarantine_until[proxy_address] = max(
                self._candidate_post_open_quarantine_until.get(proxy_address, 0.0),
                quarantine_until,
            )
            if proxy_address == self._last_successful_proxy():
                self._ignored_stored_proxy = proxy_address
        self._connect_failure_count += 1
        self._notify_diagnostics()

    def diagnostic_state(self, *, compact: bool = False) -> dict[str, object]:
        """Return a small, redacted runtime state summary."""
        release_guard = self._release_guard_remaining()
        cleanup_in_progress = self._connection_cleanup_in_progress()
        ready_session = self._ready_session()
        if not self._control_enabled and cleanup_in_progress:
            state = "releasing"
        elif not self._control_enabled:
            state = "released"
        elif self._mesh_available:
            state = (
                "connected"
                if ready_session is not None and ready_session.stable
                else "stabilizing"
            )
        elif release_guard > 0:
            state = "release_cooldown"
        elif self._connect_task is not None and not self._connect_task.done():
            state = "connecting"
        else:
            state = "unavailable"

        connected_seconds = None
        if ready_session is not None and ready_session.connected_at is not None:
            connected_seconds = round(
                max(0.0, self.hass.loop.time() - ready_session.connected_at),
                1,
            )

        command_traces = list(self._recent_command_traces)
        connect_attempts = list(self._connect_attempt_history)
        if compact:
            command_traces = command_traces[-COMPACT_COMMAND_TRACE_HISTORY_SIZE:]
            connect_attempts = connect_attempts[
                -COMPACT_CONNECT_ATTEMPT_HISTORY_SIZE:
            ]

        return {
            "state": state,
            "control_enabled": self._control_enabled,
            "mesh_available": self._mesh_available,
            "active_proxy": self._active_proxy_address,
            "connected_seconds": connected_seconds,
            "release_guard_seconds": (
                round(release_guard, 1) if release_guard > 0 else None
            ),
            "last_release_uptime_seconds": (
                round(self._last_release_uptime, 1)
                if self._last_release_uptime is not None
                else None
            ),
            "last_release_was_short": self._last_release_was_short,
            "last_release_connection_in_progress": self._last_release_in_progress,
            "connection_cleanup_in_progress": cleanup_in_progress,
            "last_esphome_slot_release": self._last_esphome_slot_release,
            "pending_esphome_slot_releases": sorted(
                self._pending_esphome_slot_releases
            ),
            "esphome_slot_recovery_wait_seconds": (
                round(max(0.0, self.hass.loop.time() - self._esphome_slot_recovery_started), 1)
                if self._pending_esphome_slot_releases
                and self._esphome_slot_recovery_started is not None
                else None
            ),
            "connection_stable": (
                ready_session.stable if ready_session is not None else False
            ),
            "connection_trusted": (
                ready_session.trusted if ready_session is not None else False
            ),
            "connection_trust_seconds_remaining": (
                round(
                    max(
                        0.0,
                        TRUSTED_CONNECTION_SECONDS
                        - (
                            self.hass.loop.time()
                            - ready_session.connected_at
                        ),
                    ),
                    1,
                )
                if ready_session is not None
                and ready_session.connected_at is not None
                and not ready_session.trusted
                else None
            ),
            "last_successful_proxy": self._last_successful_proxy(),
            "ignored_stored_proxy": self._ignored_stored_proxy,
            "candidate_cooldowns": self._candidate_cooldowns(),
            "candidate_quarantines": self._candidate_quarantines(),
            "candidate_failure_reasons": dict(self._candidate_failure_reason),
            "candidate_probe_after_round": dict(
                self._candidate_probe_after_round
            ),
            "preferred_ownership_first_attempt_pending": (
                self._preferred_ownership_first_attempt_pending
            ),
            "candidate_round_members": sorted(self._candidate_round_members()),
            "round_failed_candidates": sorted(self._round_failed_candidates),
            "candidate_cursor": self._candidate_cursor,
            "connect_failure_count": self._connect_failure_count,
            "ownership_cycle": self._ownership_cycle,
            "ownership_attempted_candidates": sorted(
                self._ownership_attempted_candidates
            ),
            "ownership_attempted_candidate_age_seconds": {
                address: round(
                    max(0.0, self.hass.loop.time() - attempted_at),
                    1,
                )
                for address, attempted_at in (
                    self._ownership_candidate_attempted_at.items()
                )
            },
            "candidate_round": self._candidate_round,
            "rssi_focus_failed_rounds": self._rssi_focus_failed_rounds,
            "rssi_focus_members": sorted(
                self._rssi_focus_members(
                    self._candidate_round_members(
                        include_deferred=True,
                        include_rssi_fallback=True,
                    )
                )
                or ()
            ),
            "connect_attempt_sequence": self._connect_attempt_sequence,
            "last_error": self._last_maintain_error,
            "last_error_hint": self._last_error_hint(),
            "connection_advice": self._connection_advice(),
            "status_notifications_supported": self.status_notifications_supported(),
            "mesh_session_sequence": self._mesh_session_sequence,
            "mesh_session_started_uptime_seconds": self._mesh_session_started_at,
            "initial_status_census_complete": self._initial_status_census_complete,
            "status_seen_in_session": [
                f"0x{address:04x}" for address in sorted(self._status_seen_in_session)
            ],
            "notification_backoffs": {
                address: remaining
                for address in self._proxy_addresses()
                if (
                    remaining := self._notification_backoff_remaining(address)
                ) is not None
            },
            "uncached_service_discovery": sorted(self._service_cache_bypass),
            "uncached_recovery_attempted": sorted(
                self._uncached_recovery_attempted
            ),
            "authorization_recovery_attempted": sorted(
                self._authorization_recovery_attempted
            ),
            "authorization_reconnect_seconds": (
                round(
                    max(
                        0.0,
                        self._authorization_reconnect_after
                        - self.hass.loop.time(),
                    ),
                    1,
                )
                or None
            ),
            "verified_gatt_cache_source": (
                self._verified_gatt_cache_template[2]
                if self._verified_gatt_cache_template is not None
                else None
            ),
            "shared_gatt_cache_sources": dict(self._shared_gatt_cache_sources),
            "bootstrap_gatt_cache_sources": sorted(
                self._bootstrap_gatt_cache_sources
            ),
            "shared_gatt_cache_incompatible": sorted(
                self._shared_gatt_cache_incompatible
            ),
            "cache_mode_semantics": "requested_use_services_cache",
            "connection_profile": {
                "name": "esphome_beta107_current_scanner_slot_recovery",
                "esphome_allocation_release_gate": True,
                "esphome_allocation_release_timeout_seconds": (
                    ESPHOME_SLOT_RELEASE_CONFIRMATION_TIMEOUT
                ),
                "esphome_persistent_slot_recovery": True,
                "esphome_slot_recovery_recheck_seconds": (
                    ESPHOME_SLOT_RECOVERY_RECHECK_TIMEOUT
                ),
                "single_candidate_connector_slot_retry": (
                    SINGLE_CANDIDATE_CONNECTOR_SLOT_RETRY_ENABLED
                ),
                "single_candidate_connector_slot_retry_min_failures": (
                    SINGLE_CANDIDATE_CONNECTOR_SLOT_RETRY_MIN_FAILURES
                ),
                "single_candidate_connector_slot_retry_max_attempts": (
                    SINGLE_CANDIDATE_CONNECTOR_SLOT_RETRY_MAX_ATTEMPTS
                ),
                "single_candidate_connector_slot_retry_membership": (
                    "broad_visible_and_bounded_ownership_cycle"
                ),
                "ownership_alternative_evidence_seconds": (
                    OWNERSHIP_ALTERNATIVE_EVIDENCE_SECONDS
                ),
                "preopen_scanner_cache_fallback_max_age_seconds": (
                    PREOPEN_SCANNER_CACHE_FALLBACK_MAX_AGE
                ),
                "deferred_multi_candidate_recovery_delay_seconds": (
                    DEFERRED_MULTI_CANDIDATE_RECOVERY_DELAY
                ),
                "advertisement_callback_scanning_mode": "passive",
                "wait_for_source_scanner_passive": (
                    WAIT_FOR_SOURCE_SCANNER_PASSIVE
                ),
                "candidate_policy": "all_fresh_visible_no_rssi_focus",
                "preopen_fallback_deferral_enabled": (
                    PREOPEN_FALLBACK_DEFERRAL_ENABLED
                ),
                "gatt_authorization_same_candidate_retry": (
                    GATT_AUTHORIZATION_SAME_CANDIDATE_RETRY
                ),
                "single_candidate_gatt_authorization_same_candidate_retry": (
                    SINGLE_CANDIDATE_GATT_AUTHORIZATION_RETRY
                ),
                "gatt_authorization_reconnect_settle_seconds": (
                    GATT_AUTHORIZATION_RECONNECT_SETTLE_SECONDS
                ),
                "force_fresh_gatt_discovery": FORCE_FRESH_GATT_DISCOVERY,
                "gatt_cache_scope": (
                    "telink_mesh_proxy_bootstrap_then_mesh_verified"
                ),
                "prime_telink_mesh_proxy_bootstrap_cache": (
                    PRIME_TELINK_MESH_PROXY_BOOTSTRAP_CACHE
                ),
                "telink_mesh_proxy_bootstrap_mtu": (
                    TELINK_MESH_PROXY_BOOTSTRAP_MTU
                ),
                "share_verified_gatt_cache_between_candidates": (
                    SHARE_VERIFIED_GATT_CACHE_BETWEEN_CANDIDATES
                ),
                "rssi_focus_enabled": RSSI_FOCUS_ENABLED,
                "rssi_focus_window_db": RSSI_FOCUS_WINDOW_DB,
                "rssi_focus_max_failed_rounds": (
                    RSSI_FOCUS_MAX_FAILED_ROUNDS
                ),
                "notification_setup_timeout_seconds": (
                    NOTIFICATION_SETUP_TIMEOUT
                ),
                "source_pre_connect_delay_seconds": OFFICIAL_CONNECT_DELAY,
                "pre_connect_delay_seconds": ESPHOME_PRE_CONNECT_DELAY,
                "source_physical_connect_timeout_seconds": (
                    OFFICIAL_GATT_CONNECT_TIMEOUT
                ),
                "backend_connect_timeout_seconds": GATT_CLIENT_CONNECT_TIMEOUT,
                "source_service_discovery_timeout_seconds": (
                    OFFICIAL_SERVICE_DISCOVERY_TIMEOUT
                ),
                "combined_establish_deadline_seconds": (
                    GATT_ESTABLISH_DEADLINE
                ),
                "preopen_advertisement_resync": (
                    PREOPEN_ADVERTISEMENT_RESYNC
                ),
                "preopen_advertisement_resync_scanning_mode": (
                    PREOPEN_ADVERTISEMENT_RESYNC_SCANNING_MODE
                ),
                "source_service_ready_delay_seconds": (
                    OFFICIAL_SERVICE_READY_DELAY
                ),
                "service_ready_delay_seconds": (
                    GATT_NOTIFICATION_SETTLE_SECONDS
                ),
                "proxy_filter_delay_seconds": PROXY_READY_DELAY,
                "filter_status_deadline_seconds": FILTER_STATUS_TIMEOUT,
                "requested_mtu": OFFICIAL_REQUESTED_MTU,
                "mtu_managed_by_backend": True,
                "post_connect_connection_params_enabled": (
                    POST_CONNECT_CONNECTION_PARAMS_ENABLED
                ),
                "post_connect_min_interval_units": POST_CONNECT_MIN_INTERVAL,
                "post_connect_max_interval_units": POST_CONNECT_MAX_INTERVAL,
                "post_connect_latency": POST_CONNECT_LATENCY,
                "post_connect_supervision_timeout_units": (
                    POST_CONNECT_SUPERVISION_TIMEOUT
                ),
                "post_connect_params_request_timeout_seconds": (
                    POST_CONNECT_PARAMS_REQUEST_TIMEOUT
                ),
            },
            "connect_attempt_stats": dict(self._connect_attempt_stats),
            "command_trace_semantics": (
                "completed_mesh_writes_without_payload_or_key_material"
            ),
            "command_trace_stats": dict(self._command_trace_stats),
            "recent_command_traces": command_traces,
            "proxy_candidates": self._proxy_addresses(),
            "candidate_health": {
                address: self._candidate_health_summary(address)
                for address in self._proxy_addresses()
            },
            "proxy_candidate_details": self._proxy_candidate_details(),
            "recent_connect_attempts": connect_attempts,
            "status_miss_count": dict(self._status_miss_count),
            "light_available": dict(self._light_available),
            "fixture_color_states": {
                f"0x{mesh_address:04x}": state.as_dict()
                for mesh_address, state in self._fixture_states.items()
            },
        }

    def _last_error_hint(self) -> str | None:
        """Return a compact category for the latest connection error."""
        if not self._last_maintain_error:
            return None
        detail = self._last_maintain_error.lower()
        if "slot recovery is still pending" in detail:
            return "proxy_slot_recovery"
        if "insufficient authorization" in detail:
            return "gatt_authorization"
        if "cccd notification setup failed" in detail:
            return "cccd_timeout"
        if "disconnect timed out: true" in detail:
            return "proxy_disconnect_timeout"
        if (
            "out of connection slots" in detail
            or "allocation is still assigned" in detail
        ):
            return "proxy_out_of_slots"
        if "esp_gatt_conn_conn_cancel" in detail:
            return "proxy_slot_closing"
        if "timeout waiting for connect response" in detail or "timeouterror" in detail:
            return "gatt_timeout"
        if "esp_gatt_conn_fail_establish" in detail:
            return "gatt_establish_failed"
        if "disconnected after" in detail or "is not connected" in detail:
            return "gatt_disconnected"
        if "device not found" in detail or "no candidates" in detail:
            return "not_seen"
        return "unknown"

    def _connection_advice(self) -> str | None:
        """Return concise operator guidance for the latest connection problem."""
        hint = self._last_error_hint()
        if hint == "proxy_slot_recovery":
            return (
                "Waiting for current ESPHome scanner allocation evidence; no new "
                "GATT request is sent. Check last_esphome_slot_release.snapshot_status "
                "and esphome_slot_recovery_wait_seconds."
            )
        if hint == "gatt_timeout":
            return (
                "Proxy can see advertisements but GATT connect is timing out; "
                "close vendor apps, keep the ESPHome Bluetooth Proxy near the fixtures, "
                "or use a native HA Bluetooth adapter for comparison."
            )
        if hint == "cccd_timeout":
            return (
                "The fixture connected, but Proxy Data Out notification setup "
                "timed out; the integration will reconnect once without the GATT "
                "service cache before rotating to another fixture."
            )
        if hint == "proxy_disconnect_timeout":
            return (
                "The ESPHome Bluetooth Proxy did not finish disconnect cleanup; "
                "release Amaran BLE control and wait before retrying, or compare with "
                "a closer/native Bluetooth adapter."
            )
        if hint == "proxy_out_of_slots":
            return (
                "The ESPHome Bluetooth Proxy reported no active connection slot; "
                "wait for other BLE connections to finish or add another proxy."
            )
        if hint == "proxy_slot_closing":
            return (
                "The ESPHome proxy reported a connection-slot close race; the "
                "integration will wait for the proxy's CLOSE event before handing "
                "off to another fixture."
            )
        if hint == "gatt_authorization":
            return (
                "The fixture rejected GATT authorization; verify the fixture is joined "
                "to the imported mesh and not locked by another controller."
            )
        if hint == "not_seen":
            return (
                "No fresh connectable advertisement is available through Home Assistant."
            )
        return None

    def _proxy_candidate_details(self) -> list[dict[str, object]]:
        """Return non-sensitive Bluetooth visibility details for each candidate."""
        details: list[dict[str, object]] = []
        for address in self._proxy_addresses():
            service_info = self._last_service_info(address)
            ble_device = self._get_ble_device(address)
            ble_details = getattr(ble_device, "details", None)
            rssi = self._candidate_rssi(address)
            age = self._last_advertisement_age(address)
            details.append(
                {
                    "address": address,
                    "visible": ble_device is not None,
                    "fresh": self._has_fresh_ble_device(address),
                    "connectable": (
                        self._service_info_is_connectable(service_info)
                        if service_info is not None
                        else None
                    ),
                    "rssi": rssi,
                    "age_seconds": round(age, 1) if age is not None else None,
                    "cooldown_seconds": self._candidate_cooldown_remaining(address),
                    "quarantine_seconds": self._candidate_quarantine_remaining(
                        address
                    ),
                    "failure_reason": self._candidate_failure_reason.get(address),
                    "probe_after_round": self._candidate_probe_after_round.get(
                        address
                    ),
                    "health_score": round(self._candidate_health_score(address), 3),
                    "health_samples": len(
                        self._candidate_health_history.get(address, ())
                    ),
                    "source": getattr(service_info, "source", None),
                    "address_type": (
                        ble_details.get("address_type")
                        if isinstance(ble_details, dict)
                        else None
                    ),
                }
            )
        return details

    def _candidate_cooldown_remaining(self, address: str) -> float | None:
        """Return candidate cooldown remaining in seconds."""
        until = max(
            self._candidate_cooldown_until.get(address, 0.0),
            self._candidate_post_open_quarantine_until.get(address, 0.0),
        )
        remaining = until - self.hass.loop.time()
        if remaining <= 0:
            return None
        return round(remaining, 1)

    def _candidate_quarantine_remaining(self, address: str) -> float | None:
        """Return post-open quarantine remaining in seconds."""
        remaining = (
            self._candidate_post_open_quarantine_until.get(address, 0.0)
            - self.hass.loop.time()
        )
        if remaining <= 0:
            return None
        return round(remaining, 1)

    def _candidate_cooldowns(self) -> dict[str, float]:
        """Return active candidate cooldowns."""
        return {
            address: remaining
            for address in self._proxy_addresses()
            if (remaining := self._candidate_cooldown_remaining(address)) is not None
        }

    def _candidate_quarantines(self) -> dict[str, float]:
        """Return active post-open candidate quarantines."""
        return {
            address: remaining
            for address in self._proxy_addresses()
            if (
                remaining := self._candidate_quarantine_remaining(address)
            )
            is not None
        }

    def _expire_stale_lights(self) -> None:
        """Mark fixtures unavailable when they stop answering refresh requests."""
        if not self._mesh_available:
            return
        if not self.status_notifications_supported():
            self._apply_write_only_availability()
            return
        now = self.hass.loop.time()
        for light in self.config.lights:
            if light.mesh_address not in self._status_seen_in_session:
                self._set_light_available(light.mac, False)
                continue
            seen_at = self._last_status_seen.get(light.mesh_address)
            if seen_at is None:
                self._set_light_available(light.mac, False)
                continue
            if now - seen_at > LIGHT_STATUS_UNAVAILABLE_SECONDS:
                self._set_light_available(light.mac, False)

    def _apply_write_only_availability(self) -> None:
        """Use physical reachability when Mesh status notifications are unavailable."""
        if not self._mesh_available:
            return
        for light in self.config.lights:
            available = light.mac == self._active_proxy_address
            if not available:
                age = self._last_advertisement_age(light.mac)
                available = (
                    age is not None
                    and age <= WRITE_ONLY_ADVERTISEMENT_MAX_AGE
                )
            self._set_light_available(light.mac, available)

    def _record_status_miss(self, mesh_address: int) -> None:
        """Record one missed status response and expire after repeated misses."""
        count = self._status_miss_count.get(mesh_address, 0) + 1
        self._status_miss_count[mesh_address] = count
        seen_at = self._last_status_seen.get(mesh_address)
        if (
            seen_at is None
            or mesh_address not in self._status_seen_in_session
        ):
            if (mac := self._mac_for_mesh_address(mesh_address)) is not None:
                self._set_light_available(mac, False)
            self._notify_diagnostics()
            return
        stale_for = self.hass.loop.time() - seen_at
        mac = self._mac_for_mesh_address(mesh_address)
        if (
            count >= STATUS_MISS_LIMIT
            and stale_for > LIGHT_STATUS_UNAVAILABLE_SECONDS
            and mac is not None
        ):
            self._set_light_available(mac, False)
        else:
            self._notify_diagnostics()

    def _ready_session(self) -> _ProxySession | None:
        """Return the currently connected proxy session, if one is ready."""
        addresses: list[str] = []
        if self._active_proxy_address:
            addresses.append(self._active_proxy_address)
        addresses.extend(self._sessions)
        for proxy_address in list(dict.fromkeys(addresses)):
            session = self._sessions.get(proxy_address)
            if (
                session is not None
                and self._client_is_connected(session.client)
                and session.proxy_data_in is not None
                and session.mesh_ready
            ):
                return session
        return None

    async def _ensure_ready_for_command(self) -> _ProxySession:
        """Return a ready proxy session, connecting inline for one user command."""
        if (release_guard := self._release_guard_remaining()) > 0:
            wait_seconds = max(1, int(release_guard + 0.999))
            raise RuntimeError(
                "Amaran BLE is safely releasing the previous connection; "
                f"retry in {wait_seconds}s"
            )
        deadline = self.hass.loop.time() + COMMAND_CONNECT_TIMEOUT
        last_error: Exception | None = None

        while not self._stopping:
            session = self._ready_session()
            if session is not None:
                return session

            remaining = deadline - self.hass.loop.time()
            if remaining <= 0:
                break

            try:
                task = self._start_connect_task()
                session = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=remaining,
                )
            except Exception as err:  # noqa: BLE001 - retry within the command budget.
                last_error = err
                self._set_mesh_available(False)
                if self._full_candidate_round_failed():
                    raise RuntimeError(
                        "All Amaran BLE proxy candidates failed in this connection "
                        "round; the background connector will retry after GATT recovery"
                    ) from err
                sleep_time = min(COMMAND_CONNECT_RETRY_DELAY, deadline - self.hass.loop.time())
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                continue

            self._set_mesh_available(True)
            self._last_maintain_error = None
            return session

        detail = f": {self._format_error(last_error)}" if last_error is not None else ""
        raise RuntimeError(
            "Amaran BLE mesh proxy did not connect within "
            f"{COMMAND_CONNECT_TIMEOUT:.0f}s{detail}"
        )

    async def _maintain_proxy_loop(self) -> None:
        """Keep one BLE Mesh Proxy connection ready after Home Assistant settles."""
        while not self._stopping:
            if not self._control_enabled:
                await self._control_enabled_event.wait()
                continue
            connect_delay = (
                max(self._next_connect_after, self._release_guard_until)
                - self.hass.loop.time()
            )
            if connect_delay > 0:
                await self._sleep_or_advertisement(connect_delay)
                continue
            retry_delay = MAINTAIN_RETRY_DELAYS[0]
            full_failure_round = False
            try:
                session = await self._ensure_active_proxy()
                self._set_mesh_available(True)
                self._last_maintain_error = None
                _LOGGER.info("Amaran BLE mesh proxy %s is connected", session.address)
                with contextlib.suppress(Exception):
                    await self._initial_refresh_all()
                next_refresh = self.hass.loop.time() + STATUS_REFRESH_INTERVAL
                while (
                    not self._stopping
                    and self._control_enabled
                    and self._client_is_connected(session.client)
                ):
                    now = self.hass.loop.time()
                    self._update_session_maturity(session, now)
                    if now >= next_refresh:
                        with contextlib.suppress(Exception):
                            await self.async_refresh_all_statuses(
                                timeout=INITIAL_STATUS_WAIT,
                            )
                        next_refresh = now + STATUS_REFRESH_INTERVAL
                    self._expire_stale_lights()
                    await asyncio.sleep(PROXY_HEALTH_CHECK_INTERVAL)
                self._set_mesh_available(False)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._set_mesh_available(False)
                detail = self._format_error(err)
                retry_delay = self._maintain_retry_delay(detail)
                full_failure_round = self._full_candidate_round_failed()
                if detail != self._last_maintain_error:
                    self._last_maintain_error = detail
                    self._notify_diagnostics()
                    _LOGGER.info("Amaran BLE mesh proxy is not connected yet: %s", detail)
                _LOGGER.debug(
                    "Unable to keep Amaran BLE mesh proxy connected; retrying in %ss",
                    retry_delay,
                    exc_info=True,
                )
            if not self._stopping:
                if full_failure_round:
                    _LOGGER.info(
                        "All Amaran BLE proxy candidates failed; pausing AutoConnect "
                        "for %ss so ESPHome can release GATT state",
                        retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    self._restart_candidate_round()
                else:
                    await self._sleep_or_advertisement(retry_delay)

    async def _sleep_or_advertisement(self, delay: float) -> None:
        """Sleep until timeout or a matching Bluetooth advertisement wakes us."""
        if delay <= 0:
            return
        self._advertisement_event.clear()
        try:
            await asyncio.wait_for(self._advertisement_event.wait(), timeout=delay)
        except TimeoutError:
            return

    async def _initial_refresh_all(self) -> None:
        """Request startup status a few times so every fixture can answer quickly."""
        if not self.status_notifications_supported():
            self._initial_status_census_complete = True
            self._apply_write_only_availability()
            self._notify_diagnostics()
            return
        self._status_seen_in_session.clear()
        self._status_miss_count.clear()
        self._initial_status_census_complete = False
        for light in self.config.lights:
            self._set_light_available(light.mac, False)
        expected_addresses = {light.mesh_address for light in self.config.lights}
        try:
            for attempt in range(INITIAL_STATUS_REFRESH_ATTEMPTS):
                await self.async_refresh_all_statuses(
                    timeout=INITIAL_STATUS_WAIT,
                )
                if expected_addresses and expected_addresses.issubset(
                    self._status_seen_in_session
                ):
                    break
                if attempt + 1 < INITIAL_STATUS_REFRESH_ATTEMPTS:
                    await asyncio.sleep(INITIAL_STATUS_REFRESH_DELAY)
        finally:
            self._initial_status_census_complete = True
            self._expire_stale_lights()
            _LOGGER.info(
                "Amaran BLE fixture census completed: %s/%s fixtures answered",
                len(expected_addresses & self._status_seen_in_session),
                len(expected_addresses),
            )
            self._notify_diagnostics()

    def _schedule_notification_upgrade(self, session: _ProxySession) -> None:
        """Try to add status notifications only after write control is stable."""
        if (
            session.status_notifications_supported
            or session.notification_callback is None
            or session.proxy_data_out is None
        ):
            return
        old_task = self._notification_upgrade_tasks.pop(session.address, None)
        if old_task is not None:
            old_task.cancel()
        self._notification_upgrade_tasks[session.address] = (
            self._create_background_task(
                self._upgrade_notifications_later(
                    session,
                    INITIAL_NOTIFICATION_DELAY,
                ),
                f"amaran_ble_notification_upgrade_{session.address}",
            )
        )

    async def _upgrade_notifications_later(
        self,
        session: _ProxySession,
        delay: float,
    ) -> None:
        """Enable status notifications on an already stable proxy connection."""
        try:
            retry_delay = delay
            while True:
                await asyncio.sleep(retry_delay)
                if (
                    self._stopping
                    or not self._control_enabled
                    or self._sessions.get(session.address) is not session
                    or not self._client_is_connected(session.client)
                    or session.client is None
                    or session.proxy_data_out is None
                    or session.notification_callback is None
                ):
                    return
                try:
                    await self._enable_proxy_notifications(session)
                except Exception as err:
                    self._notification_backoff_until[session.address] = (
                        self.hass.loop.time() + NOTIFICATION_RETRY_DELAY
                    )
                    _LOGGER.info(
                        "Amaran BLE proxy %s remains in write-only mode; status "
                        "notification upgrade failed and will retry: %s",
                        session.address,
                        self._format_error(err),
                    )
                    if not self._client_is_connected(session.client):
                        self._set_mesh_available(False)
                        return
                    retry_delay = NOTIFICATION_RETRY_DELAY
                    continue

                session.notify_started = True
                self._notification_backoff_until.pop(session.address, None)
                try:
                    await self._initialize_proxy_login(
                        session,
                        wait_for_filter_status=True,
                    )
                except Exception as err:
                    _LOGGER.info(
                        "Amaran BLE proxy %s notifications are enabled, but the "
                        "filter status confirmation was not observed: %s",
                        session.address,
                        self._format_error(err),
                    )
                _LOGGER.info(
                    "Amaran BLE proxy %s status notifications are enabled",
                    session.address,
                )
                with contextlib.suppress(Exception):
                    await self._initial_refresh_all()
                return
        except asyncio.CancelledError:
            raise
        finally:
            task = self._notification_upgrade_tasks.get(session.address)
            if task is asyncio.current_task():
                self._notification_upgrade_tasks.pop(session.address, None)

    async def _enable_proxy_notifications(self, session: _ProxySession) -> None:
        """Enable the Proxy channel required by an already provisioned Mesh."""
        if (
            session.client is None
            or session.proxy_data_out is None
            or session.notification_callback is None
        ):
            raise RuntimeError("Mesh Proxy notification channel is incomplete")
        # Proxy notifications are mandatory for filter status and light state.
        # This integration imports an existing Mesh and never provisions nodes,
        # so it deliberately does not write the optional Provisioning Data Out
        # CCCD. Some fixtures drop an otherwise healthy ESPHome GATT link with
        # status 133 when that unused handle is enabled.
        if not session.notify_started:
            await asyncio.wait_for(
                session.client.start_notify(
                    session.proxy_data_out,
                    session.notification_callback,
                ),
                timeout=NOTIFICATION_SETUP_TIMEOUT,
            )
            session.notify_started = True
            session.status_notifications_supported = True
        self._notification_backoff_until.pop(session.address, None)
        self._notify_diagnostics()

    @staticmethod
    def _ignore_provisioning_notification(_: int, data: bytearray) -> None:
        """Consume PB-Out notifications; provisioned control does not use them."""
        _ = data

    async def _enable_proxy_notifications_for_session(
        self,
        session: _ProxySession,
    ) -> bool:
        """Enable CCCD or retain a connected link in write-only recovery."""
        cached_session = session.address not in self._service_cache_bypass
        try:
            await self._enable_proxy_notifications(session)
        except Exception as err:  # noqa: BLE001 - reconnect owns the next retry.
            detail = self._format_error(err)
            if not isinstance(err, TimeoutError):
                # A descriptor/GATT error means the current link is unusable.
                # Terminal descriptor errors rotate immediately. In
                # particular, a remote authorization failure should not reuse
                # the same fixture's stale ESPHome GATT state.
                self._service_cache_bypass.discard(session.address)
                self._cccd_reconnect_pending.discard(session.address)
                self._cached_cccd_retry_attempted.discard(session.address)
                self._uncached_recovery_attempted.discard(session.address)
                self._notify_diagnostics()
                if "insufficient authorization" in detail.lower():
                    raise RuntimeError(
                        "Mesh Proxy CCCD authorization error; dispose GATT and "
                        f"rotate to another visible candidate: {detail}"
                    ) from err
                raise RuntimeError(
                    "Mesh Proxy CCCD notification setup failed with a terminal "
                    "GATT error; rotate candidates without same-candidate "
                    f"recovery: {detail}"
                ) from err
            if self._client_is_connected(session.client):
                await asyncio.sleep(WRITE_ONLY_RECOVERY_SETTLE_SECONDS)
            if self._client_is_connected(session.client):
                # Proxy evidence showed silent CCCD timeouts where GATT stayed
                # healthy until HA closed it. Keep only those links as a bounded
                # write-only fallback; explicit GATT errors never reach here.
                self._service_cache_bypass.discard(session.address)
                self._cccd_reconnect_pending.discard(session.address)
                self._cached_cccd_retry_attempted.discard(session.address)
                self._uncached_recovery_attempted.discard(session.address)
                self._notification_backoff_until[session.address] = (
                    self.hass.loop.time() + NOTIFICATION_RETRY_DELAY
                )
                _LOGGER.warning(
                    "Amaran BLE proxy %s timed out enabling status "
                    "notifications while GATT remained connected; retaining "
                    "this ownership in write-only recovery mode",
                    session.address,
                )
                self._notify_diagnostics()
                return False
            if (
                cached_session
                and session.address not in self._cached_cccd_retry_attempted
            ):
                self._cached_cccd_retry_attempted.add(session.address)
                self._cccd_reconnect_pending.add(session.address)
                self._notify_diagnostics()
                raise RuntimeError(
                    "Mesh Proxy CCCD notification setup failed; reconnect required "
                    "while preserving the known GATT service cache for one retry: "
                    f"{detail}"
                ) from err
            self._service_cache_bypass.add(session.address)
            if cached_session:
                self._cccd_reconnect_pending.add(session.address)
            await self._clear_gatt_service_cache(session)
            self._notify_diagnostics()
            raise RuntimeError(
                "Mesh Proxy CCCD notification setup failed; reconnect required "
                f"and the next service discovery will bypass cache: {detail}"
            ) from err
        self._service_cache_bypass.discard(session.address)
        self._cccd_reconnect_pending.discard(session.address)
        self._cached_cccd_retry_attempted.discard(session.address)
        self._uncached_recovery_attempted.discard(session.address)
        self._authorization_recovery_attempted.discard(session.address)
        self._authorization_reconnect_after = 0.0
        self._notify_diagnostics()
        return True

    async def _clear_gatt_service_cache(self, session: _ProxySession) -> None:
        """Ask the HA-managed client to discard services after a CCCD timeout."""
        clear_cache = getattr(session.client, "clear_cache", None)
        if clear_cache is None:
            return
        try:
            result = clear_cache()
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(
                    result,
                    timeout=SERVICE_CACHE_CLEAR_TIMEOUT,
                )
            _LOGGER.debug(
                "Amaran BLE proxy %s GATT service cache clear result: %s",
                session.address,
                result,
            )
        except Exception:
            _LOGGER.debug(
                "Unable to clear GATT service cache for Amaran BLE proxy %s",
                session.address,
                exc_info=True,
            )

    async def _initialize_proxy_login(
        self,
        session: _ProxySession,
        *,
        wait_for_filter_status: bool,
    ) -> bool:
        """Send the official beacon and two-step Mesh Proxy whitelist sequence."""
        if session.client is None or session.proxy_data_in is None:
            raise RuntimeError("Mesh Proxy write channel is incomplete")

        filter_status_event = session.filter_status_event
        if filter_status_event is not None:
            filter_status_event.clear()

        await self._write_proxy_data(
            session.client,
            session.proxy_data_in,
            mesh.build_secure_network_beacon(
                self.keys,
                iv_index=session.iv_index,
            ),
            "secure network beacon",
        )
        await asyncio.sleep(PROXY_READY_DELAY)

        # A write-only recovery session cannot observe either acknowledgement.
        # Preserve control by sending both messages with the existing short
        # spacing, but do not claim that the filter was confirmed.
        if not wait_for_filter_status or filter_status_event is None:
            await self._write_proxy_data(
                session.client,
                session.proxy_data_in,
                mesh.build_set_filter_type_pdu(
                    self.keys,
                    sequence=self._next_sequence(),
                    iv_index=session.iv_index,
                ),
                "proxy filter type",
            )
            await asyncio.sleep(PROXY_READY_DELAY)
            await self._write_proxy_data(
                session.client,
                session.proxy_data_in,
                mesh.build_add_addresses_pdu(
                    self.keys,
                    sequence=self._next_sequence(),
                    addresses=self._filter_addresses(),
                    iv_index=session.iv_index,
                ),
                "proxy add addresses",
            )
            return False

        loop = asyncio.get_running_loop()
        filter_deadline = loop.time() + FILTER_STATUS_TIMEOUT

        async def wait_for_filter_status(stage: str) -> bool:
            remaining = filter_deadline - loop.time()
            if remaining <= 0:
                _LOGGER.debug(
                    "Amaran BLE proxy %s exhausted the official %.1fs filter "
                    "deadline before %s confirmation",
                    session.address,
                    FILTER_STATUS_TIMEOUT,
                    stage,
                )
                return False
            try:
                await asyncio.wait_for(
                    filter_status_event.wait(),
                    timeout=remaining,
                )
            except TimeoutError:
                _LOGGER.debug(
                    "Amaran BLE proxy %s did not report %s within the official "
                    "%.1fs filter deadline",
                    session.address,
                    stage,
                    FILTER_STATUS_TIMEOUT,
                )
                return False
            _LOGGER.debug(
                "Amaran BLE proxy %s confirmed %s",
                session.address,
                stage,
            )
            return True

        # Amaran 1.0.71 advances this state machine only when a white-list
        # Proxy Filter Status arrives. Sending Add Addresses early can race the
        # first acknowledgement on a remote ESPHome Bluetooth proxy.
        await self._write_proxy_data(
            session.client,
            session.proxy_data_in,
            mesh.build_set_filter_type_pdu(
                self.keys,
                sequence=self._next_sequence(),
                iv_index=session.iv_index,
            ),
            "proxy filter type",
        )
        set_filter_confirmed = await wait_for_filter_status("Set Filter Type")
        filter_status_event.clear()
        await self._write_proxy_data(
            session.client,
            session.proxy_data_in,
            mesh.build_add_addresses_pdu(
                self.keys,
                sequence=self._next_sequence(),
                addresses=self._filter_addresses(),
                iv_index=session.iv_index,
            ),
            "proxy add addresses",
        )
        if not set_filter_confirmed:
            # The phone app tears down GATT here. A remote HA Bluetooth proxy
            # can take much longer to reacquire that scarce slot, so retain the
            # healthy link and finish the filter writes as an explicit,
            # unconfirmed recovery path.
            return False
        return await wait_for_filter_status("Add Addresses")

    def _maintain_retry_delay(self, detail: str = "") -> int:
        """Return a bounded backoff delay after repeated BLE connect failures."""
        if self._full_candidate_round_failed():
            round_members = self._candidate_round_members()
            broad_round_members = self._candidate_round_members(
                include_deferred=True,
                include_rssi_fallback=True,
            )
            if self._preopen_failure_storm_active():
                _LOGGER.info(
                    "Amaran BLE pre-open failure storm detected after %s "
                    "consecutive attempts; pausing AutoConnect for %ss",
                    self._consecutive_preopen_failures,
                    PREOPEN_STORM_RECOVERY_DELAY,
                )
                return PREOPEN_STORM_RECOVERY_DELAY
            if len(round_members) == 1 and len(broad_round_members) > 1:
                key = "deferred_multi_candidate_quiet_windows"
                self._connect_attempt_stats[key] = (
                    self._connect_attempt_stats.get(key, 0) + 1
                )
                _LOGGER.info(
                    "Amaran BLE candidate deferral reduced a %s-proxy Mesh to "
                    "one eligible proxy; preserving a %ss radio quiet window",
                    len(broad_round_members),
                    DEFERRED_MULTI_CANDIDATE_RECOVERY_DELAY,
                )
                return DEFERRED_MULTI_CANDIDATE_RECOVERY_DELAY
            if (
                len(round_members) == 1
                and self._candidate_failure_reason.get(next(iter(round_members)))
                == "gatt_connect"
            ):
                # With one powered fixture left there is no second address to
                # rotate through. Retry after a short radio recovery window;
                # the next attempt waits for a new passive advertisement
                # instead of reusing the same BLEDevice indefinitely. Passive
                # mode avoids overlapping an ESPHome active scan with GATT.
                return SINGLE_CANDIDATE_ADVERTISEMENT_RESYNC_RETRY_DELAY
            if self._preferred_proxy_preopen_cooldown() is not None:
                return PREFERRED_PROXY_GATT_RECOVERY_DELAY
            return RECOVERY_RETRY_DELAY
        detail_lower = detail.lower()
        if (
            "out of connection slots" in detail_lower
            or "allocation is still assigned" in detail_lower
            or "slot recovery is still pending" in detail_lower
            or "esp_gatt_conn_conn_cancel" in detail_lower
        ):
            return OUT_OF_SLOTS_RETRY_DELAY
        if (
            "disconnect timed out: true" in detail_lower
        ):
            return GATT_ESTABLISH_RETRY_DELAY
        if (
            "esp_gatt_conn_fail_establish" in detail_lower
            or "timeout waiting for connect response" in detail_lower
            or "timeouterror" in detail_lower
            or "disconnected after" in detail_lower
            or "is not connected" in detail_lower
        ):
            return GATT_ESTABLISH_RETRY_DELAY
        if "no candidates" in detail_lower or "device not found" in detail_lower:
            return NO_FRESH_ADVERTISEMENT_RETRY_DELAY
        index = min(
            max(self._connect_failure_count - 1, 0),
            len(MAINTAIN_RETRY_DELAYS) - 1,
        )
        return MAINTAIN_RETRY_DELAYS[index]

    def _preopen_failure_storm_active(self) -> bool:
        """Return whether two complete candidate rounds failed before GATT open."""
        candidate_count = len(
            self._candidate_round_members(include_deferred=True)
        )
        return bool(candidate_count) and self._consecutive_preopen_failures >= (
            candidate_count * PREOPEN_STORM_ROUNDS
        )

    def _full_candidate_round_failed(self) -> bool:
        """Return whether every currently reachable proxy failed this round."""
        addresses = self._candidate_round_members()
        return bool(addresses) and addresses.issubset(self._round_failed_candidates)

    def _get_ble_device(self, proxy_address: str):
        return bluetooth.async_ble_device_from_address(
            self.hass,
            proxy_address,
            connectable=True,
        )

    def _scanner_cached_ble_device(
        self,
        proxy_address: str,
        source: object,
    ) -> object | None:
        """Return the cached BLEDevice from the same connectable scanner path."""
        scanner_devices_by_address = getattr(
            bluetooth,
            "async_scanner_devices_by_address",
            None,
        )
        if scanner_devices_by_address is None or source is None:
            return None
        try:
            try:
                scanner_devices = scanner_devices_by_address(
                    self.hass,
                    proxy_address,
                    connectable=True,
                )
            except TypeError:
                scanner_devices = scanner_devices_by_address(self.hass, proxy_address)
        except Exception:
            _LOGGER.debug(
                "Unable to inspect scanner-local BLEDevice cache for Amaran BLE proxy %s",
                proxy_address,
                exc_info=True,
            )
            return None

        expected_source = str(source)
        for scanner_device in scanner_devices:
            scanner = getattr(scanner_device, "scanner", None)
            scanner_source = getattr(scanner, "source", None)
            ble_device = getattr(scanner_device, "ble_device", None)
            if (
                scanner_source is not None
                and str(scanner_source) == expected_source
                and ble_device is not None
            ):
                return ble_device
        return None

    @staticmethod
    def _scanner_mode_value(scanner: object | None) -> str | None:
        """Return a normalized read-only scanner mode."""
        if scanner is None:
            return None
        mode = getattr(scanner, "current_mode", None)
        value = getattr(mode, "value", mode)
        if value is None:
            return None
        normalized = str(value).lower()
        if normalized.endswith("active"):
            return "active"
        if normalized.endswith("passive"):
            return "passive"
        return normalized

    @classmethod
    def _scanner_mode_is_active(cls, scanner: object) -> bool:
        """Return whether one read-only HA scanner snapshot reports active mode."""
        return cls._scanner_mode_value(scanner) == "active"

    def _source_scanner_snapshot(self, proxy_address: str) -> dict[str, object]:
        """Return the source scanner mode without changing shared Bluetooth state."""
        service_info = self._last_service_info(proxy_address)
        source = getattr(service_info, "source", None)
        scanner_by_source = getattr(bluetooth, "async_scanner_by_source", None)
        scanner = (
            scanner_by_source(self.hass, source)
            if scanner_by_source is not None and source
            else None
        )
        return {
            "source": str(source) if source is not None else None,
            "mode": self._scanner_mode_value(scanner),
        }

    async def _wait_for_source_scanner_passive(self, proxy_address: str) -> bool:
        """Wait for an external scanner's active window without modifying it."""
        scanner_by_source = getattr(bluetooth, "async_scanner_by_source", None)
        service_info = self._last_service_info(proxy_address)
        source = getattr(service_info, "source", None)
        if scanner_by_source is None or not source:
            return False
        scanner = scanner_by_source(self.hass, source)
        initial_mode = self._scanner_mode_value(scanner)
        if scanner is None or initial_mode != "active":
            return False

        key = "scanner_active_waits"
        self._connect_attempt_stats[key] = self._connect_attempt_stats.get(key, 0) + 1
        started = self.hass.loop.time()
        deadline = started + SCANNER_PASSIVE_WAIT_TIMEOUT
        _LOGGER.info(
            "Waiting for Bluetooth scanner %s to return to passive mode before "
            "connecting Amaran BLE proxy %s",
            source,
            proxy_address,
        )
        final_mode = initial_mode
        while self.hass.loop.time() < deadline and final_mode == "active":
            await asyncio.sleep(SCANNER_MODE_POLL_INTERVAL)
            final_mode = self._scanner_mode_value(scanner)

        timed_out = final_mode == "active"
        elapsed = max(0.0, self.hass.loop.time() - started)
        trace = self._current_connect_trace
        if trace is not None and trace.get("address") == proxy_address:
            trace["scanner_wait"] = {
                "initial_mode": initial_mode,
                "final_mode": final_mode,
                "elapsed_seconds": round(elapsed, 3),
                "timed_out": timed_out,
            }

        if timed_out:
            key = "scanner_active_wait_timeouts"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            _LOGGER.warning(
                "Bluetooth scanner %s remained active for %.0fs; continuing "
                "without modifying the shared scanner",
                source,
                SCANNER_PASSIVE_WAIT_TIMEOUT,
            )
        else:
            key = "scanner_passive_wait_completions"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            await asyncio.sleep(POST_ACTIVE_SCAN_SETTLE_SECONDS)
        return True

    async def _prepare_ble_device_for_connect(
        self,
        proxy_address: str,
        *,
        resync_advertisement: bool,
    ) -> object:
        """Return a fresh BLEDevice after any shared active-scan window ends."""
        ble_device = await self._async_get_ble_device(
            proxy_address,
            resync_advertisement=resync_advertisement,
            allow_scan=True,
        )
        if ble_device is None:
            raise RuntimeError(f"Amaran BLE device not found: {proxy_address}")

        if (
            WAIT_FOR_SOURCE_SCANNER_PASSIVE
            and await self._wait_for_source_scanner_passive(proxy_address)
        ):
            ble_device = await self._async_get_ble_device(
                proxy_address,
                # The scanner has completed the requested active rediscovery
                # and settled. Reuse its latest passive device instead of
                # scheduling a second active window.
                resync_advertisement=False,
                allow_scan=True,
            )
            if ble_device is None:
                raise RuntimeError(
                    "Amaran BLE device did not advertise after the shared "
                    f"scanner settled: {proxy_address}"
                )
        return ble_device

    async def _async_get_ble_device(
        self,
        proxy_address: str,
        *,
        resync_advertisement: bool = False,
        allow_scan: bool = False,
    ):
        """Return a connectable BLE device, doing a short on-demand scan if needed."""
        stale_ble_device = None
        if (
            not resync_advertisement
            and (ble_device := self._get_ble_device(proxy_address)) is not None
        ):
            if self._has_fresh_ble_device(proxy_address):
                return ble_device
            stale_ble_device = ble_device
            _LOGGER.debug(
                "Amaran BLE proxy %s has only stale Bluetooth data; waiting for a fresh advertisement",
                proxy_address,
            )
        if not allow_scan:
            return None

        baseline_advertisement_time: float | None = None
        baseline_info: object | None = None
        baseline_source: object | None = None
        if resync_advertisement:
            baseline_info = self._last_service_info(proxy_address)
            baseline_source = getattr(baseline_info, "source", None)
            baseline_time = getattr(baseline_info, "time", None)
            if isinstance(baseline_time, (int, float)):
                baseline_advertisement_time = float(baseline_time)
            clear_history = getattr(
                bluetooth,
                "async_clear_advertisement_history",
                None,
            )
            if clear_history is not None:
                clear_history(self.hass, proxy_address)
                key = "advertisement_history_clears"
                self._connect_attempt_stats[key] = (
                    self._connect_attempt_stats.get(key, 0) + 1
                )

        _LOGGER.debug(
            "Waiting up to %ss for Amaran BLE proxy advertisement from %s",
            DISCOVERY_TIMEOUT,
            proxy_address,
        )
        deadline = self.hass.loop.time() + DISCOVERY_TIMEOUT
        while self.hass.loop.time() < deadline:
            timeout = max(0.1, min(1.0, deadline - self.hass.loop.time()))
            try:
                scan_mode = (
                    PREOPEN_ADVERTISEMENT_RESYNC_SCANNING_MODE
                    if resync_advertisement
                    else bluetooth.BluetoothScanningMode.PASSIVE
                )
                service_info = await bluetooth.async_process_advertisements(
                    self.hass,
                    lambda service_info: (
                        str(service_info.address).upper() == proxy_address
                        and self._service_info_is_connectable(service_info)
                        and self._service_info_is_fresh(service_info)
                        and (
                            baseline_advertisement_time is None
                            or (
                                isinstance(
                                    getattr(service_info, "time", None),
                                    (int, float),
                                )
                                and float(service_info.time)
                                > baseline_advertisement_time
                            )
                        )
                    ),
                    {"address": proxy_address},
                    scan_mode,
                    timeout,
                )
            except TimeoutError:
                if (
                    not resync_advertisement
                    and self._has_fresh_ble_device(proxy_address)
                ):
                    return self._get_ble_device(proxy_address)
                continue
            except Exception:
                _LOGGER.debug(
                    "Unable to process %s advertisements for Amaran BLE proxy %s",
                    "active" if resync_advertisement else "passive",
                    proxy_address,
                    exc_info=True,
                )
                if (
                    not resync_advertisement
                    and self._has_fresh_ble_device(proxy_address)
                ):
                    return self._get_ble_device(proxy_address)
                return None

            if not self._service_info_is_fresh(service_info):
                age = self._service_info_age(service_info)
                _LOGGER.debug(
                    "%s scan returned stale Amaran BLE proxy advertisement for %s: age %s",
                    "Active" if resync_advertisement else "Passive",
                    proxy_address,
                    f"{age:.1f}s" if age is not None else "unknown",
                )
                await asyncio.sleep(0.1)
                continue

            observed_time = getattr(service_info, "time", None)
            if (
                baseline_advertisement_time is not None
                and (
                    not isinstance(observed_time, (int, float))
                    or float(observed_time) <= baseline_advertisement_time
                )
            ):
                _LOGGER.debug(
                    "Ignoring replayed Amaran BLE proxy advertisement for %s "
                    "while waiting for a post-failure packet",
                    proxy_address,
                )
                await asyncio.sleep(0.1)
                continue

            ble_device = getattr(service_info, "device", None) or self._get_ble_device(
                proxy_address
            )
            if ble_device is not None:
                if resync_advertisement:
                    await asyncio.sleep(ADVERTISEMENT_RESYNC_CONNECT_SETTLE_SECONDS)
                return ble_device
            if self._has_fresh_ble_device(proxy_address):
                if resync_advertisement:
                    await asyncio.sleep(ADVERTISEMENT_RESYNC_CONNECT_SETTLE_SECONDS)
                return self._get_ble_device(proxy_address)
            await asyncio.sleep(0.1)

        if (
            resync_advertisement
            and baseline_info is not None
            and self._service_info_is_connectable(baseline_info)
            and self._service_info_is_fresh(
                baseline_info,
                max_age=PREOPEN_SCANNER_CACHE_FALLBACK_MAX_AGE,
            )
            and (
                scanner_cached_device := self._scanner_cached_ble_device(
                    proxy_address,
                    baseline_source,
                )
            )
            is not None
        ):
            baseline_age = self._service_info_age(baseline_info)
            key = "preopen_scanner_cache_fallbacks"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            trace = self._current_connect_trace
            if trace is not None and trace.get("address") == proxy_address:
                trace["advertisement_resync_fallback"] = {
                    "baseline_age_seconds": (
                        round(baseline_age, 3) if baseline_age is not None else None
                    ),
                    "max_age_seconds": PREOPEN_SCANNER_CACHE_FALLBACK_MAX_AGE,
                    "result": "same_scanner_cache",
                    "source": str(baseline_source),
                }
            _LOGGER.info(
                "No newer advertisement arrived for Amaran BLE proxy %s within "
                "%ss; retaining the %.1fs-old BLEDevice from scanner %s",
                proxy_address,
                DISCOVERY_TIMEOUT,
                baseline_age if baseline_age is not None else 0.0,
                baseline_source,
            )
            await asyncio.sleep(ADVERTISEMENT_RESYNC_CONNECT_SETTLE_SECONDS)
            return scanner_cached_device

        if stale_ble_device is not None:
            _LOGGER.debug(
                "No fresh Amaran BLE proxy advertisement for %s; skipping stale cached device",
                proxy_address,
            )
        return None

    async def _write_proxy_data(
        self,
        client: object,
        characteristic: object,
        data: bytes,
        label: str,
    ) -> None:
        """Write bytes to Proxy Data In using Write Without Response."""
        _LOGGER.debug(
            "Writing %s to Amaran proxy data in: len=%s response=False",
            label,
            len(data),
        )
        await client.write_gatt_char(characteristic, data, response=False)

    async def _ensure_active_proxy(self) -> _ProxySession:
        """Return a ready proxy session, trying candidates until one connects."""
        if (session := self._ready_session()) is not None:
            return session

        task = self._start_connect_task()
        return await asyncio.shield(task)

    def _start_connect_task(self) -> asyncio.Task[_ProxySession]:
        """Return the shared in-flight proxy connection task."""
        if self._connect_task is not None and not self._connect_task.done():
            return self._connect_task
        self._connect_task = self._create_background_task(
            self._connect_active_proxy_task(),
            "amaran_ble_connect_proxy",
        )
        self._connect_task.add_done_callback(self._clear_connect_task)
        self._notify_diagnostics()
        return self._connect_task

    def _clear_connect_task(self, task: asyncio.Task[_ProxySession]) -> None:
        """Clear the shared connection task once it completes."""
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()
        if self._connect_task is task:
            self._connect_task = None
            self._notify_diagnostics()

    async def _connect_active_proxy_task(self) -> _ProxySession:
        """Connect one proxy candidate while sharing work across callers."""
        async with self._connect_lock:
            if (session := self._ready_session()) is not None:
                return session
            return await self._connect_active_proxy_locked()

    async def _connect_active_proxy_locked(self) -> _ProxySession:
        """Run one official-style AutoConnect candidate attempt."""
        if not self._control_enabled:
            raise RuntimeError("Amaran BLE control switch is off")

        await self._wait_candidate_cleanup_tasks()
        if any(not task.done() for task in self._candidate_cleanup_tasks):
            raise RuntimeError(
                "Previous Amaran BLE GATT client is still being disposed"
            )
        await self._wait_for_pending_esphome_slot_recovery()

        candidate_addresses = self._proxy_addresses()
        if not candidate_addresses:
            raise RuntimeError("No Amaran BLE proxy candidates are configured")

        ordered_addresses = await self._connect_candidate_order(candidate_addresses)
        if not ordered_addresses:
            raise RuntimeError(
                "Unable to connect to any Amaran BLE mesh proxy: no candidates"
            )

        # The official libmesh stack stops its private scanner as soon as one
        # whitelisted node is selected. HA's scanner is shared, so leave it alone
        # and reuse its fresh connectable BLEDevice. If HA does not currently
        # have fresh data, wait for a passive advertisement; requesting ACTIVE
        # here can leave an ESPHome AUTO scanner active while that radio starts
        # GATT.
        proxy_address = ordered_addresses[0]
        started = self.hass.loop.time()
        self._ownership_attempted_candidates.add(proxy_address)
        self._ownership_candidate_attempted_at[proxy_address] = started
        self._connect_attempt_sequence += 1
        attempt_id = self._connect_attempt_sequence
        candidate_health = self._candidate_health_summary(proxy_address)
        self._current_connect_trace = {
            "address": proxy_address,
            "started": started,
            "attempt_id": attempt_id,
            "ownership_cycle": self._ownership_cycle,
            "candidate_round": self._candidate_round,
            "candidate_health_score": candidate_health["score"],
            "candidate_health_samples": candidate_health["samples"],
            "round_failed_before": sorted(self._round_failed_candidates),
            "requested_cache_mode": "undecided",
            "requested_cache_reason": "undecided",
            "advertisement_at_selection": self._advertisement_snapshot(
                proxy_address
            ),
            "scanner_at_selection": self._source_scanner_snapshot(proxy_address),
            "stages": {"candidate_selected": 0.0},
        }
        connector_slot_retry = self._should_use_connector_slot_retry(
            proxy_address
        )
        self._current_connect_trace["connector_internal_slot_retry"] = (
            connector_slot_retry
        )
        self._current_connect_trace["connector_gatt_attempts"] = 0
        if connector_slot_retry:
            key = "connector_internal_slot_retry_sessions"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            _LOGGER.info(
                "Using bleak-retry-connector slot-race recovery for the only "
                "reachable Amaran BLE proxy %s after %s recent pre-open "
                "failure(s)",
                proxy_address,
                candidate_health["gatt_failures"],
            )
        _LOGGER.debug(
            "Amaran BLE ownership=%s round=%s attempt=%s selected %s "
            "health=%s samples=%s failed_before=%s",
            self._ownership_cycle,
            self._candidate_round,
            attempt_id,
            proxy_address,
            candidate_health["score"],
            candidate_health["samples"],
            sorted(self._round_failed_candidates),
        )
        slot_recovery_resync = self._esphome_slot_recovery_resync_pending
        resync_advertisement = bool(
            slot_recovery_resync
            or (
                PREOPEN_ADVERTISEMENT_RESYNC
                and self._candidate_failure_reason.get(proxy_address)
                == "gatt_connect"
            )
        )
        if resync_advertisement:
            key = (
                "slot_recovery_advertisement_resyncs"
                if slot_recovery_resync
                else "gatt_failure_advertisement_resyncs"
            )
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            _LOGGER.info(
                "Waiting for a fresh passive advertisement from Amaran BLE "
                "proxy %s after %s",
                proxy_address,
                (
                    "ESPHome slot recovery"
                    if slot_recovery_resync
                    else "a pre-open GATT failure"
                ),
            )
        try:
            session = await self._ensure_proxy(
                proxy_address,
                resync_advertisement=resync_advertisement,
            )
        except asyncio.CancelledError:
            elapsed = self.hass.loop.time() - started
            self._record_connect_attempt(
                proxy_address,
                "cancelled",
                elapsed,
                "Connection attempt cancelled while releasing BLE control",
            )
            if proxy_address == self._active_proxy_address:
                self._active_proxy_address = None
            raise
        except Exception as err:  # noqa: BLE001 - the next loop rotates nodes.
            elapsed = self.hass.loop.time() - started
            detail = self._format_error(err)
            failure_reason = self._record_connect_attempt(
                proxy_address,
                "failed",
                elapsed,
                detail,
            )
            if proxy_address == self._active_proxy_address:
                self._active_proxy_address = None
            self._advance_candidate_cursor(
                proxy_address,
                err,
                failure_reason=failure_reason,
            )
            self._connect_failure_count += 1
            raise RuntimeError(
                "Unable to connect to any Amaran BLE mesh proxy: "
                f"{proxy_address}: {detail} after {elapsed:.1f}s"
            ) from err

        elapsed = self.hass.loop.time() - started
        if self._active_proxy_address != proxy_address:
            _LOGGER.info(
                "Using Amaran BLE mesh proxy %s after %.1fs",
                proxy_address,
                elapsed,
            )
        self._record_connect_attempt(proxy_address, "connected", elapsed)
        self._active_proxy_address = proxy_address
        self._mark_candidate_connected(proxy_address)
        self._connect_failure_count = 0
        await self._disconnect_inactive_sessions(keep=proxy_address)
        return session

    def _record_connect_attempt(
        self,
        address: str,
        result: str,
        elapsed: float,
        detail: str | None = None,
    ) -> str:
        """Keep a small diagnostic trail of recent proxy connection attempts."""
        item: dict[str, object] = {
            "address": address,
            "result": result,
            "elapsed": round(elapsed, 3),
        }
        if detail:
            item["detail"] = detail
        trace = self._current_connect_trace
        if trace is not None and trace.get("address") == address:
            stages = dict(trace.get("stages", {}))
            for key in (
                "attempt_id",
                "ownership_cycle",
                "candidate_round",
                "candidate_health_score",
                "candidate_health_samples",
                "round_failed_before",
                "advertisement_at_selection",
                "advertisement_at_connect",
                "scanner_at_selection",
                "scanner_at_connect",
                "scanner_wait",
                "shared_gatt_cache_source",
                "backend_cache_primed",
                "connector_internal_slot_retry",
                "connector_retry_round_members",
                "connector_retry_broad_round_members",
                "connector_retry_attempted_alternatives",
                "connector_gatt_attempts",
            ):
                item[key] = trace.get(key)
            item["requested_cache_mode"] = trace.get("requested_cache_mode")
            item["requested_cache_reason"] = trace.get("requested_cache_reason")
            item["stages"] = stages
            item["result_stage"] = self._connect_result_stage(
                result,
                detail,
                stages,
            )
            self._current_connect_trace = None
        else:
            item["result_stage"] = "ready" if result == "connected" else "unknown"
        if result == "connected":
            self._record_candidate_health_outcome(address, "ready")
            self._consecutive_preopen_failures = 0
        elif result == "failed":
            result_stage = str(item["result_stage"])
            self._record_candidate_health_outcome(
                address,
                result_stage,
            )
            if result_stage == "gatt_connect":
                self._consecutive_preopen_failures += 1
            else:
                self._consecutive_preopen_failures = 0
        self._record_connect_attempt_stats(item)
        self._connect_attempt_history.append(item)
        _LOGGER.debug(
            "Amaran BLE ownership=%s round=%s attempt=%s candidate=%s "
            "result=%s stage=%s cache=%s scanner=%s waited=%s elapsed=%.3fs",
            item.get("ownership_cycle"),
            item.get("candidate_round"),
            item.get("attempt_id"),
            address,
            result,
            item.get("result_stage"),
            item.get("requested_cache_mode"),
            (
                item.get("scanner_at_connect", {}).get("mode")
                if isinstance(item.get("scanner_at_connect"), Mapping)
                else None
            ),
            isinstance(item.get("scanner_wait"), Mapping),
            elapsed,
        )
        self._notify_diagnostics()
        return str(item["result_stage"])

    def _should_use_connector_slot_retry(self, address: str) -> bool:
        """Use upstream slot-race retry only for one repeatedly failing node."""
        if not SINGLE_CANDIDATE_CONNECTOR_SLOT_RETRY_ENABLED:
            return False
        health = self._candidate_health_summary(address)
        if int(health["gatt_failures"]) < (
            SINGLE_CANDIDATE_CONNECTOR_SLOT_RETRY_MIN_FAILURES
        ):
            return False

        round_members = self._candidate_round_members()
        broad_members = self._candidate_round_members(
            include_deferred=True,
            include_rssi_fallback=True,
        )
        attempted_alternatives, expired_alternatives = (
            self._ownership_alternative_evidence(address)
        )
        trace = self._current_connect_trace
        if trace is not None and trace.get("address") == address:
            trace["connector_retry_round_members"] = sorted(round_members)
            trace["connector_retry_broad_round_members"] = sorted(broad_members)
            trace["connector_retry_attempted_alternatives"] = sorted(
                attempted_alternatives
            )
            trace["connector_retry_expired_attempted_alternatives"] = sorted(
                expired_alternatives
            )

        if broad_members != {address} or attempted_alternatives:
            if attempted_alternatives:
                key = "connector_internal_slot_retry_ownership_suppressions"
                self._connect_attempt_stats[key] = (
                    self._connect_attempt_stats.get(key, 0) + 1
                )
                _LOGGER.debug(
                    "Keeping Amaran BLE proxy %s on the outer retry lifecycle; "
                    "ownership cycle already attempted alternatives %s",
                    address,
                    sorted(attempted_alternatives),
                )
            elif round_members == {address} and address in broad_members:
                key = "connector_internal_slot_retry_multi_candidate_suppressions"
                self._connect_attempt_stats[key] = (
                    self._connect_attempt_stats.get(key, 0) + 1
                )
                _LOGGER.debug(
                    "Keeping Amaran BLE proxy %s on the outer retry lifecycle; "
                    "broad round still has candidates %s",
                    address,
                    sorted(broad_members),
                )
            return False
        if expired_alternatives:
            key = "connector_internal_slot_retry_expired_ownership_releases"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            _LOGGER.debug(
                "Allowing one-survivor recovery for Amaran BLE proxy %s; "
                "ownership evidence for alternatives %s expired after %.0fs",
                address,
                sorted(expired_alternatives),
                OWNERSHIP_ALTERNATIVE_EVIDENCE_SECONDS,
            )
        return True

    def _mark_connect_stage(self, address: str, stage: str) -> None:
        """Record one non-secret connection stage elapsed time."""
        trace = self._current_connect_trace
        if trace is None or trace.get("address") != address:
            return
        started = trace.get("started")
        if not isinstance(started, (int, float)):
            return
        stages = trace.setdefault("stages", {})
        if isinstance(stages, dict):
            elapsed = round(max(0.0, self.hass.loop.time() - started), 3)
            stages[stage] = elapsed
            _LOGGER.debug(
                "Amaran BLE ownership=%s round=%s attempt=%s candidate=%s "
                "stage=%s elapsed=%.3fs",
                trace.get("ownership_cycle"),
                trace.get("candidate_round"),
                trace.get("attempt_id"),
                address,
                stage,
                elapsed,
            )

    def _set_connect_cache_decision(
        self,
        address: str,
        *,
        use_services_cache: bool,
        reason: str,
    ) -> None:
        """Attach the cache decision to the current diagnostic trace."""
        trace = self._current_connect_trace
        if trace is None or trace.get("address") != address:
            return
        trace["requested_cache_mode"] = (
            "with_cache" if use_services_cache else "without_cache"
        )
        trace["requested_cache_reason"] = reason

    def _mark_connect_advertisement_ready(self, address: str) -> None:
        """Record the advertisement used immediately before GATT establishment."""
        trace = self._current_connect_trace
        if trace is None or trace.get("address") != address:
            return
        snapshot = self._advertisement_snapshot(address)
        trace["advertisement_at_connect"] = snapshot
        scanner_snapshot = self._source_scanner_snapshot(address)
        trace["scanner_at_connect"] = scanner_snapshot
        self._mark_connect_stage(address, "advertisement_ready")
        _LOGGER.debug(
            "Amaran BLE ownership=%s round=%s attempt=%s candidate=%s "
            "advertisement age=%s rssi=%s source=%s connectable=%s scanner=%s",
            trace.get("ownership_cycle"),
            trace.get("candidate_round"),
            trace.get("attempt_id"),
            address,
            snapshot.get("age_seconds"),
            snapshot.get("rssi"),
            snapshot.get("source"),
            snapshot.get("connectable"),
            scanner_snapshot.get("mode"),
        )

    @staticmethod
    def _esphome_gatt_cache_context(
        client: object,
    ) -> tuple[object, int] | None:
        """Return a structurally compatible ESPHome GATT cache when available."""
        required_methods = (
            "get_gatt_services_cache",
            "set_gatt_services_cache",
            "get_gatt_mtu_cache",
            "set_gatt_mtu_cache",
        )
        candidates = (client, getattr(client, "_backend", None))
        for backend in candidates:
            if backend is None:
                continue
            cache = getattr(backend, "_cache", None)
            address = getattr(backend, "_address_as_int", None)
            if (
                cache is not None
                and isinstance(address, int)
                and all(
                    callable(getattr(cache, name, None))
                    for name in required_methods
                )
            ):
                return cache, address
        return None

    @staticmethod
    def _address_as_int(address: str) -> int:
        """Convert a normalized Bluetooth address to bleak-esphome's key."""
        return int(address.replace(":", ""), 16)

    @staticmethod
    def _esphome_slot_device(client: object) -> object | None:
        """Return the selected ESPHome slot tracker when the backend exposes it."""
        backend = getattr(client, "_amaran_backend", None)
        device = getattr(backend, "_bluetooth_device", None)
        if device is None:
            return None
        if not isinstance(getattr(device, "ble_connections_free", None), int):
            return None
        if not isinstance(getattr(device, "ble_connections_limit", None), int):
            return None
        if not isinstance(getattr(device, "ble_allocations", None), list):
            return None
        return device

    @staticmethod
    def _esphome_slot_snapshot(
        device: object,
        target: int,
    ) -> tuple[bool, int, int] | None:
        """Return one authoritative ESPHome allocation snapshot when available."""
        allocations = getattr(device, "ble_allocations", None)
        free = getattr(device, "ble_connections_free", None)
        limit = getattr(device, "ble_connections_limit", None)
        if not isinstance(allocations, list):
            return None
        if not isinstance(free, int) or not isinstance(limit, int):
            return None
        return target in allocations, free, limit

    def _current_esphome_slot_snapshot(
        self, device: object, target: int,
    ) -> tuple[tuple[bool, int, int] | None, str]:
        """Resolve allocations through the currently registered source scanner."""
        source = getattr(device, "mac_address", None)
        scanner_by_source = getattr(bluetooth, "async_scanner_by_source", None)
        if not source or not callable(scanner_by_source):
            if getattr(device, "available", None) is False:
                return None, "scanner_unavailable"
            snapshot = self._esphome_slot_snapshot(device, target)
            return snapshot, "backend_tracker" if snapshot is not None else "unsupported"

        # ESPHome recreates its tracker on API reconnect. Never use the old
        # backend's allocations once HA can resolve its current source.
        try:
            scanner = scanner_by_source(self.hass, str(source))
            if scanner is None:
                return None, "scanner_unavailable"
            if str(getattr(scanner, "source", "")).upper() != str(source).upper():
                return None, "source_mismatch"
            current_device = getattr(scanner, "_bluetooth_device", None)
            if getattr(current_device, "available", None) is False:
                return None, "scanner_unavailable"
            get_allocations = getattr(scanner, "get_allocations", None)
            if not callable(get_allocations):
                snapshot = self._esphome_slot_snapshot(current_device, target)
                return snapshot, "current_tracker" if snapshot is not None else "allocations_unknown"
            allocations = get_allocations()
        except Exception:
            _LOGGER.debug("Unable to read current ESPHome slot allocations", exc_info=True)
            return None, "lookup_failed"

        if allocations is None:
            return None, "allocations_unknown"
        if str(getattr(allocations, "adapter", "")).upper() != str(source).upper():
            return None, "source_mismatch"
        free = getattr(allocations, "free", None)
        limit = getattr(allocations, "slots", None)
        allocated = getattr(allocations, "allocated", None)
        if (
            not isinstance(free, int)
            or not isinstance(limit, int)
            or limit <= 0
            or not 0 <= free <= limit
            or not isinstance(allocated, list)
        ):
            return None, "allocations_unknown"
        try:
            targets = {self._address_as_int(address) for address in allocated}
        except (AttributeError, TypeError, ValueError):
            return None, "allocations_unknown"
        return (target in targets, free, limit), "current_scanner"

    def _record_esphome_slot_release(
        self,
        result: str,
        *,
        elapsed: float,
        free: int | None = None,
        limit: int | None = None,
        target_allocated: bool | None = None,
        snapshot_status: str | None = None,
        source: str | None = None,
    ) -> None:
        """Record redacted slot-release evidence for diagnostics."""
        key = f"esphome_slot_release_{result}"
        self._connect_attempt_stats[key] = (
            self._connect_attempt_stats.get(key, 0) + 1
        )
        self._last_esphome_slot_release = {
            "result": result,
            "elapsed_seconds": round(elapsed, 3),
            "free": free,
            "limit": limit,
            "target_allocated": target_allocated,
            "snapshot_status": snapshot_status,
            "source": source,
        }
        self._notify_diagnostics()

    async def _wait_for_pending_esphome_slot_recovery(
        self,
        *,
        timeout: float = ESPHOME_SLOT_RECOVERY_RECHECK_TIMEOUT,
    ) -> bool:
        """Block GATT until every timed-out ESPHome allocation is reusable."""
        if not self._pending_esphome_slot_releases:
            return False

        if self._esphome_slot_recovery_started is None:
            self._esphome_slot_recovery_started = self.hass.loop.time()

        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + max(0.0, timeout)

        while self._pending_esphome_slot_releases:
            recovered: list[tuple[str, int, int]] = []
            unavailable_trackers: list[str] = []
            pending_snapshot: tuple[bool, int, int] | None = None
            snapshot_status: str | None = None
            source: str | None = None
            pending_status: str | None = None
            pending_source: str | None = None

            for address, device in list(
                self._pending_esphome_slot_releases.items()
            ):
                snapshot, status = self._current_esphome_slot_snapshot(
                    device,
                    self._address_as_int(address),
                )
                source = getattr(device, "mac_address", None)
                snapshot_status = status
                if snapshot is None:
                    if status == "unsupported":
                        self._pending_esphome_slot_releases.pop(address, None)
                        unavailable_trackers.append(address)
                    elif pending_status is None:
                        pending_status, pending_source = status, source
                    continue

                target_allocated, free, limit = snapshot
                if not target_allocated and free > 0:
                    self._pending_esphome_slot_releases.pop(address, None)
                    recovered.append((address, free, limit))
                    continue
                if pending_status is None:
                    pending_snapshot = snapshot
                    pending_status, pending_source = status, source

            if not self._pending_esphome_slot_releases:
                elapsed = loop.time() - started
                self._esphome_slot_recovery_resync_pending = True
                self._esphome_slot_recovery_started = None
                if recovered:
                    _address, free, limit = recovered[-1]
                    self._record_esphome_slot_release(
                        "recovered",
                        elapsed=elapsed,
                        free=free,
                        limit=limit,
                        target_allocated=False,
                        snapshot_status=snapshot_status,
                        source=source,
                    )
                else:
                    self._record_esphome_slot_release(
                        "tracker_unavailable",
                        elapsed=elapsed,
                        snapshot_status=snapshot_status,
                        source=source,
                    )
                _LOGGER.info(
                    "ESPHome cleared %s pending Amaran BLE slot allocation(s); "
                    "waiting for a fresh passive advertisement before GATT",
                    len(recovered) + len(unavailable_trackers),
                )
                return True

            remaining = deadline - loop.time()
            if remaining <= 0:
                elapsed = loop.time() - started
                target_allocated, free, limit = pending_snapshot or (
                    None,
                    None,
                    None,
                )
                self._record_esphome_slot_release(
                    "recovery_pending",
                    elapsed=elapsed,
                    free=free,
                    limit=limit,
                    target_allocated=target_allocated,
                    snapshot_status=pending_status,
                    source=pending_source,
                )
                raise RuntimeError(
                    "ESPHome BLE slot recovery is still pending; no new GATT "
                    f"request was sent (snapshot={pending_status})"
                )

            await asyncio.sleep(
                min(ESPHOME_SLOT_RELEASE_POLL_INTERVAL, remaining)
            )

    async def _wait_for_esphome_slot_release(
        self,
        client: object,
        proxy_address: str,
        *,
        timeout: float = ESPHOME_SLOT_RELEASE_CONFIRMATION_TIMEOUT,
    ) -> bool | None:
        """Wait until ESPHome confirms that one target allocation is reusable."""
        device = self._esphome_slot_device(client)
        if device is None:
            self._record_esphome_slot_release("unsupported", elapsed=0.0)
            return None

        target = self._address_as_int(proxy_address)
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + max(0.0, timeout)

        while True:
            snapshot, snapshot_status = self._current_esphome_slot_snapshot(device, target)
            source = getattr(device, "mac_address", None)
            if snapshot is None and snapshot_status == "unsupported":
                self._record_esphome_slot_release(
                    "tracker_unavailable",
                    elapsed=loop.time() - started,
                )
                return None
            target_allocated, free, limit = snapshot or (None, None, None)

            if snapshot is not None and not target_allocated and free > 0:
                self._pending_esphome_slot_releases.pop(proxy_address, None)
                if not self._pending_esphome_slot_releases:
                    self._esphome_slot_recovery_started = None
                elapsed = loop.time() - started
                self._record_esphome_slot_release(
                    "confirmed",
                    elapsed=elapsed,
                    free=free,
                    limit=limit,
                    target_allocated=False,
                    snapshot_status=snapshot_status,
                    source=source,
                )
                if elapsed >= ESPHOME_SLOT_RELEASE_POLL_INTERVAL:
                    _LOGGER.info(
                        "ESPHome confirmed the Amaran BLE proxy slot release "
                        "after %.1fs (free=%s/%s)",
                        elapsed,
                        free,
                        limit,
                    )
                return True

            remaining = deadline - loop.time()
            if remaining <= 0:
                elapsed = loop.time() - started
                self._pending_esphome_slot_releases[proxy_address] = device
                if self._esphome_slot_recovery_started is None:
                    self._esphome_slot_recovery_started = self.hass.loop.time()
                self._record_esphome_slot_release(
                    "timeout",
                    elapsed=elapsed,
                    free=free,
                    limit=limit,
                    target_allocated=target_allocated,
                    snapshot_status=snapshot_status,
                    source=source,
                )
                guard_until = (
                    self.hass.loop.time() + RELEASE_RECONNECT_COOLDOWN
                )
                self._next_connect_after = max(
                    self._next_connect_after,
                    guard_until,
                )
                self._release_guard_until = max(
                    self._release_guard_until,
                    guard_until,
                )
                _LOGGER.warning(
                    "ESPHome did not confirm the Amaran BLE proxy slot release "
                    "within %.0fs (target_allocated=%s, free=%s/%s); applying "
                    "the bounded reconnect guard",
                    timeout,
                    target_allocated,
                    free,
                    limit,
                )
                return False

            # Do not wait on the retired tracker's future: a replacement
            # scanner will notify a different object. Re-resolve the source.
            await asyncio.sleep(
                min(ESPHOME_SLOT_RELEASE_POLL_INTERVAL, remaining)
            )

    @staticmethod
    def _build_telink_mesh_proxy_bootstrap_services() -> object:
        """Build the minimal verified Telink Mesh Proxy service table."""
        from bleak.backends.characteristic import BleakGATTCharacteristic
        from bleak.backends.descriptor import BleakGATTDescriptor
        from bleak.backends.service import (
            BleakGATTService,
            BleakGATTServiceCollection,
        )

        services = BleakGATTServiceCollection()
        service = BleakGATTService(
            None,
            TELINK_MESH_PROXY_SERVICE_HANDLE,
            MESH_PROXY_SERVICE_UUID,
        )
        services.add_service(service)

        def max_write_without_response() -> int:
            return TELINK_MESH_PROXY_BOOTSTRAP_MTU - 3

        data_in = BleakGATTCharacteristic(
            None,
            TELINK_MESH_PROXY_DATA_IN_HANDLE,
            PROXY_DATA_IN_UUID,
            ["write-without-response"],
            max_write_without_response,
            service,
        )
        services.add_characteristic(data_in)
        data_out = BleakGATTCharacteristic(
            None,
            TELINK_MESH_PROXY_DATA_OUT_HANDLE,
            PROXY_DATA_OUT_UUID,
            ["notify"],
            max_write_without_response,
            service,
        )
        services.add_characteristic(data_out)
        services.add_descriptor(
            BleakGATTDescriptor(
                None,
                TELINK_MESH_PROXY_DATA_OUT_CCCD_HANDLE,
                CCCD_UUID,
                data_out,
            )
        )
        return services

    def _prime_telink_mesh_proxy_bootstrap_cache(
        self,
        client: object,
        address: str,
    ) -> bool:
        """Prime a cold ESPHome backend with the standard Telink handles."""
        if (
            not PRIME_TELINK_MESH_PROXY_BOOTSTRAP_CACHE
            or address in self._shared_gatt_cache_incompatible
            or address in self._service_cache_bypass
        ):
            return False
        context = self._esphome_gatt_cache_context(client)
        if context is None:
            return False
        cache, backend_address = context
        if (
            cache.get_gatt_services_cache(backend_address)
            and cache.get_gatt_mtu_cache(backend_address)
        ):
            return False

        services = self._build_telink_mesh_proxy_bootstrap_services()
        cache.set_gatt_services_cache(backend_address, services)
        cache.set_gatt_mtu_cache(
            backend_address,
            TELINK_MESH_PROXY_BOOTSTRAP_MTU,
        )
        self._bootstrap_gatt_cache_sources.add(address)
        trace = self._current_connect_trace
        if trace is not None and trace.get("address") == address:
            trace["backend_cache_primed"] = True
            trace["bootstrap_gatt_cache_profile"] = "telink_mesh_proxy_v1"
        key = "bootstrap_gatt_cache_primes"
        self._connect_attempt_stats[key] = (
            self._connect_attempt_stats.get(key, 0) + 1
        )
        _LOGGER.info(
            "Primed Amaran BLE proxy %s with the standard Telink Mesh Proxy "
            "GATT handles so ESPHome can skip cold MTU/service waiting",
            address,
        )
        self._notify_diagnostics()
        return True

    def _prime_verified_gatt_cache(
        self,
        client: object,
        address: str,
    ) -> bool:
        """Prime one ESPHome client from a Mesh-verified peer service table."""
        template = self._verified_gatt_cache_template
        context = self._esphome_gatt_cache_context(client)
        if (
            template is None
            or context is None
            or address in self._shared_gatt_cache_incompatible
        ):
            return False

        services, mtu, source = template
        cache, backend_address = context
        if (
            cache.get_gatt_services_cache(backend_address)
            and cache.get_gatt_mtu_cache(backend_address)
        ):
            return False

        cache.set_gatt_services_cache(backend_address, services)
        cache.set_gatt_mtu_cache(backend_address, mtu)
        self._shared_gatt_cache_sources[address] = source
        trace = self._current_connect_trace
        if trace is not None and trace.get("address") == address:
            trace["shared_gatt_cache_source"] = source
            trace["backend_cache_primed"] = True
        key = "shared_gatt_cache_primes"
        self._connect_attempt_stats[key] = self._connect_attempt_stats.get(key, 0) + 1
        _LOGGER.info(
            "Primed Amaran BLE proxy %s with the Mesh-verified GATT service "
            "table from %s",
            address,
            source,
        )
        self._notify_diagnostics()
        return True

    def _discard_target_local_gatt_cache(
        self,
        client: object,
        address: str,
    ) -> bool:
        """Discard only one target's process-local bleak-esphome cache."""
        context = self._esphome_gatt_cache_context(client)
        if context is None:
            return False

        cache, backend_address = context
        clear_services = getattr(cache, "clear_gatt_services_cache", None)
        clear_mtu = getattr(cache, "clear_gatt_mtu_cache", None)
        if not callable(clear_services) or not callable(clear_mtu):
            return False

        clear_services(backend_address)
        clear_mtu(backend_address)
        self._shared_gatt_cache_sources.pop(address, None)
        trace = self._current_connect_trace
        if trace is not None and trace.get("address") == address:
            trace["target_local_gatt_cache_cleared"] = True
        key = "target_local_gatt_cache_clears"
        self._connect_attempt_stats[key] = (
            self._connect_attempt_stats.get(key, 0) + 1
        )
        return True

    def _capture_verified_gatt_cache(self, session: _ProxySession) -> None:
        """Retain and share the GATT topology only after Mesh login succeeds."""
        if session.client is None:
            return
        context = self._esphome_gatt_cache_context(session.client)
        if context is None:
            return
        cache, backend_address = context
        services = cache.get_gatt_services_cache(backend_address)
        mtu = cache.get_gatt_mtu_cache(backend_address)
        if services is None or not isinstance(mtu, int) or mtu <= 0:
            return

        self._verified_gatt_cache_template = (services, mtu, session.address)
        self._bootstrap_gatt_cache_sources.discard(session.address)
        self._shared_gatt_cache_sources.pop(session.address, None)
        self._shared_gatt_cache_incompatible.discard(session.address)
        seeded = 0
        for address in self._proxy_addresses():
            if (
                address == session.address
                or address in self._shared_gatt_cache_incompatible
            ):
                continue
            peer_address = self._address_as_int(address)
            if (
                cache.get_gatt_services_cache(peer_address)
                and cache.get_gatt_mtu_cache(peer_address)
            ):
                continue
            cache.set_gatt_services_cache(peer_address, services)
            cache.set_gatt_mtu_cache(peer_address, mtu)
            self._shared_gatt_cache_sources[address] = session.address
            seeded += 1

        if seeded:
            key = "shared_gatt_cache_seeded_candidates"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + seeded
            )
            _LOGGER.info(
                "Shared Mesh-verified GATT service table from %s with %s "
                "Amaran BLE proxy candidate(s)",
                session.address,
                seeded,
            )
            self._notify_diagnostics()

    async def _discard_failed_shared_gatt_cache(
        self,
        session: _ProxySession,
    ) -> None:
        """Fall back to discovery if a peer's shared handle table is invalid."""
        if session.address not in self._shared_gatt_cache_sources:
            return
        source = self._shared_gatt_cache_sources.pop(session.address)
        self._shared_gatt_cache_incompatible.add(session.address)
        self._service_cache_bypass.add(session.address)
        await self._clear_gatt_service_cache(session)
        key = "shared_gatt_cache_fallbacks"
        self._connect_attempt_stats[key] = self._connect_attempt_stats.get(key, 0) + 1
        _LOGGER.warning(
            "Amaran BLE proxy %s failed after using the shared GATT service "
            "table from %s; the next attempt will perform full service discovery",
            session.address,
            source,
        )
        self._notify_diagnostics()

    @staticmethod
    def _connect_result_stage(
        result: str,
        detail: str | None,
        stages: dict[str, object],
    ) -> str:
        """Classify the furthest completed connection stage."""
        if result == "connected":
            return "ready"
        if "gatt_services_ready" not in stages:
            detail_lower = (detail or "").lower()
            if (
                "esp_gatt_conn" in detail_lower
                or "connect response" in detail_lower
                or "failed to establish" in detail_lower
            ):
                return "gatt_connect"
            return "gatt_or_service_discovery"
        if "proxy_notifications_ready" not in stages:
            return "proxy_notification"
        if "mesh_login_ready" not in stages:
            return "mesh_login"
        return "post_login"

    def _record_connect_attempt_stats(self, item: dict[str, object]) -> None:
        """Accumulate compact attempt counters for long stress tests."""
        def increment(key: str) -> None:
            self._connect_attempt_stats[key] = self._connect_attempt_stats.get(key, 0) + 1

        result = str(item.get("result", "unknown"))
        cache_mode = str(item.get("requested_cache_mode", "unknown"))
        result_stage = str(item.get("result_stage", "unknown"))
        increment("attempts_total")
        increment(f"result_{result}")
        increment(f"cache_{cache_mode}_attempts")
        increment(f"cache_{cache_mode}_{result}")
        increment(f"stage_{result_stage}")
        scanner_snapshot = item.get("scanner_at_connect")
        scanner_mode = (
            scanner_snapshot.get("mode")
            if isinstance(scanner_snapshot, Mapping)
            else None
        )
        if isinstance(scanner_mode, str) and scanner_mode:
            increment(f"scanner_{scanner_mode}_attempts")
            increment(f"scanner_{scanner_mode}_{result}")
        if isinstance(item.get("scanner_wait"), Mapping):
            increment("scanner_waited_attempts")
            increment(f"scanner_waited_{result}")
        if item.get("connector_internal_slot_retry"):
            increment("connector_internal_slot_retry_results")
        if "gatt retry budget exhausted" in str(item.get("detail", "")).lower():
            increment("connector_internal_retry_budget_exhausted")

    def _track_candidate_cleanup_task(self, task: asyncio.Task) -> None:
        """Track cleanup that must survive the foreground connection attempt."""
        self._candidate_cleanup_tasks.add(task)
        task.add_done_callback(self._candidate_cleanup_done)

    def _candidate_cleanup_done(self, task: asyncio.Task[None]) -> None:
        """Forget one completed candidate cleanup task and consume errors."""
        self._candidate_cleanup_tasks.discard(task)
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()
        self._notify_diagnostics()

    async def _ensure_proxy(
        self,
        proxy_address: str,
        *,
        resync_advertisement: bool = False,
    ) -> _ProxySession:
        """Return a ready proxy session, reconnecting if needed."""
        async with self._session_lock(proxy_address):
            session = self._sessions.get(proxy_address)
            if (
                session is not None
                and self._client_is_connected(session.client)
                and session.proxy_data_in is not None
                and session.mesh_ready
            ):
                return session

            if session is not None:
                await self._disconnect_session(proxy_address)

            session = _ProxySession(proxy_address)
            self._sessions[proxy_address] = session
            try:
                await self._connect_session(
                    session,
                    resync_advertisement=resync_advertisement,
                )
            except BaseException:
                if (
                    SHARE_VERIFIED_GATT_CACHE_BETWEEN_CANDIDATES
                    and session.client is not None
                ):
                    await self._discard_failed_shared_gatt_cache(session)
                await self._disconnect_session(proxy_address)
                raise
            return session

    async def _connect_session(
        self,
        session: _ProxySession,
        *,
        resync_advertisement: bool = False,
    ) -> None:
        """Connect and prepare one BLE Mesh Proxy session."""
        proxy_address = session.address
        if not self._control_enabled:
            raise RuntimeError("Amaran BLE control switch is off")

        ble_device = await self._prepare_ble_device_for_connect(
            proxy_address,
            resync_advertisement=resync_advertisement,
        )
        if resync_advertisement and self._esphome_slot_recovery_resync_pending:
            self._esphome_slot_recovery_resync_pending = False
        self._mark_connect_advertisement_ready(proxy_address)

        # The Android SDK waits one second between closing its local GATT object
        # and connectGatt(). HA has already drained its remote client and the
        # ESPHome allocation before _connect_session starts, so an additional
        # delay is neither required nor useful on this transport.
        if ESPHOME_PRE_CONNECT_DELAY > 0:
            await asyncio.sleep(ESPHOME_PRE_CONNECT_DELAY)
            self._mark_connect_stage(
                proxy_address,
                "esphome_pre_connect_delay_complete",
            )

        def on_disconnect(disconnected_client: object) -> None:
            self.hass.loop.call_soon_threadsafe(
                self._handle_proxy_disconnect,
                proxy_address,
                disconnected_client,
            )

        _LOGGER.debug(
            "Connecting to Amaran BLE proxy %s with bleak-retry-connector",
            proxy_address,
        )
        client = await self._establish_bleak_client(
            ble_device,
            proxy_address,
            on_disconnect,
        )
        session.client = client
        self._mark_connect_stage(proxy_address, "gatt_services_ready")
        _LOGGER.debug(
            "Amaran BLE proxy %s connected with backend-managed MTU %s "
            "(official Android request: %s)",
            proxy_address,
            getattr(client, "mtu_size", "unknown"),
            OFFICIAL_REQUESTED_MTU,
        )
        await self._request_post_connect_connection_params(session)

        provisioning_data_out = client.services.get_characteristic(
            PROVISIONING_DATA_OUT_UUID
        )
        proxy_data_in = client.services.get_characteristic(PROXY_DATA_IN_UUID)
        proxy_data_out = client.services.get_characteristic(PROXY_DATA_OUT_UUID)
        if proxy_data_in is None or proxy_data_out is None:
            raise RuntimeError("Mesh Proxy Data In/Out characteristics were not found")
        session.provisioning_data_out = provisioning_data_out
        session.proxy_data_in = proxy_data_in
        session.proxy_data_out = proxy_data_out

        filter_status_seen = asyncio.Event()

        def on_notify(_: int, data: bytearray) -> None:
            if self._sessions.get(proxy_address) is not session:
                return
            payload = bytes(data)
            if payload and (payload[0] & 0x3F) == mesh.PROXY_TYPE_PROXY_CONFIG:
                filter_status_seen.set()
                return
            parsed_iv_index = mesh.parse_iv_index_from_proxy(payload)
            if parsed_iv_index is not None:
                session.iv_index = parsed_iv_index
                return
            access_message = mesh.parse_access_proxy_pdu(
                self.keys,
                payload,
                iv_index=session.iv_index,
            )
            if (
                access_message is None
                or access_message.opcode != TELINK_OPCODE
                or access_message.src == DEFAULT_LOCAL_ADDRESS
            ):
                return
            status = telink.decode_status(access_message.params)
            if status is None:
                _LOGGER.debug(
                    "Unparsed Amaran Telink payload from 0x%04x: %s",
                    access_message.src,
                    access_message.params.hex(),
                )
                return
            self._handle_status(access_message.src, status)

        session.notification_callback = on_notify
        session.filter_status_event = filter_status_seen
        session.status_notifications_supported = False
        notification_settle = self._notification_settle_seconds(proxy_address)
        _LOGGER.debug(
            "Amaran BLE proxy %s waiting %.1fs before official-style "
            "notification setup",
            proxy_address,
            notification_settle,
        )

        # Amaran 1.0.71's Telink-derived ProxyInit enables PB-Out and Proxy-Out
        # notifications before the authenticated beacon and filter sequence.
        await asyncio.sleep(notification_settle)
        notifications_ready = await self._enable_proxy_notifications_for_session(
            session
        )
        self._mark_connect_stage(
            proxy_address,
            (
                "proxy_notifications_ready"
                if notifications_ready
                else "proxy_notifications_write_only"
            ),
        )
        filter_confirmed = await self._initialize_proxy_login(
            session,
            wait_for_filter_status=notifications_ready,
        )
        if notifications_ready and not filter_confirmed:
            _LOGGER.debug(
                "Amaran BLE proxy %s did not report Proxy Filter Status; "
                "retaining the authenticated notification session",
                proxy_address,
            )
        elif not notifications_ready:
            _LOGGER.info(
                "Amaran BLE proxy %s completed Mesh login in write-only "
                "recovery mode; live status updates are unavailable for this "
                "ownership",
                proxy_address,
            )

        session.mesh_ready = True
        session.connected_at = self.hass.loop.time()
        if SHARE_VERIFIED_GATT_CACHE_BETWEEN_CANDIDATES:
            self._capture_verified_gatt_cache(session)
        self._mark_connect_stage(proxy_address, "mesh_login_ready")
        _LOGGER.debug(
            "Amaran BLE proxy %s completed official-style Proxy login",
            proxy_address,
        )

    async def _request_post_connect_connection_params(
        self,
        session: _ProxySession,
    ) -> None:
        """Request a high-priority GATT link without requiring backend support."""
        if not POST_CONNECT_CONNECTION_PARAMS_ENABLED:
            return

        client = session.client
        setter = getattr(client, "set_connection_params", None)
        if not callable(setter):
            key = "connection_params_method_unavailable"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            _LOGGER.debug(
                "Amaran BLE proxy %s backend has no connection-parameter API; "
                "continuing with backend defaults",
                session.address,
            )
            self._mark_connect_stage(
                session.address,
                "connection_params_method_unavailable",
            )
            return

        key = "connection_params_requests"
        self._connect_attempt_stats[key] = (
            self._connect_attempt_stats.get(key, 0) + 1
        )
        self._mark_connect_stage(session.address, "connection_params_requested")
        try:
            async with asyncio.timeout(POST_CONNECT_PARAMS_REQUEST_TIMEOUT):
                await setter(
                    min_interval=POST_CONNECT_MIN_INTERVAL,
                    max_interval=POST_CONNECT_MAX_INTERVAL,
                    latency=POST_CONNECT_LATENCY,
                    timeout=POST_CONNECT_SUPERVISION_TIMEOUT,
                )
        except Exception as err:  # noqa: BLE001 - advisory backend extension
            key = "connection_params_request_failures"
            self._connect_attempt_stats[key] = (
                self._connect_attempt_stats.get(key, 0) + 1
            )
            self._mark_connect_stage(
                session.address,
                "connection_params_request_failed",
            )
            _LOGGER.warning(
                "Amaran BLE proxy %s connection-parameter request failed; "
                "continuing with backend defaults: %s",
                session.address,
                err,
            )
            return

        key = "connection_params_request_returned"
        self._connect_attempt_stats[key] = (
            self._connect_attempt_stats.get(key, 0) + 1
        )
        self._mark_connect_stage(
            session.address,
            "connection_params_request_returned",
        )
        _LOGGER.debug(
            "Amaran BLE proxy %s connection-parameter request returned "
            "(interval=%d-%d, latency=%d, supervision_timeout=%d); backend "
            "feature support remains authoritative",
            session.address,
            POST_CONNECT_MIN_INTERVAL,
            POST_CONNECT_MAX_INTERVAL,
            POST_CONNECT_LATENCY,
            POST_CONNECT_SUPERVISION_TIMEOUT,
        )

    def _notification_settle_seconds(self, proxy_address: str) -> float:
        """Use extra settle time only after a silent CCCD timeout."""
        if proxy_address in self._cached_cccd_retry_attempted:
            return GATT_NOTIFICATION_RECOVERY_SETTLE_SECONDS
        return GATT_NOTIFICATION_SETTLE_SECONDS

    async def _drain_aborted_connect(self, tasks: list[asyncio.Task]) -> None:
        """Drain cancelled GATT work without blocking every future connection."""
        if not tasks:
            return

        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=CLIENT_CLEANUP_DRAIN_TIMEOUT,
            )
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
                task.add_done_callback(self._consume_cleanup_task_result)
            raise

        for task in done:
            self._consume_cleanup_task_result(task)

        if not pending:
            return

        for task in pending:
            task.cancel()
            task.add_done_callback(self._consume_cleanup_task_result)

        key = "forced_client_cleanup_cancellations"
        self._connect_attempt_stats[key] = (
            self._connect_attempt_stats.get(key, 0) + len(pending)
        )
        guard_until = self.hass.loop.time() + RELEASE_RECONNECT_COOLDOWN
        self._next_connect_after = max(
            self._next_connect_after,
            guard_until,
        )
        self._release_guard_until = max(
            self._release_guard_until,
            guard_until,
        )
        _LOGGER.warning(
            "Forced %s stalled Amaran BLE GATT cleanup task(s) to stop after "
            "%.0fs; reconnecting after the proxy slot settle guard",
            len(pending),
            CLIENT_CLEANUP_DRAIN_TIMEOUT,
        )
        self._notify_diagnostics()

    async def _finish_client_cleanup(
        self,
        disconnect_task: asyncio.Task,
        client: object,
        proxy_address: str,
    ) -> None:
        """Drain a slow disconnect and confirm its remote ESPHome allocation."""
        await self._drain_aborted_connect([disconnect_task])
        await self._wait_for_esphome_slot_release(client, proxy_address)

    @staticmethod
    def _consume_cleanup_task_result(task: asyncio.Task) -> None:
        """Consume completion from one detached GATT cleanup task."""
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()

    async def _establish_bleak_client(
        self,
        ble_device: object,
        proxy_address: str,
        disconnected_callback: Callable[[object], None],
    ) -> object:
        """Establish one HA-managed connection and retain it until release."""
        owned_client: object | None = None

        def client_created(client: object) -> None:
            nonlocal owned_client
            owned_client = client
            self._pending_clients[proxy_address] = client

        def backend_created(backend: object) -> None:
            if FORCE_FRESH_GATT_DISCOVERY:
                self._discard_target_local_gatt_cache(backend, proxy_address)
            elif SHARE_VERIFIED_GATT_CACHE_BETWEEN_CANDIDATES:
                if not self._prime_verified_gatt_cache(backend, proxy_address):
                    self._prime_telink_mesh_proxy_bootstrap_cache(
                        backend,
                        proxy_address,
                    )

        trace = self._current_connect_trace
        connector_slot_retry = bool(
            trace is not None
            and trace.get("address") == proxy_address
            and trace.get("connector_internal_slot_retry")
        )

        def connect_attempt_started() -> None:
            current = self._current_connect_trace
            if current is None or current.get("address") != proxy_address:
                return
            attempts = int(current.get("connector_gatt_attempts", 0)) + 1
            current["connector_gatt_attempts"] = attempts
            if connector_slot_retry:
                key = "connector_internal_gatt_attempts"
                self._connect_attempt_stats[key] = (
                    self._connect_attempt_stats.get(key, 0) + 1
                )

        try:
            cccd_recovery = proxy_address in self._service_cache_bypass
            use_services_cache = not (
                cccd_recovery or FORCE_FRESH_GATT_DISCOVERY
            )
            if cccd_recovery:
                cache_reason = (
                    "gatt_connect_recovery"
                    if self._candidate_failure_reason.get(proxy_address)
                    == "gatt_connect"
                    else "cccd_recovery"
                )
                self._uncached_recovery_attempted.add(proxy_address)
            elif FORCE_FRESH_GATT_DISCOVERY:
                cache_reason = "forced_fresh_discovery"
            else:
                cache_reason = "normal_cache"
            self._set_connect_cache_decision(
                proxy_address,
                use_services_cache=use_services_cache,
                reason=cache_reason,
            )
            if not use_services_cache:
                _LOGGER.info(
                    "Amaran BLE proxy %s is connecting with uncached GATT "
                    "services (%s)",
                    proxy_address,
                    cache_reason,
                )
            return await establish_connection(
                _AmaranBleakClient,
                ble_device,
                f"Amaran BLE {proxy_address}",
                disconnected_callback=disconnected_callback,
                max_attempts=CONNECT_MAX_ATTEMPTS,
                use_services_cache=use_services_cache,
                services=[
                    MESH_PROVISIONING_SERVICE_UUID,
                    MESH_PROXY_SERVICE_UUID,
                ],
                amaran_client_created=client_created,
                amaran_backend_created=backend_created,
                amaran_connect_attempt_started=connect_attempt_started,
                amaran_retry_transient_slot_errors=connector_slot_retry,
                amaran_retry_transient_slot_errors_max_attempts=(
                    SINGLE_CANDIDATE_CONNECTOR_SLOT_RETRY_MAX_ATTEMPTS
                ),
            )
        except BaseException as err:
            if owned_client is not None:
                await self._cleanup_client(owned_client, proxy_address)
            raise
        finally:
            if self._pending_clients.get(proxy_address) is owned_client:
                self._pending_clients.pop(proxy_address, None)

    async def _cleanup_client(self, client: object, proxy_address: str) -> bool:
        """Disconnect an owned client and confirm remote slot disposal when known."""
        disconnect_task = asyncio.create_task(
            client.disconnect(),
            name=f"amaran_ble_client_cleanup_{proxy_address}",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(disconnect_task),
                timeout=CLIENT_CLEANUP_TIMEOUT,
            )
        except TimeoutError:
            if disconnect_task.done():
                self._consume_cleanup_task_result(disconnect_task)
                await self._wait_for_esphome_slot_release(
                    client,
                    proxy_address,
                )
            else:
                cleanup_task = self._create_background_task(
                    self._finish_client_cleanup(
                        disconnect_task,
                        client,
                        proxy_address,
                    ),
                    f"amaran_ble_client_cleanup_drain_{proxy_address}",
                )
                self._track_candidate_cleanup_task(cleanup_task)
        except Exception:
            _LOGGER.debug(
                "Unable to finish Amaran BLE client cleanup for %s",
                proxy_address,
                exc_info=True,
            )
            await self._wait_for_esphome_slot_release(client, proxy_address)
        else:
            await self._wait_for_esphome_slot_release(client, proxy_address)
        return not self._client_is_connected(client)

    async def _disconnect_session(
        self,
        proxy_address: str,
        *,
        fast: bool = False,
    ) -> None:
        """Disconnect one stored proxy session without taking its lock."""
        upgrade_task = self._notification_upgrade_tasks.pop(proxy_address, None)
        if (
            upgrade_task is not None
            and upgrade_task is not asyncio.current_task()
        ):
            upgrade_task.cancel()
            await asyncio.gather(upgrade_task, return_exceptions=True)
        session = self._sessions.pop(proxy_address, None)
        if session is None:
            return
        client = session.client
        if client is None:
            return
        if (
            not fast
            and session.provisioning_notify_started
            and session.provisioning_data_out is not None
        ):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    client.stop_notify(session.provisioning_data_out),
                    timeout=CLIENT_CLEANUP_TIMEOUT,
                )
            session.provisioning_notify_started = False
        if not fast and session.notify_started and session.proxy_data_out is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    client.stop_notify(session.proxy_data_out),
                    timeout=CLIENT_CLEANUP_TIMEOUT,
                )
            session.notify_started = False
        await self._cleanup_client(client, proxy_address)
        deadline = self.hass.loop.time() + DISCONNECT_SETTLE_SECONDS
        while self._client_is_connected(client) and self.hass.loop.time() < deadline:
            await asyncio.sleep(0.1)

    async def _disconnect_inactive_sessions(self, keep: str) -> None:
        """Disconnect proxy sessions that are not the active mesh gateway."""
        for proxy_address in list(self._sessions):
            if proxy_address != keep:
                await self._reset_proxy(proxy_address)

    async def _reset_proxy(self, proxy_address: str, *, fast: bool = False) -> None:
        """Drop one proxy session so the next command reconnects."""
        async with self._session_lock(proxy_address):
            await self._disconnect_session(proxy_address, fast=fast)
        if self._active_proxy_address == proxy_address:
            self._active_proxy_address = None

    async def _with_proxy(
        self,
        proxy_address: str,
        action: Callable[[object, object, int], object],
    ) -> None:
        """Run an action on the active mesh proxy session."""
        _ = proxy_address
        if not self._control_enabled:
            raise RuntimeError("Amaran BLE control switch is off")
        try:
            await asyncio.wait_for(
                self._operation_lock.acquire(),
                timeout=COMMAND_LOCK_TIMEOUT,
            )
        except TimeoutError as err:
            raise RuntimeError(
                "Another Amaran BLE command is still waiting for the mesh proxy"
            ) from err
        try:
            session = await self._ensure_ready_for_command()
            if session is None or session.client is None or session.proxy_data_in is None:
                self._set_mesh_available(False)
                raise RuntimeError(
                    "Amaran BLE mesh proxy is not connected"
                )
            try:
                maybe_awaitable = action(
                    session.client,
                    session.proxy_data_in,
                    session.iv_index,
                )
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
            except Exception as err:  # noqa: BLE001 - reconnect in the background.
                _LOGGER.debug(
                    "Amaran BLE proxy %s command failed; reconnecting in background",
                    session.address,
                    exc_info=True,
                )
                await self._reset_proxy(session.address)
                self._set_mesh_available(False)
                raise RuntimeError(
                    "Amaran BLE mesh proxy disconnected; retry after it reconnects"
                ) from err
        finally:
            self._operation_lock.release()

    async def _send_telink(
        self,
        proxy_address: str,
        dst: int,
        payload: bytes,
        retries: int = 2,
        *,
        command_name: str | None = None,
    ) -> None:
        command_id = (
            self._next_command_trace_id() if command_name is not None else None
        )
        started = self.hass.loop.time()
        network_sequence: int | None = None
        completed_writes = 0
        active_proxy: str | None = None

        async def sender(client: object, proxy_data_in: object, iv_index: int) -> None:
            nonlocal network_sequence, completed_writes, active_proxy
            network_sequence = self._next_sequence()
            active_proxy = self._active_proxy_address
            pdu = mesh.build_access_proxy_pdu(
                self.keys,
                sequence=network_sequence,
                dst=dst,
                params=payload,
                iv_index=iv_index,
            )
            send_count = max(1, retries)
            for attempt in range(send_count):
                await self._write_proxy_data(client, proxy_data_in, pdu, "telink access message")
                completed_writes += 1
                if attempt + 1 < send_count:
                    await asyncio.sleep(0.08)

        try:
            await self._with_proxy(proxy_address, sender)
        except BaseException as err:
            if command_id is not None and command_name is not None:
                self._record_command_trace(
                    command_id=command_id,
                    command_name=command_name,
                    dst=dst,
                    sequence=network_sequence,
                    writes=completed_writes,
                    elapsed=self.hass.loop.time() - started,
                    result=(
                        "cancelled" if isinstance(err, asyncio.CancelledError) else "failed"
                    ),
                    active_proxy=active_proxy,
                    error=err,
                )
            raise
        if command_id is not None and command_name is not None:
            self._record_command_trace(
                command_id=command_id,
                command_name=command_name,
                dst=dst,
                sequence=network_sequence,
                writes=completed_writes,
                elapsed=self.hass.loop.time() - started,
                result="sent",
                active_proxy=active_proxy,
            )

    async def _send_telink_wait_status(
        self,
        proxy_address: str,
        dst: int,
        payload: bytes,
        retries: int = 2,
        timeout: float = COMMAND_STATUS_TIMEOUT,
        predicate: Callable[[telink.TelinkStatus], bool] | None = None,
    ) -> telink.TelinkStatus | None:
        """Send a Telink command and wait for a matching status reply."""
        future: asyncio.Future[telink.TelinkStatus] = self.hass.loop.create_future()

        def callback(status: telink.TelinkStatus) -> None:
            if not future.done() and (predicate is None or predicate(status)):
                future.set_result(status)

        unsubscribe = self.async_track_status(dst, callback)
        try:
            await self._send_telink(proxy_address, dst, payload, retries=retries)
            if not self.status_notifications_supported():
                return None
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return None
        finally:
            unsubscribe()

    async def async_refresh_all(self) -> None:
        """Request status from all configured fixtures."""
        for light in self.config.lights:
            await self.async_refresh(light.mac, light.mesh_address)

    async def async_refresh_all_statuses(
        self,
        timeout: float = COMMAND_STATUS_TIMEOUT,
    ) -> dict[int, telink.TelinkStatus]:
        """Request all fixture statuses and return the reports seen in time."""
        if not self.status_notifications_supported():
            return {}
        futures: dict[int, asyncio.Future[telink.TelinkStatus]] = {}
        unsubscribers: list[Callable[[], None]] = []

        for light in self.config.lights:
            future: asyncio.Future[telink.TelinkStatus] = self.hass.loop.create_future()
            futures[light.mesh_address] = future

            def callback(
                status: telink.TelinkStatus,
                *,
                mesh_address: int = light.mesh_address,
            ) -> None:
                if not futures[mesh_address].done():
                    futures[mesh_address].set_result(status)

            unsubscribers.append(
                self.async_track_status(light.mesh_address, callback)
            )

        try:
            await self.async_refresh_all()
            if futures:
                await asyncio.wait(futures.values(), timeout=timeout)
        finally:
            for unsubscribe in unsubscribers:
                unsubscribe()

        statuses = {
            mesh_address: future.result()
            for mesh_address, future in futures.items()
            if future.done() and not future.cancelled()
        }
        for light in self.config.lights:
            if light.mesh_address not in statuses:
                self._record_status_miss(light.mesh_address)
        return statuses

    async def async_turn_on(self, proxy_address: str, address: int) -> telink.TelinkStatus | None:
        """Turn on a fixture and let notifications confirm asynchronously."""
        await self._send_telink(
            proxy_address,
            address,
            telink.onoff(True),
            command_name="turn_on",
        )
        self._fixture_states[address].apply_power(True)
        self._notify_fixture_state(address)
        return None

    async def async_turn_off(self, proxy_address: str, address: int) -> telink.TelinkStatus | None:
        """Turn off a fixture and let notifications confirm asynchronously."""
        await self._send_telink(
            proxy_address,
            address,
            telink.onoff(False),
            command_name="turn_off",
        )
        self._fixture_states[address].apply_power(False)
        self._notify_fixture_state(address)
        return None

    async def async_set_brightness(
        self,
        proxy_address: str,
        address: int,
        intensity: int,
    ) -> telink.TelinkStatus | None:
        """Set brightness and let notifications confirm asynchronously."""
        await self._send_telink(
            proxy_address,
            address,
            telink.brightness(intensity),
            command_name="set_brightness",
        )
        self._fixture_states[address].apply_intensity(intensity)
        self._notify_fixture_state(address)
        return None

    async def async_set_cct(
        self,
        proxy_address: str,
        address: int,
        kelvin: int,
        intensity: int,
        gm_offset: int = 0,
    ) -> telink.TelinkStatus | None:
        """Set CCT mode and let notifications confirm asynchronously."""
        fixture_state = self._fixture_states[address]
        requested_state = fixture_state.snapshot()
        requested_state.apply_cct(kelvin, intensity, gm_offset)
        await self._send_telink(
            proxy_address,
            address,
            telink.cct(
                requested_state.kelvin,
                requested_state.intensity,
                requested_state.gm,
            ),
            command_name="set_cct",
        )
        fixture_state.apply_cct(
            requested_state.kelvin,
            requested_state.intensity,
            requested_state.gm,
        )
        self._notify_fixture_state(address)
        return None

    async def async_set_hsi(
        self,
        proxy_address: str,
        address: int,
        hue: int,
        saturation: int,
        intensity: int,
    ) -> telink.TelinkStatus | None:
        """Set HSI mode and let notifications confirm asynchronously."""
        fixture_state = self._fixture_states[address]
        requested_state = fixture_state.snapshot()
        requested_state.apply_hsi(hue, saturation, intensity)
        await self._send_telink(
            proxy_address,
            address,
            telink.hsi(
                requested_state.hue,
                requested_state.saturation,
                requested_state.intensity,
            ),
            command_name="set_hsi",
        )
        fixture_state.apply_hsi(
            requested_state.hue,
            requested_state.saturation,
            requested_state.intensity,
        )
        self._notify_fixture_state(address)
        return None

    async def async_refresh(self, proxy_address: str, address: int) -> None:
        """Request a status refresh from a fixture."""
        await self._send_telink(proxy_address, address, telink.status_request())

    async def async_refresh_status(
        self,
        proxy_address: str,
        address: int,
        timeout: float = COMMAND_STATUS_TIMEOUT,
    ) -> telink.TelinkStatus | None:
        """Request one fixture status and wait briefly for its report."""
        future: asyncio.Future[telink.TelinkStatus] = self.hass.loop.create_future()

        def callback(status: telink.TelinkStatus) -> None:
            if not future.done():
                future.set_result(status)

        unsubscribe = self.async_track_status(address, callback)
        try:
            await self.async_refresh(proxy_address, address)
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._record_status_miss(address)
            return None
        finally:
            unsubscribe()
