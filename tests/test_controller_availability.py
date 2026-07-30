"""Offline checks for controller availability behavior."""

from __future__ import annotations

import importlib
import asyncio
from types import MappingProxyType
import sys
import types
import unittest


def _install_homeassistant_stubs() -> None:
    """Install tiny Home Assistant module stubs for controller-only tests."""
    if "homeassistant" in sys.modules:
        return

    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    core = types.ModuleType("homeassistant.core")

    def _missing(*_: object, **__: object) -> None:
        return None

    bluetooth.async_ble_device_from_address = _missing
    bluetooth.async_last_service_info = _missing
    bluetooth.BluetoothScanningMode = types.SimpleNamespace(ACTIVE="active")
    core.HomeAssistant = object
    components.bluetooth = bluetooth

    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.bluetooth"] = bluetooth
    sys.modules["homeassistant.core"] = core


def _install_bleak_retry_connector_stub() -> None:
    """Install the small connector surface used by the controller tests."""
    if "bleak_retry_connector" in sys.modules:
        return

    connector = types.ModuleType("bleak_retry_connector")

    class FakeBleakClientWithServiceCache:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.is_connected = False

        async def disconnect(self) -> None:
            self.is_connected = False

        async def connect(self, **kwargs: object) -> object:
            self.connect_kwargs = kwargs
            self.is_connected = True
            return True

    async def fake_establish_connection(
        client_class: type,
        device: object,
        _name: str,
        **kwargs: object,
    ) -> object:
        created = kwargs.pop("amaran_client_created", None)
        client = client_class(
            device,
            amaran_client_created=created,
            **kwargs,
        )
        client.is_connected = True
        return client

    connector.BleakClientWithServiceCache = FakeBleakClientWithServiceCache
    connector.establish_connection = fake_establish_connection
    sys.modules["bleak_retry_connector"] = connector


def _install_bleak_stub() -> None:
    """Install the Bleak exception imported by the controller."""
    if "bleak.exc" in sys.modules:
        return

    bleak = types.ModuleType("bleak")
    bleak_exc = types.ModuleType("bleak.exc")

    class FakeBleakError(Exception):
        pass

    bleak_exc.BleakError = FakeBleakError
    bleak.exc = bleak_exc
    sys.modules["bleak"] = bleak
    sys.modules["bleak.exc"] = bleak_exc


def _install_mesh_stub() -> None:
    """Install a tiny mesh helper stub so the controller can be imported offline."""
    mesh = types.ModuleType("custom_components.amaran_ble.mesh")
    mesh.PROXY_TYPE_PROXY_CONFIG = 0x02
    mesh.derive_keys = lambda *_: object()
    mesh.parse_iv_index_from_proxy = lambda *_: None
    mesh.parse_access_proxy_pdu = lambda *_args, **_kwargs: None
    mesh.build_set_filter_type_pdu = lambda *_args, **_kwargs: b""
    mesh.build_add_addresses_pdu = lambda *_args, **_kwargs: b""
    mesh.build_secure_network_beacon = lambda *_args, **_kwargs: b""
    mesh.build_access_proxy_pdu = lambda *_args, **_kwargs: b""
    sys.modules["custom_components.amaran_ble.mesh"] = mesh


class _FakeLoop:
    def __init__(self) -> None:
        self._time = 100.0

    def time(self) -> float:
        return self._time

    def create_future(self) -> asyncio.Future:
        return asyncio.get_running_loop().create_future()


class _FakeHass:
    def __init__(self) -> None:
        self.loop = _FakeLoop()
        self.config_entries = types.SimpleNamespace(async_update_entry=lambda *_, **__: None)


