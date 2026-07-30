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

# Match Amaran Desktop's AutoConnect lifecycle: scan the configured mesh nodes,
# connect one node at a time, complete Proxy login, then retain that connection.
CONNECT_MAX_ATTEMPTS = 1
GATT_CLIENT_CONNECT_TIMEOUT = 45.0
GATT_ESTABLISH_DEADLINE = 40.0
CONNECT_CANDIDATES_PER_ROUND = 1
CANDIDATE_FAILURE_COOLDOWN = 30.0
SINGLE_CANDIDATE_FAILURE_COOLDOWN = 2.0
GATT_FAILURE_COOLDOWN = 120.0
POST_OPEN_FAILURE_COOLDOWN = 180.0
SHORT_CONNECTION_COOLDOWN = 180.0
PROBATION_CONNECTION_COOLDOWN = 300.0
CANDIDATE_FAILURE_COOLDOWN_MULTIPLIER = 1.5
CANDIDATE_FAILURE_COOLDOWN_MAX = 300.0
CANDIDATE_HEALTH_HISTORY_SIZE = 8
CANDIDATE_HEALTH_DECAY = 0.8
CANDIDATE_LAST_STABLE_BONUS = 1.5
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
CONTROL_RELEASE_DRAIN_TIMEOUT = 25.0
SERVICE_CACHE_CLEAR_TIMEOUT = 2.0
DISCOVERY_TIMEOUT = 3
ADVERTISEMENT_MAX_AGE = 120.0
STARTUP_CONNECT_ADVERTISEMENT_MAX_AGE = 8.0
STARTUP_SCAN_GRACE = 30.0
FILTER_STATUS_TIMEOUT = 3.0
PROXY_READY_DELAY = 0.1
NOTIFICATION_SETUP_TIMEOUT = 12.0
GATT_NOTIFICATION_SETTLE_SECONDS = 3.0
GATT_NOTIFICATION_RECOVERY_SETTLE_SECONDS = 5.0
WRITE_ONLY_RECOVERY_SETTLE_SECONDS = 2.0
NOTIFICATION_RETRY_DELAY = 60.0
INITIAL_NOTIFICATION_DELAY = 30.0
AUTO_CONNECT_DELAY = 0.5
RELEASE_RECONNECT_COOLDOWN = 2
SHORT_RELEASE_RECONNECT_COOLDOWN = 2.0
MAINTAIN_RETRY_DELAYS = (1, 2, 5, 10)
OUT_OF_SLOTS_RETRY_DELAY = 4
GATT_ESTABLISH_RETRY_DELAY = 2
RECOVERY_RETRY_DELAY = 12
PREFERRED_PROXY_GATT_RECOVERY_DELAY = 30
PREOPEN_STORM_ROUNDS = 2
# Preserve the 0.3.34 fixed recovery timing, which had the best 30-cycle
# connection latency of the comparable long-running tests.
PREOPEN_STORM_RECOVERY_DELAY = 30
NO_FRESH_ADVERTISEMENT_RETRY_DELAY = 2
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
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if amaran_client_created is not None:
            amaran_client_created(self)

    async def connect(self, *args: object, **kwargs: object) -> object:
        """Run one complete GATT attempt without connector-internal retries."""
        kwargs["timeout"] = GATT_CLIENT_CONNECT_TIMEOUT
        try:
            return await super().connect(*args, **kwargs)
        except BleakError as err:
            # bleak-retry-connector retries ESP status 133 up to nine times even
            # when max_attempts=1.  That keeps HA on one fixture for minutes.
            # Convert the completed backend attempt to a non-Bleak exception so
            # the integration can rotate mesh proxy candidates immediately.
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
        self._round_failed_candidates: set[str] = set()
        self._consecutive_preopen_failures = 0
        self._notification_backoff_until: dict[str, float] = {}
        self._service_cache_bypass: set[str] = set()
        self._cccd_reconnect_pending: set[str] = set()
        self._cached_cccd_retry_attempted: set[str] = set()
        self._uncached_recovery_attempted: set[str] = set()
        self._current_connect_trace: dict[str, object] | None = None
        self._connect_attempt_stats: dict[str, int] = {}
        self._candidate_health_history: dict[str, deque[str]] = {}
        self._connect_attempt_sequence = 0
        self._ownership_cycle = 1
        self._candidate_round = 1
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
        self._connect_attempt_history: deque[dict[str, object]] = deque(maxlen=12)
        self._stopping = False

    async def async_start(self, entry: object | None = None) -> None:
        """Start a delayed single-proxy connector."""
        self._stopping = False
        self._entry = entry
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
                bluetooth.BluetoothScanningMode.ACTIVE,
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
        self.hass.loop.call_soon_threadsafe(self._wake_for_advertisement, address)

    def _wake_for_advertisement(self, address: str) -> None:
        """Trigger a quick reconnect after a fresh proxy advertisement."""
        if self._stopping or not self._control_enabled:
            return
        if address not in self._proxy_addresses():
            return
        now = self.hass.loop.time()
        self._next_connect_after = max(
            self._release_guard_until,
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
                await self._reset_proxy(proxy_address)
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
        self._round_failed_candidates.clear()
        self._consecutive_preopen_failures = 0

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

    async def _cancel_candidate_cleanup_tasks(self) -> None:
        """Stop and drain background cleanup from a completed candidate race."""
        tasks = list(self._candidate_cleanup_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._candidate_cleanup_tasks.clear()

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
        return bool(self._pending_clients) or any(
            not task.done() for task in self._candidate_cleanup_tasks
        )

    async def async_stop(self) -> None:
        """Stop work and release all BLE connections."""
        self._stopping = True
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

        await self._cancel_candidate_cleanup_tasks()

        for task in self._notification_upgrade_tasks.values():
            task.cancel()
        if self._notification_upgrade_tasks:
            await asyncio.gather(
                *self._notification_upgrade_tasks.values(),
                return_exceptions=True,
            )
        self._notification_upgrade_tasks.clear()

        for proxy_address in list(self._sessions):
            await self._reset_proxy(proxy_address)

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
                "Amaran BLE proxy %s uncached CCCD recovery failed; "
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
        if len(self._proxy_addresses()) > 1:
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
                if len(self._proxy_addresses()) > 1
                else SINGLE_CANDIDATE_FAILURE_COOLDOWN,
            )
        if len(self._proxy_addresses()) <= 1:
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
        streak = self._candidate_failure_streak.get(address, 0) + 1
        self._candidate_failure_streak[address] = streak
        self._candidate_failure_reason[address] = reason
        base = self._candidate_cooldown_seconds(err)
        if reason == "probation_disconnect":
            base = max(base, PROBATION_CONNECTION_COOLDOWN)
        if len(self._proxy_addresses()) > 1:
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
        self._candidate_round += 1
        self._connect_failure_count = 0
        self._round_failed_candidates.clear()
        self._ignored_stored_proxy = None
        addresses = self._proxy_addresses()
        preferred = self._last_successful_proxy()
        if preferred in addresses:
            self._candidate_cursor = addresses.index(preferred)
            key = "preferred_proxy_round_restarts"
            self._connect_attempt_stats[key] = self._connect_attempt_stats.get(key, 0) + 1
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
        self._notify_diagnostics()

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
        ordered = self._rotated_proxy_addresses(addresses)
        ordered = self._filter_candidate_cooldowns(ordered)
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
                bluetooth.BluetoothScanningMode.ACTIVE,
                DISCOVERY_TIMEOUT,
            )
        except TimeoutError:
            return None
        except Exception:
            _LOGGER.debug(
                "Unable to process active advertisements for Amaran BLE proxy candidates",
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
        # This is the same whitelist emitted by Amaran Desktop's PyMeshSDK:
        # the controller's local unicast address and All Nodes.
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
            return
        self._mesh_available = available
        for callback in list(self._mesh_callbacks):
            callback(available)
        self._notify_diagnostics()
        if not available:
            for light in self.config.lights:
                self._set_light_available(light.mac, False)
        else:
            for light in self.config.lights:
                self._set_light_available(light.mac, True)
            self._expire_stale_lights()

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
            "round_failed_candidates": sorted(self._round_failed_candidates),
            "candidate_cursor": self._candidate_cursor,
            "connect_failure_count": self._connect_failure_count,
            "ownership_cycle": self._ownership_cycle,
            "candidate_round": self._candidate_round,
            "connect_attempt_sequence": self._connect_attempt_sequence,
            "last_error": self._last_maintain_error,
            "last_error_hint": self._last_error_hint(),
            "connection_advice": self._connection_advice(),
            "status_notifications_supported": self.status_notifications_supported(),
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
            "cache_mode_semantics": "requested_use_services_cache",
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
            return "gatt_connect_cancelled"
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
        if hint == "gatt_connect_cancelled":
            return (
                "The BLE backend cancelled this GATT establishment attempt; this does "
                "not by itself mean the ESPHome proxy ran out of connection slots."
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
            for light in self.config.lights:
                self._set_light_available(light.mac, True)
            return
        now = self.hass.loop.time()
        for light in self.config.lights:
            seen_at = self._last_status_seen.get(light.mesh_address)
            if seen_at is None:
                continue
            if now - seen_at > LIGHT_STATUS_UNAVAILABLE_SECONDS:
                self._set_light_available(light.mac, False)

    def _record_status_miss(self, mesh_address: int) -> None:
        """Record one missed status response and expire after repeated misses."""
        count = self._status_miss_count.get(mesh_address, 0) + 1
        self._status_miss_count[mesh_address] = count
        seen_at = self._last_status_seen.get(mesh_address)
        if seen_at is None:
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
            return
        seen_addresses: set[int] = set()
        expected_addresses = {light.mesh_address for light in self.config.lights}
        for attempt in range(INITIAL_STATUS_REFRESH_ATTEMPTS):
            statuses = await self.async_refresh_all_statuses(
                timeout=INITIAL_STATUS_WAIT,
            )
            seen_addresses.update(statuses)
            if expected_addresses and expected_addresses.issubset(seen_addresses):
                break
            if attempt + 1 < INITIAL_STATUS_REFRESH_ATTEMPTS:
                await asyncio.sleep(INITIAL_STATUS_REFRESH_DELAY)

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
        """Enable the required Proxy channel before optional Provisioning data."""
        if (
            session.client is None
            or session.proxy_data_out is None
            or session.notification_callback is None
        ):
            raise RuntimeError("Mesh Proxy notification channel is incomplete")
        # The captured Sidus/Amaran sequence writes the Proxy Data Out CCCD
        # (handle 0x30) before the Provisioning Data Out CCCD (handle 0x36).
        # Proxy notifications are mandatory for filter status and light state.
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

        if (
            session.provisioning_data_out is not None
            and not session.provisioning_notify_started
        ):
            try:
                await asyncio.wait_for(
                    session.client.start_notify(
                        session.provisioning_data_out,
                        self._ignore_provisioning_notification,
                    ),
                    timeout=NOTIFICATION_SETUP_TIMEOUT,
                )
                session.provisioning_notify_started = True
            except Exception:
                # Provisioned control does not consume PB-Out. Keep the already
                # established Proxy-Out channel when this optional CCCD fails.
                _LOGGER.debug(
                    "Amaran BLE proxy %s could not enable optional Provisioning "
                    "Data Out notifications",
                    session.address,
                    exc_info=True,
                )
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
                # A descriptor/GATT error means the link is already unusable.
                # Keep same-candidate recovery for a silent CCCD timeout only.
                self._service_cache_bypass.discard(session.address)
                self._cccd_reconnect_pending.discard(session.address)
                self._cached_cccd_retry_attempted.discard(session.address)
                self._uncached_recovery_attempted.discard(session.address)
                self._notify_diagnostics()
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
        """Send Amaran Desktop's beacon and Mesh Proxy whitelist sequence."""
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

        if not wait_for_filter_status:
            return False
        if filter_status_event is None:
            return False
        try:
            await asyncio.wait_for(
                filter_status_event.wait(),
                timeout=FILTER_STATUS_TIMEOUT,
            )
        except TimeoutError:
            return False
        return True

    def _maintain_retry_delay(self, detail: str = "") -> int:
        """Return a bounded backoff delay after repeated BLE connect failures."""
        if self._full_candidate_round_failed():
            if self._preopen_failure_storm_active():
                _LOGGER.info(
                    "Amaran BLE pre-open failure storm detected after %s "
                    "consecutive attempts; pausing AutoConnect for %ss",
                    self._consecutive_preopen_failures,
                    PREOPEN_STORM_RECOVERY_DELAY,
                )
                return PREOPEN_STORM_RECOVERY_DELAY
            if self._preferred_proxy_preopen_cooldown() is not None:
                return PREFERRED_PROXY_GATT_RECOVERY_DELAY
            return RECOVERY_RETRY_DELAY
        detail_lower = detail.lower()
        if (
            "out of connection slots" in detail_lower
            or "allocation is still assigned" in detail_lower
        ):
            return OUT_OF_SLOTS_RETRY_DELAY
        if (
            "disconnect timed out: true" in detail_lower
        ):
            return GATT_ESTABLISH_RETRY_DELAY
        if (
            "esp_gatt_conn_conn_cancel" in detail_lower
            or "esp_gatt_conn_fail_establish" in detail_lower
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
        candidate_count = len(self._proxy_addresses())
        return bool(candidate_count) and self._consecutive_preopen_failures >= (
            candidate_count * PREOPEN_STORM_ROUNDS
        )

    def _full_candidate_round_failed(self) -> bool:
        """Return whether every configured proxy has failed in this round."""
        addresses = set(self._proxy_addresses())
        return bool(addresses) and addresses.issubset(self._round_failed_candidates)

    def _get_ble_device(self, proxy_address: str):
        return bluetooth.async_ble_device_from_address(
            self.hass,
            proxy_address,
            connectable=True,
        )

    async def _async_get_ble_device(
        self,
        proxy_address: str,
        *,
        force_scan: bool = False,
        allow_scan: bool = False,
    ):
        """Return a connectable BLE device, doing a short on-demand scan if needed."""
        stale_ble_device = None
        if not force_scan and (ble_device := self._get_ble_device(proxy_address)) is not None:
            if self._has_fresh_ble_device(proxy_address):
                return ble_device
            stale_ble_device = ble_device
            _LOGGER.debug(
                "Amaran BLE proxy %s has only stale Bluetooth data; waiting for a fresh advertisement",
                proxy_address,
            )
        if not allow_scan:
            return None

        _LOGGER.debug(
            "Waiting up to %ss for Amaran BLE proxy advertisement from %s",
            DISCOVERY_TIMEOUT,
            proxy_address,
        )
        deadline = self.hass.loop.time() + DISCOVERY_TIMEOUT
        while self.hass.loop.time() < deadline:
            timeout = max(0.1, min(1.0, deadline - self.hass.loop.time()))
            try:
                service_info = await bluetooth.async_process_advertisements(
                    self.hass,
                    lambda service_info: (
                        str(service_info.address).upper() == proxy_address
                        and self._service_info_is_connectable(service_info)
                        and self._service_info_is_fresh(service_info)
                    ),
                    {"address": proxy_address},
                    bluetooth.BluetoothScanningMode.ACTIVE,
                    timeout,
                )
            except TimeoutError:
                if self._has_fresh_ble_device(proxy_address):
                    return self._get_ble_device(proxy_address)
                continue
            except Exception:
                _LOGGER.debug(
                    "Unable to process active advertisements for Amaran BLE proxy %s",
                    proxy_address,
                    exc_info=True,
                )
                if self._has_fresh_ble_device(proxy_address):
                    return self._get_ble_device(proxy_address)
                return None

            if not self._service_info_is_fresh(service_info):
                age = self._service_info_age(service_info)
                _LOGGER.debug(
                    "Active scan returned stale Amaran BLE proxy advertisement for %s: age %s",
                    proxy_address,
                    f"{age:.1f}s" if age is not None else "unknown",
                )
                await asyncio.sleep(0.1)
                continue

            ble_device = getattr(service_info, "device", None) or self._get_ble_device(proxy_address)
            if ble_device is not None:
                return ble_device
            if self._has_fresh_ble_device(proxy_address):
                return self._get_ble_device(proxy_address)
            await asyncio.sleep(0.1)

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

        candidate_addresses = self._proxy_addresses()
        if not candidate_addresses:
            raise RuntimeError("No Amaran BLE proxy candidates are configured")

        ordered_addresses = await self._connect_candidate_order(candidate_addresses)
        if not ordered_addresses:
            raise RuntimeError(
                "Unable to connect to any Amaran BLE mesh proxy: no candidates"
            )

        # PyMeshSDK stops its private scanner as soon as one whitelisted node is
        # selected. HA's scanner is shared, so leave it alone and reuse its fresh
        # connectable BLEDevice. A short active scan remains the fallback when
        # HA does not currently have fresh data for the selected candidate.
        proxy_address = ordered_addresses[0]
        started = self.hass.loop.time()
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
            "stages": {"candidate_selected": 0.0},
        }
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
        try:
            session = await self._ensure_proxy(
                proxy_address,
                force_scan=False,
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
            "result=%s stage=%s cache=%s elapsed=%.3fs",
            item.get("ownership_cycle"),
            item.get("candidate_round"),
            item.get("attempt_id"),
            address,
            result,
            item.get("result_stage"),
            item.get("requested_cache_mode"),
            elapsed,
        )
        self._notify_diagnostics()
        return str(item["result_stage"])

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
        self._mark_connect_stage(address, "advertisement_ready")
        _LOGGER.debug(
            "Amaran BLE ownership=%s round=%s attempt=%s candidate=%s "
            "advertisement age=%s rssi=%s source=%s connectable=%s",
            trace.get("ownership_cycle"),
            trace.get("candidate_round"),
            trace.get("attempt_id"),
            address,
            snapshot.get("age_seconds"),
            snapshot.get("rssi"),
            snapshot.get("source"),
            snapshot.get("connectable"),
        )

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
        force_scan: bool = False,
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
                await self._connect_session(session, force_scan=force_scan)
            except BaseException:
                await self._disconnect_session(proxy_address)
                raise
            return session

    async def _connect_session(
        self,
        session: _ProxySession,
        *,
        force_scan: bool = False,
    ) -> None:
        """Connect and prepare one BLE Mesh Proxy session."""
        proxy_address = session.address
        if not self._control_enabled:
            raise RuntimeError("Amaran BLE control switch is off")

        ble_device = await self._async_get_ble_device(
            proxy_address,
            force_scan=force_scan,
            allow_scan=True,
        )
        if ble_device is None:
            raise RuntimeError(f"Amaran BLE device not found: {proxy_address}")
        self._mark_connect_advertisement_ready(proxy_address)

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

        # PyMeshSDK's ProxyInit enables PB-Out and Proxy-Out notifications before
        # it sends the authenticated network beacon and proxy filter sequence.
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
        self._mark_connect_stage(proxy_address, "mesh_login_ready")
        _LOGGER.debug(
            "Amaran BLE proxy %s completed official-style Proxy login",
            proxy_address,
        )

    def _notification_settle_seconds(self, proxy_address: str) -> float:
        """Use extra settle time only after a silent CCCD timeout."""
        if proxy_address in self._cached_cccd_retry_attempted:
            return GATT_NOTIFICATION_RECOVERY_SETTLE_SECONDS
        return GATT_NOTIFICATION_SETTLE_SECONDS

    @staticmethod
    async def _drain_aborted_connect(tasks: list[asyncio.Task]) -> None:
        """Consume completion from an aborted connect and its disconnect request."""
        await asyncio.gather(*tasks, return_exceptions=True)

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

        try:
            cccd_recovery = proxy_address in self._service_cache_bypass
            use_services_cache = not cccd_recovery
            if cccd_recovery:
                cache_reason = "cccd_recovery"
                self._uncached_recovery_attempted.add(proxy_address)
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
            establish_task = asyncio.create_task(
                establish_connection(
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
                ),
                name=f"amaran_ble_establish_{proxy_address}",
            )
            try:
                return await asyncio.wait_for(
                    establish_task,
                    timeout=GATT_ESTABLISH_DEADLINE,
                )
            except TimeoutError as err:
                if establish_task.done() and not establish_task.cancelled():
                    raise
                raise TimeoutError(
                    "Amaran BLE GATT establish/service discovery exceeded "
                    f"{GATT_ESTABLISH_DEADLINE:.0f}s for {proxy_address}"
                ) from err
        except BaseException as err:
            if owned_client is not None:
                await self._cleanup_client(owned_client, proxy_address)
            raise
        finally:
            if self._pending_clients.get(proxy_address) is owned_client:
                self._pending_clients.pop(proxy_address, None)

    async def _cleanup_client(self, client: object, proxy_address: str) -> bool:
        """Disconnect an owned client without inspecting HA or ESPHome internals."""
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
            cleanup_task = self._create_background_task(
                self._drain_aborted_connect([disconnect_task]),
                f"amaran_ble_client_cleanup_drain_{proxy_address}",
            )
            self._track_candidate_cleanup_task(cleanup_task)
        except Exception:
            _LOGGER.debug(
                "Unable to finish Amaran BLE client cleanup for %s",
                proxy_address,
                exc_info=True,
            )
        return not self._client_is_connected(client)

    async def _disconnect_session(self, proxy_address: str) -> None:
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
            session.provisioning_notify_started
            and session.provisioning_data_out is not None
        ):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    client.stop_notify(session.provisioning_data_out),
                    timeout=CLIENT_CLEANUP_TIMEOUT,
                )
            session.provisioning_notify_started = False
        if session.notify_started and session.proxy_data_out is not None:
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

    async def _reset_proxy(self, proxy_address: str) -> None:
        """Drop one proxy session so the next command reconnects."""
        async with self._session_lock(proxy_address):
            await self._disconnect_session(proxy_address)
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