class ControllerAvailabilityTest(unittest.TestCase):
    def _make_controller(self):
        _install_homeassistant_stubs()
        _install_bleak_stub()
        _install_bleak_retry_connector_stub()
        _install_mesh_stub()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        models_module = importlib.import_module("custom_components.amaran_ble.models")

        light = models_module.AmaranLightConfig(
            name="amaran T2c #1",
            mac="AA:BB:CC:11:22:33",
            mesh_address=5,
        )
        config = models_module.AmaranConfig(
            net_key="00112233445566778899AABBCCDDEEFF",
            app_key="FFEEDDCCBBAA99887766554433221100",
            relay_hub=light.mac,
            lights=[light],
            proxy_candidates=[],
        )
        return controller_module.AmaranBleController(_FakeHass(), config), light

    def test_mesh_available_marks_configured_lights_available(self) -> None:
        controller, light = self._make_controller()

        updates: list[bool] = []
        controller.async_track_availability(light.mac, updates.append)
        controller._set_mesh_available(True)

        self.assertTrue(controller.is_light_available(light.mac))
        self.assertEqual(updates, [True])

    def test_filter_addresses_match_official_sdk_whitelist(self) -> None:
        controller, _light = self._make_controller()

        self.assertEqual(controller._filter_addresses(), [0x0001, 0xFFFF])

    def test_status_marks_light_available(self) -> None:
        controller, light = self._make_controller()
        telink_module = importlib.import_module("custom_components.amaran_ble.telink")
        status = telink_module.TelinkStatus(
            on=True,
            is_hs=False,
            intensity=500,
            cct_kelvin=4500,
            gm=0,
            hue=0,
            saturation=0,
            command_type=2,
        )

        updates: list[bool] = []
        state_updates: list[object] = []
        controller.async_track_availability(light.mac, updates.append)
        controller.async_track_fixture_state(
            light.mesh_address,
            state_updates.append,
        )
        controller._set_mesh_available(True)
        controller._handle_status(light.mesh_address, status)

        self.assertTrue(controller.is_light_available(light.mac))
        self.assertEqual(controller.last_status(light.mesh_address), status)
        self.assertEqual(updates, [True])
        self.assertEqual(len(state_updates), 1)
        self.assertTrue(state_updates[0].on)
        self.assertEqual(state_updates[0].kelvin, 4500)
        self.assertEqual(state_updates[0].intensity, 500)

    def test_command_wait_ignores_stale_non_matching_status(self) -> None:
        controller, light = self._make_controller()
        telink_module = importlib.import_module("custom_components.amaran_ble.telink")
        stale = telink_module.TelinkStatus(
            on=True,
            is_hs=False,
            intensity=500,
            cct_kelvin=4500,
            gm=0,
            hue=0,
            saturation=0,
            command_type=2,
        )
        expected = telink_module.TelinkStatus(
            on=False,
            is_hs=False,
            intensity=500,
            cct_kelvin=4500,
            gm=0,
            hue=0,
            saturation=0,
            command_type=2,
        )

        async def fake_send(*_args: object, **_kwargs: object) -> None:
            controller._handle_status(light.mesh_address, stale)
            await asyncio.sleep(0)
            controller._handle_status(light.mesh_address, expected)

        controller._send_telink = fake_send  # type: ignore[method-assign]
        result = asyncio.run(
            controller._send_telink_wait_status(
                light.mac,
                light.mesh_address,
                b"payload",
                predicate=lambda status: not status.on,
            )
        )

        self.assertIs(result, expected)

    def test_public_light_commands_return_after_mesh_write(self) -> None:
        controller, light = self._make_controller()
        sent: list[tuple[str, int, bytes, str]] = []

        async def fake_send(
            proxy_address: str,
            address: int,
            payload: bytes,
            retries: int = 2,
            *,
            command_name: str | None = None,
        ) -> None:
            self.assertEqual(retries, 2)
            self.assertIsNotNone(command_name)
            sent.append((proxy_address, address, payload, str(command_name)))

        async def fail_wait(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("public light commands must not wait for status")

        controller._send_telink = fake_send  # type: ignore[method-assign]
        controller._send_telink_wait_status = fail_wait  # type: ignore[method-assign]

        async def run_commands() -> list[object]:
            return [
                await controller.async_turn_on(light.mac, light.mesh_address),
                await controller.async_turn_off(light.mac, light.mesh_address),
                await controller.async_set_brightness(
                    light.mac,
                    light.mesh_address,
                    500,
                ),
                await controller.async_set_cct(
                    light.mac,
                    light.mesh_address,
                    4500,
                    500,
                ),
                await controller.async_set_hsi(
                    light.mac,
                    light.mesh_address,
                    120,
                    100,
                    500,
                ),
            ]

        results = asyncio.run(run_commands())

        self.assertEqual(results, [None, None, None, None, None])
        self.assertEqual(len(sent), 5)
        self.assertEqual(
            [item[3] for item in sent],
            ["turn_on", "turn_off", "set_brightness", "set_cct", "set_hsi"],
        )
        state = controller.fixture_state(light.mesh_address)
        self.assertFalse(state.on)
        self.assertTrue(state.is_hs)
        self.assertEqual(state.hue, 120)
        self.assertEqual(state.saturation, 100)
        self.assertEqual(state.intensity, 500)

    def test_failed_color_command_does_not_change_shared_state(self) -> None:
        controller, light = self._make_controller()
        initial = controller.fixture_state(light.mesh_address)

        async def fail_send(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated GATT write failure")

        controller._send_telink = fail_send  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "GATT write failure"):
            asyncio.run(
                controller.async_set_cct(
                    light.mac,
                    light.mesh_address,
                    5600,
                    500,
                    5,
                )
            )

        self.assertEqual(
            controller.fixture_state(light.mesh_address),
            initial,
        )

    def test_cct_command_normalizes_hardware_resolution(self) -> None:
        controller, light = self._make_controller()
        telink_module = importlib.import_module(
            "custom_components.amaran_ble.telink"
        )
        payloads: list[bytes] = []

        async def fake_send(
            _proxy_address: str,
            _address: int,
            payload: bytes,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            payloads.append(payload)

        controller._send_telink = fake_send  # type: ignore[method-assign]

        asyncio.run(
            controller.async_set_cct(
                light.mac,
                light.mesh_address,
                4550,
                1200,
                99,
            )
        )

        self.assertEqual(payloads, [telink_module.cct(4600, 1000, 10)])
        state = controller.fixture_state(light.mesh_address)
        self.assertEqual(state.kelvin, 4600)
        self.assertEqual(state.intensity, 1000)
        self.assertEqual(state.gm, 10)

    def test_with_proxy_waits_for_session_before_sending(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        session = controller_module._ProxySession(
            address=light.mac,
            client=object(),
            proxy_data_in=object(),
        )
        sent: list[object] = []

        async def fake_ensure_ready():
            return session

        async def action(client: object, proxy_data_in: object, iv_index: int) -> None:
            sent.extend([client, proxy_data_in, iv_index])

        controller._ensure_ready_for_command = fake_ensure_ready  # type: ignore[method-assign]

        asyncio.run(controller._with_proxy(light.mac, action))

        self.assertEqual(sent, [session.client, session.proxy_data_in, 0])

    def test_telink_retries_repeat_one_official_network_pdu(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        build_calls: list[dict[str, object]] = []
        writes: list[bytes] = []

        def fake_build(*_args: object, **kwargs: object) -> bytes:
            build_calls.append(kwargs)
            return b"official-pdu"

        async def fake_with_proxy(
            _proxy_address: str,
            action: object,
        ) -> None:
            await action(object(), object(), 0)

        async def fake_write(
            _client: object,
            _characteristic: object,
            data: bytes,
            _label: str,
        ) -> None:
            writes.append(data)

        original_build = controller_module.mesh.build_access_proxy_pdu
        controller_module.mesh.build_access_proxy_pdu = fake_build
        controller._with_proxy = fake_with_proxy  # type: ignore[method-assign]
        controller._write_proxy_data = fake_write  # type: ignore[method-assign]
        try:
            asyncio.run(
                controller._send_telink(
                    light.mac,
                    light.mesh_address,
                    b"payload",
                )
            )
        finally:
            controller_module.mesh.build_access_proxy_pdu = original_build

        self.assertEqual(len(build_calls), 1)
        self.assertEqual(writes, [b"official-pdu", b"official-pdu"])

    def test_user_command_trace_records_sequence_without_payload(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        build_calls: list[dict[str, object]] = []

        def fake_build(*_args: object, **kwargs: object) -> bytes:
            build_calls.append(kwargs)
            return b"secret-test-pdu"

        async def fake_with_proxy(
            _proxy_address: str,
            action: object,
        ) -> None:
            controller._active_proxy_address = "AA:BB:CC:44:55:66"
            await action(object(), object(), 0)

        async def fake_write(*_args: object, **_kwargs: object) -> None:
            return None

        original_build = controller_module.mesh.build_access_proxy_pdu
        controller_module.mesh.build_access_proxy_pdu = fake_build
        controller._with_proxy = fake_with_proxy  # type: ignore[method-assign]
        controller._write_proxy_data = fake_write  # type: ignore[method-assign]
        try:
            asyncio.run(
                controller._send_telink(
                    light.mac,
                    light.mesh_address,
                    b"private-command-payload",
                    command_name="turn_off",
                )
            )
        finally:
            controller_module.mesh.build_access_proxy_pdu = original_build

        trace = controller.diagnostic_state()["recent_command_traces"][-1]
        self.assertEqual(trace["command_id"], 1)
        self.assertEqual(trace["command"], "turn_off")
        self.assertEqual(trace["mesh_address"], "0x0005")
        self.assertEqual(trace["sequence"], build_calls[0]["sequence"])
        self.assertEqual(trace["writes"], 2)
        self.assertEqual(trace["result"], "sent")
        self.assertEqual(trace["active_proxy"], "AA:BB:CC:44:55:66")
        self.assertNotIn("payload", trace)
        self.assertNotIn("private-command-payload", repr(trace))

    def test_quick_preferred_candidate_avoids_second_connect(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        winning_session = controller_module._ProxySession(address=light.mac)

        async def run_test() -> tuple[object, float, bool]:
            other_started = asyncio.Event()

            async def fake_order(_addresses: list[str]) -> list[str]:
                return [light.mac, other]

            async def fake_ensure(
                address: str,
                **_kwargs: object,
            ):
                if address == light.mac:
                    await asyncio.sleep(0.01)
                    return winning_session
                other_started.set()
                await asyncio.sleep(1)

            async def fake_disconnect_inactive(keep: str) -> None:
                self.assertEqual(keep, light.mac)

            controller._connect_candidate_order = fake_order  # type: ignore[method-assign]
            controller._ensure_proxy = fake_ensure  # type: ignore[method-assign]
            controller._disconnect_inactive_sessions = (  # type: ignore[method-assign]
                fake_disconnect_inactive
            )
            started = asyncio.get_running_loop().time()
            result = await controller._connect_active_proxy_locked()
            elapsed = asyncio.get_running_loop().time() - started
            return result, elapsed, other_started.is_set()

        result, elapsed, other_started = asyncio.run(run_test())

        self.assertIs(result, winning_session)
        self.assertLess(elapsed, 0.2)
        self.assertFalse(other_started)

    def test_autoconnect_attempts_only_one_candidate_at_a_time(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        attempted: list[str] = []

        async def fake_order(_addresses: list[str]) -> list[str]:
            return [light.mac, other]

        async def fake_ensure(
            address: str,
            *,
            force_scan: bool = False,
        ):
            self.assertFalse(force_scan)
            attempted.append(address)
            raise TimeoutError("Timeout waiting for connect response")

        controller._connect_candidate_order = fake_order  # type: ignore[method-assign]
        controller._ensure_proxy = fake_ensure  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, light.mac):
            asyncio.run(controller._connect_active_proxy_locked())

        self.assertEqual(attempted, [light.mac])
        self.assertNotIn(other, attempted)
        self.assertEqual(controller._candidate_cursor, 1)
        self.assertIn(light.mac, controller._candidate_cooldown_until)

    def test_control_toggle_preserves_candidate_failure_cooldown(self) -> None:
        controller, light = self._make_controller()
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]
        controller._ignored_stored_proxy = light.mac
        controller._candidate_cooldown_until[light.mac] = 999
        controller._notification_backoff_until[light.mac] = 999
        controller._service_cache_bypass.add(light.mac)
        controller._cccd_reconnect_pending.add(light.mac)
        controller._cached_cccd_retry_attempted.add(light.mac)
        controller._connect_failure_count = 4

        asyncio.run(controller.async_set_control_enabled(False))
        self.assertIsNone(controller._ignored_stored_proxy)
        self.assertEqual(controller._service_cache_bypass, set())
        self.assertEqual(controller._cccd_reconnect_pending, set())
        self.assertEqual(controller._cached_cccd_retry_attempted, set())

        controller._ignored_stored_proxy = light.mac
        controller._service_cache_bypass.add(light.mac)
        controller._cccd_reconnect_pending.add(light.mac)
        controller._cached_cccd_retry_attempted.add(light.mac)
        asyncio.run(controller.async_set_control_enabled(True))
        self.assertIsNone(controller._ignored_stored_proxy)
        self.assertEqual(controller._candidate_cooldown_until, {light.mac: 999})
        self.assertEqual(controller._notification_backoff_until, {})
        self.assertEqual(controller._service_cache_bypass, set())
        self.assertEqual(controller._cccd_reconnect_pending, set())
        self.assertEqual(controller._cached_cccd_retry_attempted, set())
        self.assertEqual(controller._connect_failure_count, 0)

    def test_short_release_guard_survives_fast_reenable_and_advertisement(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]
        controller._active_proxy_address = light.mac
        controller._sessions[light.mac] = controller_module._ProxySession(
            address=light.mac,
            client=types.SimpleNamespace(is_connected=True),
            proxy_data_in=object(),
            mesh_ready=True,
            connected_at=controller.hass.loop.time() - 5,
        )

        async def fake_reset(address: str) -> None:
            controller._sessions.pop(address, None)
            controller._active_proxy_address = None

        controller._reset_proxy = fake_reset  # type: ignore[method-assign]

        asyncio.run(controller.async_set_control_enabled(False))

        self.assertEqual(controller._release_guard_until, 102.0)
        self.assertEqual(controller._last_release_uptime, 5.0)
        self.assertTrue(controller._last_release_was_short)

        controller.hass.loop._time += 4
        asyncio.run(controller.async_set_control_enabled(True))
        controller._wake_for_advertisement(light.mac)

        self.assertEqual(controller._next_connect_after, 104.0)
        state = controller.diagnostic_state()
        self.assertEqual(state["state"], "unavailable")
        self.assertIsNone(state["release_guard_seconds"])

    def test_stable_release_uses_normal_guard(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]
        controller._active_proxy_address = light.mac
        controller._sessions[light.mac] = controller_module._ProxySession(
            address=light.mac,
            client=types.SimpleNamespace(is_connected=True),
            proxy_data_in=object(),
            mesh_ready=True,
            connected_at=controller.hass.loop.time() - 40,
            stable=True,
        )

        async def fake_reset(address: str) -> None:
            controller._sessions.pop(address, None)
            controller._active_proxy_address = None

        controller._reset_proxy = fake_reset  # type: ignore[method-assign]

        asyncio.run(controller.async_set_control_enabled(False))
        controller.hass.loop._time += 3
        asyncio.run(controller.async_set_control_enabled(True))

        self.assertEqual(controller._release_guard_until, 102.0)
        self.assertEqual(controller._next_connect_after, 103.0)
        self.assertFalse(controller._last_release_was_short)
        self.assertEqual(
            controller.diagnostic_state()["release_guard_seconds"],
            None,
        )

    def test_release_during_connection_attempt_uses_short_guard(self) -> None:
        controller, light = self._make_controller()
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]
        controller._pending_clients[light.mac] = object()

        asyncio.run(controller.async_set_control_enabled(False))

        self.assertEqual(controller._release_guard_until, 102.0)
        self.assertIsNone(controller._last_release_uptime)
        self.assertTrue(controller._last_release_was_short)
        self.assertTrue(controller._last_release_in_progress)

    def test_expired_short_release_guard_does_not_delay_reenable(self) -> None:
        controller, light = self._make_controller()
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]
        controller._pending_clients[light.mac] = object()

        asyncio.run(controller.async_set_control_enabled(False))
        controller.hass.loop._time += 3
        asyncio.run(controller.async_set_control_enabled(True))

        self.assertEqual(controller._release_guard_remaining(), 0.0)
        self.assertEqual(controller._next_connect_after, 103.0)
        self.assertEqual(controller.diagnostic_state()["state"], "unavailable")

    def test_last_successful_proxy_accepts_mapping_options(self) -> None:
        controller, light = self._make_controller()
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )

        self.assertEqual(controller._last_successful_proxy(), light.mac)

    def test_timeout_deprioritizes_last_successful_proxy(self) -> None:
        controller, light = self._make_controller()
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )

        controller._advance_candidate_cursor(light.mac, TimeoutError("connect timeout"))

        self.assertEqual(controller._ignored_stored_proxy, light.mac)
        self.assertIn(light.mac, controller._candidate_cooldown_until)

    def test_authorization_failure_ignores_last_successful_proxy(self) -> None:
        controller, light = self._make_controller()
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("Insufficient authorization (8)"),
        )

        self.assertEqual(controller._ignored_stored_proxy, light.mac)

    def test_proxy_slot_race_deprioritizes_last_successful_proxy(self) -> None:
        controller, light = self._make_controller()
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("ESP_GATT_CONN_CONN_CANCEL"),
        )

        self.assertEqual(controller._ignored_stored_proxy, light.mac)

    def test_last_stable_proxy_beats_stronger_rssi_on_first_try(self) -> None:
        _install_homeassistant_stubs()
        _install_mesh_stub()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        models_module = importlib.import_module("custom_components.amaran_ble.models")

        weak = models_module.AmaranLightConfig(
            name="weak",
            mac="AA:BB:CC:11:22:33",
            mesh_address=5,
        )
        strong = models_module.AmaranLightConfig(
            name="strong",
            mac="AA:BB:CC:44:55:66",
            mesh_address=6,
        )
        config = models_module.AmaranConfig(
            net_key="00112233445566778899AABBCCDDEEFF",
            app_key="FFEEDDCCBBAA99887766554433221100",
            relay_hub=weak.mac,
            lights=[weak, strong],
            proxy_candidates=[],
        )
        controller = controller_module.AmaranBleController(_FakeHass(), config)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": weak.mac})
        )

        rssi_by_address = {weak.mac: -82, strong.mac: -68}
        original_device = controller_module.bluetooth.async_ble_device_from_address
        original_info = controller_module.bluetooth.async_last_service_info
        controller_module.bluetooth.async_ble_device_from_address = (
            lambda _hass, address, **_kwargs: types.SimpleNamespace(
                rssi=rssi_by_address[address]
            )
        )
        controller_module.bluetooth.async_last_service_info = (
            lambda _hass, address, **_kwargs: types.SimpleNamespace(
                rssi=rssi_by_address[address],
                time=controller.hass.loop.time(),
                connectable=True,
            )
        )
        try:
            addresses = [weak.mac, strong.mac]
            self.assertEqual(controller._rotated_proxy_addresses(addresses)[0], weak.mac)
            self.assertEqual(controller._sort_visible_candidates(addresses)[0], weak.mac)
        finally:
            controller_module.bluetooth.async_ble_device_from_address = original_device
            controller_module.bluetooth.async_last_service_info = original_info

    def test_candidate_health_can_override_recent_stable_post_open_failures(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": other})
        )
        controller._last_service_info = (  # type: ignore[method-assign]
            lambda _address: types.SimpleNamespace(
                rssi=-70,
                time=controller.hass.loop.time(),
            )
        )

        controller._record_candidate_health_outcome(light.mac, "stable")
        controller._record_candidate_health_outcome(light.mac, "stable")
        controller._record_candidate_health_outcome(other, "proxy_notification")
        controller._record_candidate_health_outcome(other, "stable")
        controller._record_candidate_health_outcome(other, "proxy_notification")

        self.assertEqual(
            controller._sort_visible_candidates([other, light.mac])[0],
            light.mac,
        )

    def test_unknown_candidate_precedes_recent_post_open_failures(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._last_service_info = (  # type: ignore[method-assign]
            lambda _address: types.SimpleNamespace(
                rssi=-70,
                time=controller.hass.loop.time(),
            )
        )
        controller._record_candidate_health_outcome(
            light.mac,
            "proxy_notification",
        )

        self.assertEqual(
            controller._sort_visible_candidates([light.mac, other])[0],
            other,
        )

    def test_recent_gatt_failure_yields_despite_old_stable_history(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._last_service_info = (  # type: ignore[method-assign]
            lambda _address: types.SimpleNamespace(
                rssi=-70,
                time=controller.hass.loop.time(),
            )
        )
        for _ in range(7):
            controller._record_candidate_health_outcome(light.mac, "stable")
        controller._record_candidate_health_outcome(light.mac, "gatt_connect")

        self.assertGreater(controller._candidate_health_score(light.mac), 0)
        self.assertEqual(
            controller._sort_visible_candidates([light.mac, other])[0],
            other,
        )

    def test_candidate_health_penalizes_post_open_more_than_gatt_failure(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)

        controller._record_candidate_health_outcome(light.mac, "gatt_connect")
        controller._record_candidate_health_outcome(other, "proxy_notification")

        self.assertGreater(
            controller._candidate_health_score(light.mac),
            controller._candidate_health_score(other),
        )

    def test_candidate_health_penalizes_stable_disconnect_more_than_gatt_failure(
        self,
    ) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)

        controller._record_candidate_health_outcome(light.mac, "gatt_connect")
        controller._record_candidate_health_outcome(other, "stable_disconnect")

        self.assertGreater(
            controller._candidate_health_score(light.mac),
            controller._candidate_health_score(other),
        )
        self.assertEqual(
            controller._candidate_health_summary(other)["stable_disconnects"],
            1,
        )

    def test_candidate_health_promotes_ready_sample_after_stability(self) -> None:
        controller, light = self._make_controller()
        controller._record_candidate_health_outcome(light.mac, "ready")

        controller._promote_candidate_health_to_stable(light.mac)

        summary = controller._candidate_health_summary(light.mac)
        self.assertEqual(summary["samples"], 1)
        self.assertEqual(summary["stable"], 1)
        self.assertEqual(summary["ready_unproven"], 0)
        self.assertEqual(summary["recent_outcomes"], ["stable"])

    def test_operational_candidate_keeps_health_unproven_until_trusted(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._round_failed_candidates.add(other)
        controller._candidate_failure_streak[other] = 1
        controller._candidate_cooldown_until[other] = (
            controller.hass.loop.time() + 120
        )
        controller._record_candidate_health_outcome(light.mac, "ready")

        controller._remember_operational_candidate(light.mac)

        summary = controller._candidate_health_summary(light.mac)
        self.assertEqual(summary["latest_outcome"], "ready")
        self.assertEqual(summary["stable"], 0)
        self.assertEqual(summary["ready_unproven"], 1)
        self.assertEqual(controller._round_failed_candidates, set())
        self.assertEqual(controller._candidate_cooldown_until, {})

    def test_session_maturity_persists_proxy_only_after_trusted_window(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        entry = types.SimpleNamespace(options={})

        def update_entry(target: object, *, options: dict[str, object]) -> None:
            target.options = options

        controller.hass.config_entries.async_update_entry = update_entry
        controller._entry = entry
        controller._record_candidate_health_outcome(light.mac, "ready")
        session = controller_module._ProxySession(
            address=light.mac,
            connected_at=100.0,
            mesh_ready=True,
        )

        controller._update_session_maturity(session, 131.0)

        self.assertTrue(session.stable)
        self.assertFalse(session.trusted)
        self.assertEqual(
            controller._candidate_health_summary(light.mac)["latest_outcome"],
            "ready",
        )
        self.assertNotIn("last_successful_proxy", entry.options)

        controller._update_session_maturity(session, 221.0)

        self.assertTrue(session.trusted)
        self.assertEqual(
            controller._candidate_health_summary(light.mac)["latest_outcome"],
            "stable",
        )
        self.assertEqual(entry.options["last_successful_proxy"], light.mac)
        self.assertEqual(
            controller.diagnostic_state()["connect_attempt_stats"][
                "trusted_mesh_connections"
            ],
            1,
        )

    def test_recent_success_outweighs_historical_score_after_trace(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": other})
        )
        controller._last_service_info = (  # type: ignore[method-assign]
            lambda _address: types.SimpleNamespace(rssi=-70, time=0.0)
        )
        reliable_trace = (
            "gatt_connect",
            "stable",
            "gatt_connect",
            "stable",
            "stable",
            "gatt_connect",
            "stable",
            "proxy_notification",
        )
        post_open_failure_trace = (
            "gatt_or_service_discovery",
            "gatt_or_service_discovery",
            "gatt_connect",
            "gatt_connect",
            "stable",
            "gatt_connect",
            "gatt_or_service_discovery",
            "stable",
        )
        for outcome in reliable_trace:
            controller._record_candidate_health_outcome(light.mac, outcome)
        for outcome in post_open_failure_trace:
            controller._record_candidate_health_outcome(other, outcome)

        self.assertGreater(
            controller._candidate_health_score(light.mac),
            controller._candidate_health_score(other) + 1.5,
        )
        self.assertEqual(
            controller._sort_visible_candidates([other, light.mac])[0],
            other,
        )

    def test_equal_candidate_health_preserves_input_order_not_mac_order(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._last_service_info = (  # type: ignore[method-assign]
            lambda _address: types.SimpleNamespace(rssi=-70, time=0.0)
        )

        self.assertEqual(
            controller._sort_visible_candidates([other, light.mac]),
            [other, light.mac],
        )

    def test_failed_last_stable_proxy_yields_to_next_candidate(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("ESP_GATT_CONN_CONN_CANCEL"),
        )

        ordered = controller._rotated_proxy_addresses([light.mac, other])
        self.assertEqual(ordered[0], other)
        self.assertEqual(controller._ignored_stored_proxy, light.mac)

    def test_no_fresh_advertisement_retry_is_short(self) -> None:
        controller, _light = self._make_controller()

        self.assertEqual(
            controller._maintain_retry_delay("RuntimeError: no candidates"),
            2,
        )

    def test_full_candidate_round_gives_gatt_recovery_time(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._connect_failure_count = 2
        controller._round_failed_candidates.update((light.mac, other))

        self.assertTrue(controller._full_candidate_round_failed())
        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_FAIL_ESTABLISH"),
            12,
        )

    def test_preferred_gatt_retry_gets_full_release_window(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )
        controller._round_failed_candidates.update((light.mac, other))
        controller._candidate_cooldown_until[light.mac] = 999
        controller._record_candidate_health_outcome(light.mac, "gatt_connect")

        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.PREFERRED_PROXY_GATT_RECOVERY_DELAY,
        )
        self.assertEqual(
            controller_module.PREFERRED_PROXY_GATT_RECOVERY_DELAY,
            30,
        )

    def test_command_wait_exits_after_full_candidate_round_failure(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._control_enabled = True
        starts = 0

        async def failed_connect() -> object:
            controller._round_failed_candidates.update((light.mac, other))
            raise RuntimeError("Unable to connect: no candidates")

        def start_connect_task() -> asyncio.Task:
            nonlocal starts
            starts += 1
            return asyncio.create_task(failed_connect())

        controller._start_connect_task = start_connect_task  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            RuntimeError,
            "background connector will retry after GATT recovery",
        ):
            asyncio.run(controller._ensure_ready_for_command())

        self.assertEqual(starts, 1)

    def test_full_candidate_round_counts_unique_candidates(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._connect_failure_count = 3
        controller._round_failed_candidates.add(light.mac)

        self.assertFalse(controller._full_candidate_round_failed())

        controller._round_failed_candidates.add(other)

        self.assertTrue(controller._full_candidate_round_failed())

    def test_new_candidate_round_retries_last_stable_proxy_first(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )
        controller._candidate_cursor = 1
        controller._connect_failure_count = 2
        controller._ignored_stored_proxy = light.mac
        controller._round_failed_candidates.update((light.mac, other))
        controller._candidate_cooldown_until[light.mac] = 999
        controller._record_candidate_health_outcome(light.mac, "gatt_connect")

        controller._restart_candidate_round()

        self.assertEqual(controller._connect_failure_count, 0)
        self.assertEqual(controller._round_failed_candidates, set())
        self.assertIsNone(controller._ignored_stored_proxy)
        self.assertEqual(controller._candidate_cursor, 0)
        self.assertNotIn(light.mac, controller._candidate_cooldown_until)
        self.assertEqual(
            controller._rotated_proxy_addresses([light.mac, other])[0],
            light.mac,
        )
        self.assertEqual(
            controller.diagnostic_state()["connect_attempt_stats"][
                "preferred_proxy_round_restarts"
            ],
            1,
        )
        self.assertEqual(
            controller.diagnostic_state()["connect_attempt_stats"][
                "preferred_proxy_cooldown_overrides"
            ],
            1,
        )

    def test_new_candidate_round_preserves_post_open_cooldown(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )
        controller._candidate_cooldown_until[light.mac] = 999
        controller._record_candidate_health_outcome(
            light.mac,
            "proxy_notification",
        )
        controller._round_failed_candidates.update((light.mac, other))

        controller._restart_candidate_round()

        self.assertEqual(
            controller._candidate_cooldown_until[light.mac],
            999,
        )
        self.assertNotIn(
            "preferred_proxy_cooldown_overrides",
            controller.diagnostic_state()["connect_attempt_stats"],
        )

    def test_full_round_cccd_failure_uses_gatt_recovery_time(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._connect_failure_count = 2
        controller._round_failed_candidates.update((light.mac, other))

        self.assertEqual(
            controller._maintain_retry_delay("CCCD notification setup timeout"),
            12,
        )

    def test_preopen_failure_storm_preserves_034_fixed_recovery(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._round_failed_candidates.update((light.mac, other))

        controller._consecutive_preopen_failures = 2
        self.assertEqual(controller._maintain_retry_delay(), 12)

        controller._consecutive_preopen_failures = 4
        self.assertEqual(
            controller._maintain_retry_delay(),
            controller_module.PREOPEN_STORM_RECOVERY_DELAY,
        )
        self.assertEqual(controller_module.PREOPEN_STORM_RECOVERY_DELAY, 30)

        controller._consecutive_preopen_failures = 6
        self.assertEqual(
            controller._maintain_retry_delay(),
            controller_module.PREOPEN_STORM_RECOVERY_DELAY,
        )
        self.assertEqual(controller_module.PREOPEN_STORM_RECOVERY_DELAY, 30)

    def test_post_open_progress_resets_preopen_failure_streak(self) -> None:
        controller, light = self._make_controller()
        controller._consecutive_preopen_failures = 3
        controller._current_connect_trace = {
            "address": light.mac,
            "started": controller.hass.loop.time(),
            "attempt_id": 4,
            "ownership_cycle": 1,
            "candidate_round": 2,
            "candidate_health_score": -3.0,
            "candidate_health_samples": 3,
            "round_failed_before": [],
            "requested_cache_mode": "with_cache",
            "requested_cache_reason": "normal_cache",
            "advertisement_at_selection": {},
            "advertisement_at_connect": {},
            "stages": {"candidate_selected": 0.0, "gatt_services_ready": 1.0},
        }

        controller._record_connect_attempt(
            light.mac,
            "failed",
            2.0,
            "Mesh Proxy CCCD notification setup failed",
        )

        self.assertEqual(controller._consecutive_preopen_failures, 0)

    def test_control_cycle_reset_clears_preopen_failure_streak(self) -> None:
        controller, _light = self._make_controller()
        controller._consecutive_preopen_failures = 7

        controller._reset_connection_recovery_state()

        self.assertEqual(controller._consecutive_preopen_failures, 0)

    def test_candidate_cooldown_temporarily_filters_failed_proxy(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller._candidate_cooldown_until[light.mac] = controller.hass.loop.time() + 10

        self.assertEqual(
            controller._filter_candidate_cooldowns([light.mac, other]),
            [other],
        )

    def test_autoconnect_does_not_retry_failed_candidate_in_same_round(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._round_failed_candidates.add(light.mac)
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        controller._has_fresh_ble_device = (  # type: ignore[method-assign]
            lambda _address, **_kwargs: True
        )
        controller._sort_visible_candidates = (  # type: ignore[method-assign]
            lambda addresses: addresses
        )

        ordered = asyncio.run(
            controller._connect_candidate_order([light.mac, other])
        )

        self.assertEqual(ordered, [other])

    def test_generic_handshake_failure_yields_to_another_candidate(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("Mesh Proxy notification setup failed"),
        )

        self.assertIn(light.mac, controller._candidate_cooldown_until)
        self.assertEqual(
            controller._filter_candidate_cooldowns([light.mac, other]),
            [other],
        )

    def test_gatt_failure_immediately_yields_to_another_candidate(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("ESP_GATT_CONN_FAIL_ESTABLISH"),
        )

        self.assertEqual(controller._candidate_cursor, 1)
        self.assertIn(light.mac, controller._round_failed_candidates)
        self.assertIn(light.mac, controller._candidate_cooldown_until)

    def test_gatt_failure_cooldown_grows_and_is_capped(self) -> None:
        controller, light = self._make_controller()
        controller.config.proxy_candidates.append("AA:BB:CC:44:55:66")
        error = RuntimeError("ESP_GATT_CONN_FAIL_ESTABLISH")

        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 120.0)
        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 180.0)
        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 270.0)
        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 300.0)
        self.assertEqual(controller._candidate_failure_streak[light.mac], 4)

    def test_ready_connection_keeps_round_failures_until_stable(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._round_failed_candidates.add(other)
        controller._candidate_failure_streak[light.mac] = 2
        controller._candidate_failure_streak[other] = 3
        controller._candidate_cooldown_until[light.mac] = (
            controller.hass.loop.time() + 120
        )
        controller._candidate_cooldown_until[other] = (
            controller.hass.loop.time() + 180
        )

        controller._record_candidate_health_outcome(light.mac, "ready")
        controller._mark_candidate_connected(light.mac)

        self.assertEqual(controller._round_failed_candidates, {other})
        self.assertEqual(controller._candidate_failure_streak[light.mac], 2)
        self.assertNotIn(light.mac, controller._candidate_cooldown_until)

        controller._remember_successful_candidate(light.mac)

        self.assertEqual(controller._round_failed_candidates, set())
        self.assertEqual(controller._candidate_failure_streak, {})
        self.assertEqual(controller._candidate_cooldown_until, {})
        self.assertEqual(
            controller._candidate_health_summary(light.mac)["latest_outcome"],
            "stable",
        )
        self.assertEqual(
            controller.diagnostic_state()["connect_attempt_stats"][
                "stable_mesh_backoff_resets"
            ],
            1,
        )

    def test_stable_candidate_preserves_post_open_quarantine(self) -> None:
        controller, light = self._make_controller()
        preopen = "AA:BB:CC:44:55:66"
        post_open = "AA:BB:CC:77:88:99"
        controller.config.proxy_candidates.extend([preopen, post_open])
        now = controller.hass.loop.time()
        controller._candidate_failure_streak[preopen] = 2
        controller._candidate_failure_reason[preopen] = "gatt_connect"
        controller._candidate_cooldown_until[preopen] = now + 120
        controller._cooldown_failed_candidate(
            post_open,
            RuntimeError("Proxy disconnected after 45.0s"),
            failure_reason="stable_disconnect",
        )
        controller._record_candidate_health_outcome(light.mac, "ready")

        controller._remember_successful_candidate(light.mac)

        self.assertNotIn(preopen, controller._candidate_failure_streak)
        self.assertNotIn(preopen, controller._candidate_failure_reason)
        self.assertNotIn(preopen, controller._candidate_cooldown_until)
        self.assertIn(post_open, controller._candidate_failure_streak)
        self.assertEqual(
            controller._candidate_failure_reason[post_open],
            "stable_disconnect",
        )
        self.assertGreater(
            controller._candidate_post_open_quarantine_until[post_open],
            now,
        )
        self.assertGreater(
            controller._candidate_cooldown_remaining(post_open) or 0,
            0,
        )
        self.assertEqual(
            controller._filter_candidate_cooldowns(
                [post_open, light.mac]
            ),
            [light.mac],
        )
        self.assertEqual(
            controller.diagnostic_state()["connect_attempt_stats"][
                "stable_mesh_quarantines_preserved"
            ],
            1,
        )

    def test_connection_state_waits_for_stability_window(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        session = controller_module._ProxySession(
            address=light.mac,
            client=types.SimpleNamespace(is_connected=True),
            proxy_data_in=object(),
            mesh_ready=True,
            connected_at=controller.hass.loop.time() - 5,
        )
        controller._sessions[light.mac] = session
        controller._active_proxy_address = light.mac
        controller._mesh_available = True

        self.assertEqual(
            controller.diagnostic_state()["state"],
            "stabilizing",
        )
        self.assertFalse(controller.diagnostic_state()["connection_trusted"])
        self.assertGreater(
            controller.diagnostic_state()["connection_trust_seconds_remaining"],
            0,
        )

        session.stable = True

        self.assertEqual(
            controller.diagnostic_state()["state"],
            "connected",
        )
        self.assertFalse(controller.diagnostic_state()["connection_trusted"])

        session.trusted = True

        self.assertTrue(controller.diagnostic_state()["connection_trusted"])
        self.assertIsNone(
            controller.diagnostic_state()["connection_trust_seconds_remaining"]
        )

    def test_uncached_cccd_recovery_retries_same_candidate_without_handoff(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._service_cache_bypass.add(light.mac)
        controller._cccd_reconnect_pending.add(light.mac)

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("Mesh Proxy CCCD notification setup failed"),
        )

        self.assertEqual(controller._candidate_cursor, 0)
        self.assertNotIn(light.mac, controller._cccd_reconnect_pending)
        self.assertIn(light.mac, controller._service_cache_bypass)
        self.assertNotIn(light.mac, controller._candidate_cooldown_until)
        self.assertNotIn(light.mac, controller._round_failed_candidates)

    def test_cached_cccd_recovery_retries_same_candidate_without_handoff(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._cccd_reconnect_pending.add(light.mac)

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("Mesh Proxy CCCD notification setup failed"),
        )

        self.assertEqual(controller._candidate_cursor, 0)
        self.assertNotIn(light.mac, controller._cccd_reconnect_pending)
        self.assertNotIn(light.mac, controller._service_cache_bypass)
        self.assertNotIn(light.mac, controller._candidate_cooldown_until)
        self.assertNotIn(light.mac, controller._round_failed_candidates)

    def test_first_cccd_failure_hands_off_to_fresh_unused_candidate(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._service_cache_bypass.add(light.mac)
        controller._cccd_reconnect_pending.add(light.mac)
        controller._cached_cccd_retry_attempted.add(light.mac)
        controller._has_fresh_ble_device = (  # type: ignore[method-assign]
            lambda address, **_kwargs: address == other
        )

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("Mesh Proxy CCCD notification setup failed"),
        )

        self.assertEqual(controller._candidate_cursor, 1)
        self.assertNotIn(light.mac, controller._cccd_reconnect_pending)
        self.assertIn(light.mac, controller._service_cache_bypass)
        self.assertIn(light.mac, controller._round_failed_candidates)
        self.assertEqual(
            controller._notification_settle_seconds(light.mac),
            5.0,
        )

    def test_failed_uncached_recovery_clears_bypass_and_rotates(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._service_cache_bypass.add(light.mac)
        controller._uncached_recovery_attempted.add(light.mac)

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("ESP_GATT_CONN_FAIL_ESTABLISH"),
        )

        self.assertEqual(controller._candidate_cursor, 1)
        self.assertNotIn(light.mac, controller._service_cache_bypass)
        self.assertNotIn(light.mac, controller._uncached_recovery_attempted)
        self.assertIn(light.mac, controller._round_failed_candidates)

    def test_second_cccd_failure_yields_to_another_candidate(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._service_cache_bypass.add(light.mac)

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("Mesh Proxy CCCD notification setup failed"),
        )

        self.assertEqual(controller._candidate_cursor, 1)
        self.assertIn(light.mac, controller._candidate_cooldown_until)

    def test_advertisement_wait_uses_only_non_cooled_candidates(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller._candidate_cooldown_until[light.mac] = controller.hass.loop.time() + 10
        waited_for: list[list[str]] = []

        async def fake_wait(addresses: list[str], **_kwargs: object) -> None:
            waited_for.append(addresses)
            return None

        controller._wait_for_any_proxy_advertisement = fake_wait  # type: ignore[method-assign]

        result = asyncio.run(controller._connect_candidate_order([light.mac, other]))

        self.assertEqual(result, [])
        self.assertEqual(waited_for, [[other]])

    def test_startup_uses_only_one_fresh_candidate_per_round(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"
        info_by_address = {
            light.mac: types.SimpleNamespace(
                address=light.mac,
                time=controller.hass.loop.time(),
                rssi=-70,
                connectable=True,
            ),
            other: types.SimpleNamespace(
                address=other,
                time=controller.hass.loop.time() - 40,
                rssi=-65,
                connectable=True,
            ),
        }
        original_device = controller_module.bluetooth.async_ble_device_from_address
        original_info = controller_module.bluetooth.async_last_service_info
        controller_module.bluetooth.async_ble_device_from_address = (
            lambda _hass, address, **_kwargs: types.SimpleNamespace(
                rssi=info_by_address[address].rssi
            )
        )
        controller_module.bluetooth.async_last_service_info = (
            lambda _hass, address, **_kwargs: info_by_address[address]
        )
        try:
            result = asyncio.run(
                controller._connect_candidate_order([light.mac, other])
            )
        finally:
            controller_module.bluetooth.async_ble_device_from_address = original_device
            controller_module.bluetooth.async_last_service_info = original_info

        self.assertEqual(result, [light.mac])

    def test_active_scan_stale_result_does_not_return_cached_device(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        stale_info = types.SimpleNamespace(
            address=light.mac,
            time=controller.hass.loop.time() - 200,
            device=object(),
            connectable=True,
        )

        async def fake_process_advertisements(*_args: object, **_kwargs: object):
            controller.hass.loop._time += 0.1
            return stale_info

        original_device = controller_module.bluetooth.async_ble_device_from_address
        original_info = controller_module.bluetooth.async_last_service_info
        original_process = getattr(
            controller_module.bluetooth,
            "async_process_advertisements",
            None,
        )
        original_timeout = controller_module.DISCOVERY_TIMEOUT
        controller_module.bluetooth.async_ble_device_from_address = (
            lambda *_args, **_kwargs: types.SimpleNamespace(rssi=-70)
        )
        controller_module.bluetooth.async_last_service_info = (
            lambda *_args, **_kwargs: stale_info
        )
        controller_module.bluetooth.async_process_advertisements = fake_process_advertisements
        controller_module.DISCOVERY_TIMEOUT = 0.2
        try:
            result = asyncio.run(
                controller._async_get_ble_device(light.mac, allow_scan=True)
            )
        finally:
            controller_module.bluetooth.async_ble_device_from_address = original_device
            controller_module.bluetooth.async_last_service_info = original_info
            if original_process is None:
                delattr(controller_module.bluetooth, "async_process_advertisements")
            else:
                controller_module.bluetooth.async_process_advertisements = original_process
            controller_module.DISCOVERY_TIMEOUT = original_timeout

        self.assertIsNone(result)

    def test_disconnect_timeout_has_longer_backoff(self) -> None:
        controller, _light = self._make_controller()
        detail = "Timeout waiting for connect response, disconnect timed out: True"

        self.assertEqual(controller._candidate_cooldown_seconds(RuntimeError(detail)), 20.0)
        self.assertEqual(controller._maintain_retry_delay(detail), 2)

    def test_notification_backoff_expires(self) -> None:
        controller, light = self._make_controller()
        controller._notification_backoff_until[light.mac] = (
            controller.hass.loop.time() + 10
        )

        self.assertEqual(controller._notification_backoff_remaining(light.mac), 10.0)

        controller.hass.loop._time += 11

        self.assertIsNone(controller._notification_backoff_remaining(light.mac))
        self.assertNotIn(light.mac, controller._notification_backoff_until)

    def test_cccd_timeout_has_specific_diagnostic_hint(self) -> None:
        controller, _light = self._make_controller()
        controller._last_maintain_error = (
            "Mesh Proxy CCCD notification setup failed: TimeoutError"
        )

        self.assertEqual(controller._last_error_hint(), "cccd_timeout")
        self.assertIn("service cache", controller._connection_advice())

    def test_conn_cancel_is_not_reported_as_out_of_slots(self) -> None:
        controller, _light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller._last_maintain_error = "Error ESP_GATT_CONN_CONN_CANCEL"

        self.assertEqual(controller._last_error_hint(), "gatt_connect_cancelled")
        self.assertIn("does not by itself mean", controller._connection_advice())
        self.assertEqual(
            controller._maintain_retry_delay(controller._last_maintain_error),
            controller_module.GATT_ESTABLISH_RETRY_DELAY,
        )

        controller._last_maintain_error = "Out of connection slots"
        self.assertEqual(controller._last_error_hint(), "proxy_out_of_slots")
        self.assertEqual(
            controller._maintain_retry_delay(controller._last_maintain_error),
            controller_module.OUT_OF_SLOTS_RETRY_DELAY,
        )

    def test_silent_notification_timeout_keeps_connected_write_only_session(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            is_connected = True

            def __init__(self) -> None:
                self.start_notify_calls = 0
                self.clear_cache_calls = 0

            async def start_notify(self, *_args: object) -> None:
                self.start_notify_calls += 1
                raise TimeoutError("CCCD not ready")

            async def clear_cache(self) -> bool:
                self.clear_cache_calls += 1
                return True

        client = FakeClient()
        session = controller_module._ProxySession(
            address=light.mac,
            client=client,
            proxy_data_in=object(),
            proxy_data_out=object(),
            status_notifications_supported=False,
            notification_callback=lambda *_args: None,
        )
        controller._sessions[light.mac] = session
        original_settle = controller_module.WRITE_ONLY_RECOVERY_SETTLE_SECONDS
        controller_module.WRITE_ONLY_RECOVERY_SETTLE_SECONDS = 0
        try:
            result = asyncio.run(
                controller._enable_proxy_notifications_for_session(session)
            )
        finally:
            controller_module.WRITE_ONLY_RECOVERY_SETTLE_SECONDS = original_settle

        self.assertFalse(result)
        self.assertEqual(client.start_notify_calls, 1)
        self.assertFalse(session.notify_started)
        self.assertFalse(session.status_notifications_supported)
        self.assertNotIn(light.mac, controller._service_cache_bypass)
        self.assertNotIn(light.mac, controller._cached_cccd_retry_attempted)
        self.assertNotIn(light.mac, controller._cccd_reconnect_pending)
        self.assertIn(light.mac, controller._notification_backoff_until)
        self.assertEqual(client.clear_cache_calls, 0)

    def test_disconnected_first_notification_timeout_preserves_service_cache(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            is_connected = False

            def __init__(self) -> None:
                self.clear_cache_calls = 0

            async def start_notify(self, *_args: object) -> None:
                raise TimeoutError("CCCD not ready before disconnect")

            async def clear_cache(self) -> bool:
                self.clear_cache_calls += 1
                return True

        client = FakeClient()
        session = controller_module._ProxySession(
            address=light.mac,
            client=client,
            proxy_data_in=object(),
            proxy_data_out=object(),
            status_notifications_supported=False,
            notification_callback=lambda *_args: None,
        )

        with self.assertRaisesRegex(RuntimeError, "preserving the known"):
            asyncio.run(controller._enable_proxy_notifications_for_session(session))

        self.assertIn(light.mac, controller._cached_cccd_retry_attempted)
        self.assertIn(light.mac, controller._cccd_reconnect_pending)
        self.assertNotIn(light.mac, controller._service_cache_bypass)
        self.assertEqual(client.clear_cache_calls, 0)

    def test_notification_link_drop_during_settle_does_not_use_write_only(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            def __init__(self) -> None:
                self.connected_checks = 0

            @property
            def is_connected(self) -> bool:
                self.connected_checks += 1
                return self.connected_checks == 1

            async def start_notify(self, *_args: object) -> None:
                raise TimeoutError("CCCD timed out before link drop")

        client = FakeClient()
        session = controller_module._ProxySession(
            address=light.mac,
            client=client,
            proxy_data_in=object(),
            proxy_data_out=object(),
            status_notifications_supported=False,
            notification_callback=lambda *_args: None,
        )
        original_settle = controller_module.WRITE_ONLY_RECOVERY_SETTLE_SECONDS
        controller_module.WRITE_ONLY_RECOVERY_SETTLE_SECONDS = 0
        try:
            with self.assertRaisesRegex(RuntimeError, "preserving the known"):
                asyncio.run(
                    controller._enable_proxy_notifications_for_session(session)
                )
        finally:
            controller_module.WRITE_ONLY_RECOVERY_SETTLE_SECONDS = original_settle

        self.assertIn(light.mac, controller._cached_cccd_retry_attempted)
        self.assertNotIn(light.mac, controller._notification_backoff_until)

    def test_disconnected_second_notification_timeout_clears_service_cache(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            is_connected = False

            def __init__(self) -> None:
                self.clear_cache_calls = 0

            async def start_notify(self, *_args: object) -> None:
                raise TimeoutError("CCCD still not ready")

            async def clear_cache(self) -> bool:
                self.clear_cache_calls += 1
                return True

        client = FakeClient()
        session = controller_module._ProxySession(
            address=light.mac,
            client=client,
            proxy_data_in=object(),
            proxy_data_out=object(),
            status_notifications_supported=False,
            notification_callback=lambda *_args: None,
        )
        controller._sessions[light.mac] = session
        controller._cached_cccd_retry_attempted.add(light.mac)

        with self.assertRaisesRegex(RuntimeError, "bypass cache"):
            asyncio.run(controller._enable_proxy_notifications_for_session(session))

        self.assertIn(light.mac, controller._service_cache_bypass)
        self.assertIn(light.mac, controller._cccd_reconnect_pending)
        self.assertEqual(client.clear_cache_calls, 1)

    def test_terminal_notification_error_skips_same_candidate_recovery(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            is_connected = False

            def __init__(self) -> None:
                self.clear_cache_calls = 0

            async def start_notify(self, *_args: object) -> None:
                raise RuntimeError(
                    "Bluetooth GATT Error handle=54 error=133 description=Error"
                )

            async def clear_cache(self) -> bool:
                self.clear_cache_calls += 1
                return True

        client = FakeClient()
        session = controller_module._ProxySession(
            address=light.mac,
            client=client,
            proxy_data_in=object(),
            proxy_data_out=object(),
            status_notifications_supported=False,
            notification_callback=lambda *_args: None,
        )
        controller._sessions[light.mac] = session
        controller._service_cache_bypass.add(light.mac)
        controller._cccd_reconnect_pending.add(light.mac)
        controller._cached_cccd_retry_attempted.add(light.mac)
        controller._uncached_recovery_attempted.add(light.mac)

        with self.assertRaisesRegex(RuntimeError, "terminal GATT error"):
            asyncio.run(controller._enable_proxy_notifications_for_session(session))

        self.assertNotIn(light.mac, controller._service_cache_bypass)
        self.assertNotIn(light.mac, controller._cccd_reconnect_pending)
        self.assertNotIn(light.mac, controller._cached_cccd_retry_attempted)
        self.assertNotIn(light.mac, controller._uncached_recovery_attempted)
        self.assertEqual(client.clear_cache_calls, 0)

    def test_successful_notification_setup_clears_service_cache_bypass(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            async def start_notify(self, *_args: object) -> None:
                return None

        session = controller_module._ProxySession(
            address=light.mac,
            client=FakeClient(),
            proxy_data_out=object(),
            status_notifications_supported=False,
            notification_callback=lambda *_args: None,
        )
        controller._service_cache_bypass.add(light.mac)
        controller._cached_cccd_retry_attempted.add(light.mac)

        asyncio.run(controller._enable_proxy_notifications_for_session(session))

        self.assertNotIn(light.mac, controller._service_cache_bypass)
        self.assertNotIn(light.mac, controller._cached_cccd_retry_attempted)
        self.assertTrue(session.notify_started)
        self.assertTrue(session.status_notifications_supported)

    def test_captured_notification_order_is_proxy_then_provisioning(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        calls: list[tuple[object, object]] = []

        class FakeClient:
            async def start_notify(self, characteristic: object, callback: object) -> None:
                calls.append((characteristic, callback))

        callback = lambda *_args: None
        provisioning_characteristic = object()
        proxy_characteristic = object()
        session = controller_module._ProxySession(
            address=light.mac,
            client=FakeClient(),
            provisioning_data_out=provisioning_characteristic,
            proxy_data_out=proxy_characteristic,
            notification_callback=callback,
            status_notifications_supported=False,
        )

        asyncio.run(controller._enable_proxy_notifications(session))

        self.assertEqual(calls[0], (proxy_characteristic, callback))
        self.assertEqual(calls[1][0], provisioning_characteristic)
        self.assertTrue(session.provisioning_notify_started)
        self.assertTrue(session.notify_started)
        self.assertTrue(session.status_notifications_supported)

    def test_optional_provisioning_notification_failure_does_not_block_proxy(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        provisioning_characteristic = object()
        proxy_characteristic = object()
        calls: list[object] = []

        class FakeClient:
            async def start_notify(self, characteristic: object, _callback: object) -> None:
                calls.append(characteristic)
                if characteristic is provisioning_characteristic:
                    raise RuntimeError("optional PB-Out CCCD failed")

        session = controller_module._ProxySession(
            address=light.mac,
            client=FakeClient(),
            provisioning_data_out=provisioning_characteristic,
            proxy_data_out=proxy_characteristic,
            notification_callback=lambda *_args: None,
            status_notifications_supported=False,
        )

        asyncio.run(controller._enable_proxy_notifications(session))

        self.assertEqual(calls, [proxy_characteristic, provisioning_characteristic])
        self.assertFalse(session.provisioning_notify_started)
        self.assertTrue(session.notify_started)

    def test_required_proxy_notification_failure_skips_optional_provisioning(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        provisioning_characteristic = object()
        proxy_characteristic = object()
        calls: list[object] = []

        class FakeClient:
            async def start_notify(self, characteristic: object, _callback: object) -> None:
                calls.append(characteristic)
                if characteristic is proxy_characteristic:
                    raise RuntimeError("required Proxy-Out CCCD failed")

        session = controller_module._ProxySession(
            address=light.mac,
            client=FakeClient(),
            provisioning_data_out=provisioning_characteristic,
            proxy_data_out=proxy_characteristic,
            notification_callback=lambda *_args: None,
            status_notifications_supported=False,
        )

        with self.assertRaisesRegex(RuntimeError, "required Proxy-Out"):
            asyncio.run(controller._enable_proxy_notifications(session))

        self.assertEqual(calls, [proxy_characteristic])
        self.assertFalse(session.notify_started)
        self.assertFalse(session.provisioning_notify_started)

    def test_connect_session_enables_notifications_before_proxy_login(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        provisioning_characteristic = object()
        proxy_in = object()
        proxy_out = object()
        events: list[str] = []

        class FakeServices:
            def get_characteristic(self, uuid: str) -> object | None:
                return {
                    controller_module.PROVISIONING_DATA_OUT_UUID: (
                        provisioning_characteristic
                    ),
                    controller_module.PROXY_DATA_IN_UUID: proxy_in,
                    controller_module.PROXY_DATA_OUT_UUID: proxy_out,
                }.get(uuid)

        client = types.SimpleNamespace(is_connected=True, services=FakeServices())

        async def fake_get_device(*_args: object, **_kwargs: object) -> object:
            return object()

        async def fake_establish(*_args: object, **_kwargs: object) -> object:
            return client

        async def fake_enable(_session: object) -> bool:
            events.append("notifications")
            return True

        async def fake_login(
            _session: object,
            *,
            wait_for_filter_status: bool,
        ) -> bool:
            events.append(f"login:{wait_for_filter_status}")
            return True

        controller._async_get_ble_device = fake_get_device  # type: ignore[method-assign]
        controller._establish_bleak_client = fake_establish  # type: ignore[method-assign]
        controller._enable_proxy_notifications_for_session = (  # type: ignore[method-assign]
            fake_enable
        )
        controller._initialize_proxy_login = fake_login  # type: ignore[method-assign]
        session = controller_module._ProxySession(light.mac)
        original_settle = controller_module.GATT_NOTIFICATION_SETTLE_SECONDS
        controller_module.GATT_NOTIFICATION_SETTLE_SECONDS = 0
        try:
            asyncio.run(controller._connect_session(session))
        finally:
            controller_module.GATT_NOTIFICATION_SETTLE_SECONDS = original_settle

        self.assertEqual(events, ["notifications", "login:True"])
        self.assertIs(session.provisioning_data_out, provisioning_characteristic)
        self.assertTrue(session.mesh_ready)

    def test_connect_session_write_only_fallback_skips_filter_status_wait(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        proxy_in = object()
        proxy_out = object()
        events: list[str] = []

        class FakeServices:
            def get_characteristic(self, uuid: str) -> object | None:
                return {
                    controller_module.PROXY_DATA_IN_UUID: proxy_in,
                    controller_module.PROXY_DATA_OUT_UUID: proxy_out,
                }.get(uuid)

        client = types.SimpleNamespace(is_connected=True, services=FakeServices())

        async def fake_get_device(*_args: object, **_kwargs: object) -> object:
            return object()

        async def fake_establish(*_args: object, **_kwargs: object) -> object:
            return client

        async def fake_enable(_session: object) -> bool:
            events.append("notifications:write_only")
            return False

        async def fake_login(
            _session: object,
            *,
            wait_for_filter_status: bool,
        ) -> bool:
            events.append(f"login:{wait_for_filter_status}")
            return False

        controller._async_get_ble_device = fake_get_device  # type: ignore[method-assign]
        controller._establish_bleak_client = fake_establish  # type: ignore[method-assign]
        controller._enable_proxy_notifications_for_session = (  # type: ignore[method-assign]
            fake_enable
        )
        controller._initialize_proxy_login = fake_login  # type: ignore[method-assign]
        session = controller_module._ProxySession(light.mac)
        original_settle = controller_module.GATT_NOTIFICATION_SETTLE_SECONDS
        controller_module.GATT_NOTIFICATION_SETTLE_SECONDS = 0
        try:
            asyncio.run(controller._connect_session(session))
        finally:
            controller_module.GATT_NOTIFICATION_SETTLE_SECONDS = original_settle

        self.assertEqual(
            events,
            ["notifications:write_only", "login:False"],
        )
        self.assertFalse(session.status_notifications_supported)
        self.assertTrue(session.mesh_ready)

    def test_notification_settle_waits_for_proxy_connection_update(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        self.assertEqual(controller_module.GATT_NOTIFICATION_SETTLE_SECONDS, 3.0)
        self.assertEqual(controller._notification_settle_seconds(light.mac), 3.0)

    def test_notification_recovery_uses_longer_settle(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        controller._cached_cccd_retry_attempted.add(light.mac)

        self.assertEqual(
            controller._notification_settle_seconds(light.mac),
            controller_module.GATT_NOTIFICATION_RECOVERY_SETTLE_SECONDS,
        )
        self.assertEqual(
            controller_module.GATT_NOTIFICATION_RECOVERY_SETTLE_SECONDS,
            5.0,
        )

    def test_inactive_candidate_disconnect_does_not_drop_active_proxy(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller._control_enabled = True
        controller._active_proxy_address = light.mac
        controller._set_mesh_available(True)

        controller._handle_proxy_disconnect(other)

        self.assertEqual(controller._active_proxy_address, light.mac)
        self.assertTrue(controller._mesh_available)

    def test_short_active_disconnect_quarantines_candidate(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )
        controller._control_enabled = True
        controller._active_proxy_address = light.mac
        controller._sessions[light.mac] = controller_module._ProxySession(
            address=light.mac,
            connected_at=controller.hass.loop.time() - 5,
        )
        controller._set_mesh_available(True)

        controller._handle_proxy_disconnect(light.mac)

        self.assertIsNone(controller._active_proxy_address)
        self.assertEqual(controller._ignored_stored_proxy, light.mac)
        self.assertEqual(controller._candidate_cursor, 1)
        self.assertEqual(
            controller._candidate_cooldown_until[light.mac],
            controller.hass.loop.time()
            + controller_module.SHORT_CONNECTION_COOLDOWN,
        )
        self.assertEqual(
            controller._candidate_post_open_quarantine_until[light.mac],
            controller.hass.loop.time()
            + controller_module.SHORT_CONNECTION_COOLDOWN,
        )
        self.assertEqual(
            controller._candidate_failure_reason[light.mac],
            "short_disconnect",
        )
        self.assertIn(light.mac, controller._round_failed_candidates)

    def test_probation_active_disconnect_quarantines_candidate_for_full_window(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._control_enabled = True
        controller._active_proxy_address = light.mac
        controller._sessions[light.mac] = controller_module._ProxySession(
            address=light.mac,
            connected_at=controller.hass.loop.time() - 45,
            stable=True,
        )
        controller._set_mesh_available(True)

        controller._handle_proxy_disconnect(light.mac)

        self.assertEqual(
            controller._candidate_failure_reason[light.mac],
            "probation_disconnect",
        )
        self.assertEqual(
            controller._candidate_quarantine_remaining(light.mac),
            controller_module.PROBATION_CONNECTION_COOLDOWN,
        )
        self.assertEqual(
            controller._candidate_health_summary(light.mac)["latest_outcome"],
            "probation_disconnect",
        )
        self.assertEqual(
            controller._candidate_health_summary(light.mac)[
                "probation_disconnects"
            ],
            1,
        )

    def test_trusted_active_disconnect_uses_stable_disconnect(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._control_enabled = True
        controller._active_proxy_address = light.mac
        controller._sessions[light.mac] = controller_module._ProxySession(
            address=light.mac,
            connected_at=controller.hass.loop.time() - 150,
            stable=True,
            trusted=True,
        )
        controller._set_mesh_available(True)

        controller._handle_proxy_disconnect(light.mac)

        self.assertEqual(
            controller._candidate_failure_reason[light.mac],
            "stable_disconnect",
        )
        self.assertGreater(
            controller._candidate_quarantine_remaining(light.mac) or 0,
            0,
        )
        self.assertEqual(
            controller._candidate_health_summary(light.mac)["latest_outcome"],
            "stable_disconnect",
        )

    def test_standard_connector_receives_persistent_proxy_options(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        calls: dict[str, object] = {}
        connected_client = types.SimpleNamespace(is_connected=True)

        async def fake_establish(
            client_class: type,
            device: object,
            name: str,
            **kwargs: object,
        ) -> object:
            calls.update(
                client_class=client_class,
                device=device,
                name=name,
                kwargs=kwargs,
            )
            created = kwargs["amaran_client_created"]
            created(connected_client)
            return connected_client

        original = controller_module.establish_connection
        controller_module.establish_connection = fake_establish
        device = object()
        callback = lambda *_: None
        try:
            result = asyncio.run(
                controller._establish_bleak_client(device, light.mac, callback)
            )
        finally:
            controller_module.establish_connection = original

        kwargs = calls["kwargs"]
        self.assertIs(result, connected_client)
        self.assertIs(calls["device"], device)
        self.assertIs(calls["client_class"], controller_module._AmaranBleakClient)
        self.assertEqual(kwargs["max_attempts"], 1)
        self.assertTrue(kwargs["use_services_cache"])
        self.assertEqual(
            kwargs["services"],
            [
                controller_module.MESH_PROVISIONING_SERVICE_UUID,
                controller_module.MESH_PROXY_SERVICE_UUID,
            ],
        )
        self.assertNotIn(light.mac, controller._pending_clients)

    def test_normal_attempts_request_service_cache_across_ownership_cycles(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        cache_decisions: list[bool] = []
        connected_client = types.SimpleNamespace(is_connected=True)

        async def fake_establish(
            _client_class: type,
            _device: object,
            _name: str,
            **kwargs: object,
        ) -> object:
            cache_decisions.append(bool(kwargs["use_services_cache"]))
            kwargs["amaran_client_created"](connected_client)
            return connected_client

        original = controller_module.establish_connection
        controller_module.establish_connection = fake_establish
        try:
            asyncio.run(
                controller._establish_bleak_client(
                    object(),
                    light.mac,
                    lambda *_: None,
                )
            )
            asyncio.run(
                controller._establish_bleak_client(
                    object(),
                    light.mac,
                    lambda *_: None,
                )
            )
        finally:
            controller_module.establish_connection = original

        self.assertEqual(cache_decisions, [True, True])

    def test_connect_advertisement_snapshot_records_fresh_service_info(self) -> None:
        controller, light = self._make_controller()
        now = controller.hass.loop.time()
        controller._last_service_info = (  # type: ignore[method-assign]
            lambda _address: types.SimpleNamespace(
                rssi=-67,
                time=now - 0.25,
                source="proxy-a",
                connectable=True,
            )
        )
        controller._current_connect_trace = {
            "address": light.mac,
            "started": now,
            "stages": {"candidate_selected": 0.0},
        }

        controller._mark_connect_advertisement_ready(light.mac)

        trace = controller._current_connect_trace
        self.assertIsNotNone(trace)
        self.assertEqual(
            trace["advertisement_at_connect"],
            {
                "age_seconds": 0.25,
                "rssi": -67,
                "source": "proxy-a",
                "connectable": True,
            },
        )
        self.assertEqual(trace["stages"]["advertisement_ready"], 0.0)

    def test_connect_attempt_diagnostics_include_cache_and_stages(self) -> None:
        controller, light = self._make_controller()
        controller._current_connect_trace = {
            "address": light.mac,
            "started": controller.hass.loop.time(),
            "requested_cache_mode": "without_cache",
            "requested_cache_reason": "cccd_recovery",
            "advertisement_at_selection": {
                "age_seconds": 0.4,
                "rssi": -70,
                "source": "proxy-a",
                "connectable": True,
            },
            "advertisement_at_connect": {
                "age_seconds": 0.1,
                "rssi": -68,
                "source": "proxy-a",
                "connectable": True,
            },
            "stages": {"candidate_selected": 0.0},
        }
        controller.hass.loop._time += 3
        controller._mark_connect_stage(light.mac, "gatt_services_ready")

        controller._record_connect_attempt(
            light.mac,
            "failed",
            15.0,
            "Proxy notification TimeoutError",
        )

        item = controller.diagnostic_state()["recent_connect_attempts"][-1]
        self.assertEqual(item["requested_cache_mode"], "without_cache")
        self.assertEqual(item["requested_cache_reason"], "cccd_recovery")
        self.assertEqual(item["advertisement_at_selection"]["age_seconds"], 0.4)
        self.assertEqual(item["advertisement_at_connect"]["age_seconds"], 0.1)
        self.assertEqual(item["stages"]["gatt_services_ready"], 3.0)
        self.assertEqual(item["result_stage"], "proxy_notification")
        self.assertEqual(
            controller.diagnostic_state()["connect_attempt_stats"],
            {
                "attempts_total": 1,
                "result_failed": 1,
                "cache_without_cache_attempts": 1,
                "cache_without_cache_failed": 1,
                "stage_proxy_notification": 1,
            },
        )

    def test_connector_rotates_after_one_gatt_attempt_with_multiple_candidates(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller.config.proxy_candidates.append("AA:BB:CC:44:55:66")
        calls: dict[str, object] = {}
        connected_client = types.SimpleNamespace(is_connected=True)

        async def fake_establish(
            _client_class: type,
            _device: object,
            _name: str,
            **kwargs: object,
        ) -> object:
            calls.update(kwargs)
            kwargs["amaran_client_created"](connected_client)
            return connected_client

        original = controller_module.establish_connection
        controller_module.establish_connection = fake_establish
        try:
            asyncio.run(
                controller._establish_bleak_client(
                    object(),
                    light.mac,
                    lambda *_: None,
                )
            )
        finally:
            controller_module.establish_connection = original

        self.assertEqual(calls["max_attempts"], 1)
        self.assertTrue(calls["use_services_cache"])
        self.assertEqual(
            calls["services"],
            [
                controller_module.MESH_PROVISIONING_SERVICE_UUID,
                controller_module.MESH_PROXY_SERVICE_UUID,
            ],
        )

    def test_connector_bypasses_service_cache_after_cccd_failure(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        calls: dict[str, object] = {}
        connected_client = types.SimpleNamespace(is_connected=True)
        controller._service_cache_bypass.add(light.mac)

        async def fake_establish(
            _client_class: type,
            _device: object,
            _name: str,
            **kwargs: object,
        ) -> object:
            calls.update(kwargs)
            kwargs["amaran_client_created"](connected_client)
            return connected_client

        original = controller_module.establish_connection
        controller_module.establish_connection = fake_establish
        try:
            asyncio.run(
                controller._establish_bleak_client(
                    object(),
                    light.mac,
                    lambda *_: None,
                )
            )
        finally:
            controller_module.establish_connection = original

        self.assertFalse(calls["use_services_cache"])
        self.assertIn(light.mac, controller._service_cache_bypass)
        self.assertIn(light.mac, controller._uncached_recovery_attempted)

    def test_gatt_connection_allows_connector_to_finish_its_retry_cycle(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        connected_client = types.SimpleNamespace(is_connected=True)
        completed = False

        async def fake_establish(
            _client_class: type,
            _device: object,
            _name: str,
            **kwargs: object,
        ) -> object:
            nonlocal completed
            kwargs["amaran_client_created"](connected_client)
            await asyncio.sleep(0.02)
            completed = True
            return connected_client

        original_establish = controller_module.establish_connection
        controller_module.establish_connection = fake_establish
        try:
            result = asyncio.run(
                controller._establish_bleak_client(
                    object(),
                    light.mac,
                    lambda *_: None,
                )
            )
        finally:
            controller_module.establish_connection = original_establish

        self.assertTrue(completed)
        self.assertIs(result, connected_client)
        self.assertNotIn(light.mac, controller._pending_clients)

    def test_candidate_establish_deadline_cancels_and_cleans_client(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        created: list[object] = []
        connector_cancelled = False

        class FakeClient:
            def __init__(self) -> None:
                self.is_connected = True
                self.disconnect_calls = 0
                created.append(self)

            async def disconnect(self) -> None:
                self.disconnect_calls += 1
                self.is_connected = False

        async def fake_establish(
            _client_class: type,
            _device: object,
            _name: str,
            **kwargs: object,
        ) -> object:
            nonlocal connector_cancelled
            client = FakeClient()
            kwargs["amaran_client_created"](client)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                connector_cancelled = True
                raise
            return client

        original_establish = controller_module.establish_connection
        original_deadline = controller_module.GATT_ESTABLISH_DEADLINE
        controller_module.establish_connection = fake_establish
        controller_module.GATT_ESTABLISH_DEADLINE = 0.01
        try:
            with self.assertRaisesRegex(
                TimeoutError,
                "GATT establish/service discovery exceeded",
            ):
                asyncio.run(
                    controller._establish_bleak_client(
                        object(),
                        light.mac,
                        lambda *_: None,
                    )
                )
        finally:
            controller_module.GATT_ESTABLISH_DEADLINE = original_deadline
            controller_module.establish_connection = original_establish

        self.assertTrue(connector_cancelled)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].disconnect_calls, 1)
        self.assertNotIn(light.mac, controller._pending_clients)

    def test_candidate_establish_deadline_uses_gatt_cooldown(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller._proxy_addresses = lambda: [  # type: ignore[method-assign]
            light.mac,
            "AA:BB:CC:44:55:66",
        ]
        error = TimeoutError(
            "Amaran BLE GATT establish/service discovery exceeded "
            f"{controller_module.GATT_ESTABLISH_DEADLINE:.0f}s for {light.mac}"
        )

        self.assertEqual(
            controller._candidate_cooldown_seconds(error),
            controller_module.GATT_FAILURE_COOLDOWN,
        )

    def test_cancelled_standard_connector_cleans_up_owned_client(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        created: list[object] = []

        class FakeClient:
            def __init__(self) -> None:
                self.is_connected = True
                self.disconnect_calls = 0
                created.append(self)

            async def disconnect(self) -> None:
                self.disconnect_calls += 1
                self.is_connected = False

        async def fake_establish(
            _client_class: type,
            _device: object,
            _name: str,
            **kwargs: object,
        ) -> object:
            client = FakeClient()
            kwargs["amaran_client_created"](client)
            await asyncio.sleep(60)
            return client

        async def run_test() -> None:
            task = asyncio.create_task(
                controller._establish_bleak_client(
                    object(),
                    light.mac,
                    lambda *_: None,
                )
            )
            while not created:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        original = controller_module.establish_connection
        controller_module.establish_connection = fake_establish
        try:
            asyncio.run(run_test())
        finally:
            controller_module.establish_connection = original

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].disconnect_calls, 1)
        self.assertNotIn(light.mac, controller._pending_clients)

    def test_cancelled_candidate_attempt_is_recorded(self) -> None:
        controller, light = self._make_controller()

        async def run_test() -> None:
            started = asyncio.Event()

            async def fake_order(_addresses: list[str]) -> list[str]:
                return [light.mac]

            async def fake_ensure_proxy(
                _address: str,
                *,
                force_scan: bool = False,
            ) -> object:
                self.assertFalse(force_scan)
                controller._set_connect_cache_decision(
                    light.mac,
                    use_services_cache=True,
                    reason="normal_cache",
                )
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            controller._connect_candidate_order = fake_order  # type: ignore[method-assign]
            controller._ensure_proxy = fake_ensure_proxy  # type: ignore[method-assign]
            task = asyncio.create_task(controller._connect_active_proxy_locked())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run_test())

        state = controller.diagnostic_state()
        attempt = state["recent_connect_attempts"][-1]
        self.assertEqual(attempt["address"], light.mac)
        self.assertEqual(attempt["result"], "cancelled")
        self.assertEqual(attempt["requested_cache_mode"], "with_cache")
        self.assertEqual(state["connect_attempt_stats"]["attempts_total"], 1)
        self.assertEqual(state["connect_attempt_stats"]["result_cancelled"], 1)

    def test_released_state_waits_for_background_gatt_cleanup(self) -> None:
        controller, _light = self._make_controller()

        async def run_test() -> None:
            blocker = asyncio.Event()
            cleanup = asyncio.create_task(blocker.wait())
            controller._candidate_cleanup_tasks.add(cleanup)
            controller._control_enabled = False

            self.assertEqual(controller.diagnostic_state()["state"], "releasing")
            blocker.set()
            self.assertTrue(
                await controller._wait_candidate_cleanup_tasks(timeout=1.0)
            )
            self.assertEqual(controller.diagnostic_state()["state"], "released")

        asyncio.run(run_test())

    def test_connector_and_official_autoconnect_use_one_candidate_at_a_time(self) -> None:
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        self.assertEqual(controller_module.CONNECT_MAX_ATTEMPTS, 1)
        self.assertEqual(controller_module.CONNECT_CANDIDATES_PER_ROUND, 1)

    def test_amaran_client_extends_esphome_service_discovery_timeout(self) -> None:
        self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        client = controller_module._AmaranBleakClient(object())

        asyncio.run(client.connect(timeout=20.0))

        self.assertEqual(
            client.connect_kwargs["timeout"],
            controller_module.GATT_CLIENT_CONNECT_TIMEOUT,
        )
        self.assertEqual(controller_module.GATT_CLIENT_CONNECT_TIMEOUT, 45.0)
        self.assertEqual(controller_module.GATT_ESTABLISH_DEADLINE, 40.0)

    def test_amaran_client_returns_completed_bleak_error_without_retry(self) -> None:
        self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        async def failing_connect(_client: object, **_kwargs: object) -> object:
            raise controller_module.BleakError("ESP_GATT_CONN_FAIL_ESTABLISH")

        original_connect = controller_module.BleakClientWithServiceCache.connect
        controller_module.BleakClientWithServiceCache.connect = failing_connect
        try:
            client = controller_module._AmaranBleakClient(object())
            with self.assertRaisesRegex(
                RuntimeError,
                "Single Amaran GATT attempt failed",
            ) as raised:
                asyncio.run(client.connect(timeout=20.0))
        finally:
            controller_module.BleakClientWithServiceCache.connect = original_connect

        self.assertIsInstance(raised.exception.__cause__, controller_module.BleakError)

    def test_new_connect_waits_for_previous_client_disposal(self) -> None:
        controller, _light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        async def run_test() -> None:
            blocker = asyncio.Event()
            cleanup = asyncio.create_task(blocker.wait())
            controller._candidate_cleanup_tasks.add(cleanup)
            original_timeout = controller_module.CLIENT_CLEANUP_TIMEOUT
            controller_module.CLIENT_CLEANUP_TIMEOUT = 0
            try:
                with self.assertRaisesRegex(RuntimeError, "still being disposed"):
                    await controller._connect_active_proxy_locked()
            finally:
                controller_module.CLIENT_CLEANUP_TIMEOUT = original_timeout
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

        asyncio.run(run_test())

    def test_delayed_disconnect_from_old_client_does_not_drop_new_session(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        old_client = object()
        new_client = object()
        controller._active_proxy_address = light.mac
        controller._sessions[light.mac] = controller_module._ProxySession(
            address=light.mac,
            client=new_client,
            proxy_data_in=object(),
            mesh_ready=True,
            connected_at=controller.hass.loop.time(),
        )
        controller._set_mesh_available(True)

        controller._handle_proxy_disconnect(light.mac, old_client)

        self.assertEqual(controller._active_proxy_address, light.mac)
        self.assertTrue(controller._mesh_available)
        self.assertIs(controller._sessions[light.mac].client, new_client)

    def test_refresh_all_statuses_collects_reports(self) -> None:
        controller, light = self._make_controller()
        telink_module = importlib.import_module("custom_components.amaran_ble.telink")
        status = telink_module.TelinkStatus(
            on=True,
            is_hs=False,
            intensity=500,
            cct_kelvin=4500,
            gm=0,
            hue=0,
            saturation=0,
            command_type=2,
        )

        async def fake_refresh_all() -> None:
            controller._handle_status(light.mesh_address, status)

        controller.async_refresh_all = fake_refresh_all  # type: ignore[method-assign]

        statuses = asyncio.run(controller.async_refresh_all_statuses(timeout=0.01))

        self.assertEqual(statuses[light.mesh_address], status)

    def test_refresh_all_statuses_does_not_expire_recent_status_after_burst_misses(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller._set_mesh_available(True)
        controller._light_available[light.mac] = True
        controller._last_status_seen[light.mesh_address] = controller.hass.loop.time()

        async def fake_refresh_all() -> None:
            return None

        controller.async_refresh_all = fake_refresh_all  # type: ignore[method-assign]

        for _ in range(controller_module.STATUS_MISS_LIMIT):
            statuses = asyncio.run(
                controller.async_refresh_all_statuses(timeout=0.01)
            )
            self.assertEqual(statuses, {})
            self.assertTrue(controller.is_light_available(light.mac))

    def test_refresh_all_statuses_marks_stale_missing_light_unavailable(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller._set_mesh_available(True)
        controller._light_available[light.mac] = True
        controller._last_status_seen[light.mesh_address] = (
            controller.hass.loop.time()
            - controller_module.LIGHT_STATUS_UNAVAILABLE_SECONDS
            - 1
        )

        async def fake_refresh_all() -> None:
            return None

        controller.async_refresh_all = fake_refresh_all  # type: ignore[method-assign]

        for _ in range(controller_module.STATUS_MISS_LIMIT - 1):
            statuses = asyncio.run(
                controller.async_refresh_all_statuses(timeout=0.01)
            )
            self.assertEqual(statuses, {})
            self.assertTrue(controller.is_light_available(light.mac))

        statuses = asyncio.run(controller.async_refresh_all_statuses(timeout=0.01))
        self.assertEqual(statuses, {})
        self.assertFalse(controller.is_light_available(light.mac))

    def test_compact_diagnostics_limit_recorder_histories(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        for index in range(controller_module.COMMAND_TRACE_HISTORY_SIZE):
            controller._recent_command_traces.append(
                {
                    "command_id": index,
                    "command": "set_brightness",
                    "mesh_address": f"0x{light.mesh_address:04X}",
                }
            )
        for index in range(12):
            controller._connect_attempt_history.append(
                {
                    "attempt_id": index,
                    "address": light.mac,
                    "result": "failed",
                    "detail": "test failure",
                }
            )

        full = controller.diagnostic_state()
        compact = controller.diagnostic_state(compact=True)

        self.assertEqual(
            len(full["recent_command_traces"]),
            controller_module.COMMAND_TRACE_HISTORY_SIZE,
        )
        self.assertEqual(len(full["recent_connect_attempts"]), 12)
        self.assertEqual(
            len(compact["recent_command_traces"]),
            controller_module.COMPACT_COMMAND_TRACE_HISTORY_SIZE,
        )
        self.assertEqual(
            len(compact["recent_connect_attempts"]),
            controller_module.COMPACT_CONNECT_ATTEMPT_HISTORY_SIZE,
        )


if __name__ == "__main__":
    unittest.main()
