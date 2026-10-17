"""Offline checks for controller availability behavior."""

from __future__ import annotations

import importlib
import asyncio
from types import MappingProxyType
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


def _install_homeassistant_stubs() -> None:
    """Install tiny Home Assistant module stubs for controller-only tests."""
    if "homeassistant" in sys.modules:
        return

    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    core = types.ModuleType("homeassistant.core")

    class FakeHassJob:
        def __init__(self, target: object) -> None:
            self.target = target

    def _missing(*_: object, **__: object) -> None:
        return None

    bluetooth.async_ble_device_from_address = _missing
    bluetooth.async_last_service_info = _missing
    bluetooth.BluetoothScanningMode = types.SimpleNamespace(
        ACTIVE="active",
        PASSIVE="passive",
    )
    core.HomeAssistant = object
    core.HassJob = FakeHassJob
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

    def test_mesh_available_waits_for_current_session_status(self) -> None:
        controller, light = self._make_controller()

        updates: list[bool] = []
        controller.async_track_availability(light.mac, updates.append)
        controller._set_mesh_available(True)

        self.assertFalse(controller.is_light_available(light.mac))
        self.assertEqual(updates, [])

    def test_previous_session_status_does_not_make_new_session_available(self) -> None:
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

        controller._set_mesh_available(True)
        controller._handle_status(light.mesh_address, status)
        self.assertTrue(controller.is_light_available(light.mac))

        controller._set_mesh_available(False)
        controller._set_mesh_available(True)

        self.assertFalse(controller.is_light_available(light.mac))
        self.assertEqual(controller.diagnostic_state()["status_seen_in_session"], [])

    def test_write_only_session_marks_active_proxy_available(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        session = controller_module._ProxySession(
            address=light.mac,
            client=types.SimpleNamespace(is_connected=True),
            proxy_data_in=object(),
            mesh_ready=True,
            status_notifications_supported=False,
        )
        controller._sessions[light.mac] = session
        controller._active_proxy_address = light.mac

        controller._set_mesh_available(True)

        self.assertTrue(controller.is_light_available(light.mac))

    def test_homeassistant_shutdown_job_releases_retained_proxy(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        jobs: list[object] = []

        class FakeHassJob:
            def __init__(self, target: object) -> None:
                self.target = target

        controller_module.ha_core.HassJob = FakeHassJob

        def add_shutdown_job(job: object) -> object:
            jobs.append(job)

            def unsubscribe() -> None:
                jobs.remove(job)

            return unsubscribe

        class FakeClient:
            def __init__(self) -> None:
                self.is_connected = True

            async def disconnect(self) -> None:
                self.is_connected = False

            async def stop_notify(self, _characteristic: object) -> None:
                raise AssertionError("shutdown should use direct GATT disconnect")

        controller.hass.async_add_shutdown_job = add_shutdown_job
        controller._register_advertisement_callback = lambda: None  # type: ignore[method-assign]
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]
        client = FakeClient()
        controller._sessions[light.mac] = controller_module._ProxySession(
            address=light.mac,
            client=client,
            proxy_data_out=object(),
            notify_started=True,
            mesh_ready=True,
        )
        controller._active_proxy_address = light.mac

        asyncio.run(controller.async_start())
        self.assertEqual(len(jobs), 1)
        shutdown_job = jobs.pop()
        controller._shutdown_unsubscribe = None
        asyncio.run(shutdown_job.target())

        self.assertFalse(client.is_connected)
        self.assertEqual(controller._sessions, {})
        self.assertTrue(controller._stopping)

    def test_stop_event_fallback_does_not_unsubscribe_twice(self) -> None:
        controller, _light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        listeners: dict[str, object] = {}
        unsubscribe_calls = 0

        class FakeBus:
            def async_listen_once(self, event: str, callback: object):
                listeners[event] = callback

                def unsubscribe() -> None:
                    nonlocal unsubscribe_calls
                    unsubscribe_calls += 1
                    listeners.pop(event, None)

                return unsubscribe

        controller.hass.bus = FakeBus()
        controller._register_advertisement_callback = lambda: None  # type: ignore[method-assign]
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]

        asyncio.run(controller.async_start())
        stop_callback = listeners.pop(controller_module.EVENT_HOMEASSISTANT_STOP)
        controller._shutdown_unsubscribe = None
        asyncio.run(stop_callback(object()))

        self.assertEqual(unsubscribe_calls, 0)
        self.assertTrue(controller._stopping)

    def test_stop_drains_candidate_cleanup_instead_of_cancelling_it(self) -> None:
        controller, _light = self._make_controller()

        async def scenario() -> bool:
            completed = False

            async def cleanup() -> None:
                nonlocal completed
                await asyncio.sleep(0)
                completed = True

            task = asyncio.create_task(cleanup())
            controller._candidate_cleanup_tasks.add(task)
            task.add_done_callback(controller._candidate_cleanup_tasks.discard)
            await controller.async_stop(cleanup_timeout=1.0)
            return completed and not task.cancelled()

        self.assertTrue(asyncio.run(scenario()))

    def test_filter_addresses_match_official_sdk_whitelist(self) -> None:
        controller, _light = self._make_controller()

        self.assertEqual(controller._filter_addresses(), [0x0001, 0xFFFF])

    def test_proxy_login_waits_for_each_official_filter_status(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        async def scenario() -> tuple[bool, list[str]]:
            session = controller_module._ProxySession(
                address=light.mac,
                client=object(),
                proxy_data_in=object(),
                filter_status_event=asyncio.Event(),
            )
            labels: list[str] = []
            set_filter_written = asyncio.Event()
            add_addresses_written = asyncio.Event()

            async def fake_write(
                _client: object,
                _characteristic: object,
                _data: bytes,
                label: str,
            ) -> None:
                labels.append(label)
                if label == "proxy filter type":
                    set_filter_written.set()
                elif label == "proxy add addresses":
                    add_addresses_written.set()

            controller._write_proxy_data = fake_write  # type: ignore[method-assign]
            task = asyncio.create_task(
                controller._initialize_proxy_login(
                    session,
                    wait_for_filter_status=True,
                )
            )
            await set_filter_written.wait()
            await asyncio.sleep(0)
            self.assertFalse(add_addresses_written.is_set())

            session.filter_status_event.set()
            await add_addresses_written.wait()
            self.assertFalse(session.filter_status_event.is_set())
            session.filter_status_event.set()
            return await task, labels

        original_delay = controller_module.PROXY_READY_DELAY
        controller_module.PROXY_READY_DELAY = 0
        try:
            confirmed, labels = asyncio.run(scenario())
        finally:
            controller_module.PROXY_READY_DELAY = original_delay

        self.assertTrue(confirmed)
        self.assertEqual(
            labels,
            [
                "secure network beacon",
                "proxy filter type",
                "proxy add addresses",
            ],
        )

    def test_proxy_login_finishes_writes_when_first_status_times_out(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        async def scenario() -> tuple[bool, list[str]]:
            session = controller_module._ProxySession(
                address=light.mac,
                client=object(),
                proxy_data_in=object(),
                filter_status_event=asyncio.Event(),
            )
            labels: list[str] = []

            async def fake_write(
                _client: object,
                _characteristic: object,
                _data: bytes,
                label: str,
            ) -> None:
                labels.append(label)

            controller._write_proxy_data = fake_write  # type: ignore[method-assign]
            confirmed = await controller._initialize_proxy_login(
                session,
                wait_for_filter_status=True,
            )
            return confirmed, labels

        original_delay = controller_module.PROXY_READY_DELAY
        original_timeout = controller_module.FILTER_STATUS_TIMEOUT
        controller_module.PROXY_READY_DELAY = 0
        controller_module.FILTER_STATUS_TIMEOUT = 0.01
        try:
            confirmed, labels = asyncio.run(scenario())
        finally:
            controller_module.PROXY_READY_DELAY = original_delay
            controller_module.FILTER_STATUS_TIMEOUT = original_timeout

        self.assertFalse(confirmed)
        self.assertEqual(
            labels,
            [
                "secure network beacon",
                "proxy filter type",
                "proxy add addresses",
            ],
        )

    def test_proxy_login_write_only_fallback_sends_complete_filter(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        async def scenario() -> tuple[bool, list[str]]:
            session = controller_module._ProxySession(
                address=light.mac,
                client=object(),
                proxy_data_in=object(),
            )
            labels: list[str] = []

            async def fake_write(
                _client: object,
                _characteristic: object,
                _data: bytes,
                label: str,
            ) -> None:
                labels.append(label)

            controller._write_proxy_data = fake_write  # type: ignore[method-assign]
            confirmed = await controller._initialize_proxy_login(
                session,
                wait_for_filter_status=False,
            )
            return confirmed, labels

        original_delay = controller_module.PROXY_READY_DELAY
        controller_module.PROXY_READY_DELAY = 0
        try:
            confirmed, labels = asyncio.run(scenario())
        finally:
            controller_module.PROXY_READY_DELAY = original_delay

        self.assertFalse(confirmed)
        self.assertEqual(
            labels,
            [
                "secure network beacon",
                "proxy filter type",
                "proxy add addresses",
            ],
        )

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
            resync_advertisement: bool = False,
        ):
            self.assertFalse(resync_advertisement)
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

    def test_cached_preopen_failure_preserves_medium_cached_path(self) -> None:
        controller, light = self._make_controller()

        async def fake_order(_addresses: list[str]) -> list[str]:
            return [light.mac]

        async def fake_ensure(
            address: str,
            *,
            resync_advertisement: bool = False,
        ) -> object:
            self.assertEqual(address, light.mac)
            self.assertFalse(resync_advertisement)
            controller._set_connect_cache_decision(
                address,
                use_services_cache=True,
                reason="normal_cache",
            )
            raise RuntimeError("ESP_GATT_CONN_FAIL_ESTABLISH")

        controller._connect_candidate_order = fake_order  # type: ignore[method-assign]
        controller._ensure_proxy = fake_ensure  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, light.mac):
            asyncio.run(controller._connect_active_proxy_locked())

        self.assertNotIn(light.mac, controller._service_cache_bypass)
        self.assertNotIn(
            "gatt_connect_uncached_recoveries_scheduled",
            controller.diagnostic_state()["connect_attempt_stats"],
        )

    def test_control_toggle_preserves_candidate_failure_cooldown(self) -> None:
        controller, light = self._make_controller()
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]
        controller._ignored_stored_proxy = light.mac
        controller._candidate_cooldown_until[light.mac] = 999
        controller._notification_backoff_until[light.mac] = 999
        controller._service_cache_bypass.add(light.mac)
        controller._cccd_reconnect_pending.add(light.mac)
        controller._cached_cccd_retry_attempted.add(light.mac)
        controller._authorization_recovery_attempted.add(light.mac)
        controller._ownership_attempted_candidates.add(light.mac)
        controller._ownership_candidate_attempted_at[light.mac] = 100.0
        controller._authorization_reconnect_after = 999
        controller._connect_failure_count = 4

        asyncio.run(controller.async_set_control_enabled(False))
        self.assertIsNone(controller._ignored_stored_proxy)
        self.assertEqual(controller._service_cache_bypass, set())
        self.assertEqual(controller._cccd_reconnect_pending, set())
        self.assertEqual(controller._cached_cccd_retry_attempted, set())
        self.assertEqual(controller._authorization_recovery_attempted, set())
        self.assertEqual(controller._ownership_attempted_candidates, set())
        self.assertEqual(controller._ownership_candidate_attempted_at, {})
        self.assertEqual(controller._authorization_reconnect_after, 0.0)

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

    def test_control_off_uses_official_direct_gatt_release(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]

        class FakeClient:
            def __init__(self) -> None:
                self.is_connected = True

            async def disconnect(self) -> None:
                self.is_connected = False

            async def stop_notify(self, _characteristic: object) -> None:
                raise AssertionError(
                    "master control release must not disable CCCD first"
                )

        client = FakeClient()
        controller._sessions[light.mac] = controller_module._ProxySession(
            address=light.mac,
            client=client,
            proxy_data_out=object(),
            notify_started=True,
            mesh_ready=True,
            connected_at=controller.hass.loop.time() - 40,
            stable=True,
        )
        controller._active_proxy_address = light.mac

        asyncio.run(controller.async_set_control_enabled(False))

        self.assertFalse(client.is_connected)
        self.assertEqual(controller._sessions, {})
        self.assertIsNone(controller._active_proxy_address)

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

        async def fake_reset(address: str, *, fast: bool = False) -> None:
            self.assertTrue(fast)
            controller._sessions.pop(address, None)
            controller._active_proxy_address = None

        controller._reset_proxy = fake_reset  # type: ignore[method-assign]

        asyncio.run(controller.async_set_control_enabled(False))

        self.assertEqual(controller._release_guard_until, 145.0)
        self.assertEqual(controller._last_release_uptime, 5.0)
        self.assertTrue(controller._last_release_was_short)

        controller.hass.loop._time += 4
        asyncio.run(controller.async_set_control_enabled(True))
        controller._wake_for_advertisement(light.mac)

        self.assertEqual(controller._next_connect_after, 145.0)
        state = controller.diagnostic_state()
        self.assertEqual(state["state"], "release_cooldown")
        self.assertEqual(state["release_guard_seconds"], 41.0)

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

        async def fake_reset(address: str, *, fast: bool = False) -> None:
            self.assertTrue(fast)
            controller._sessions.pop(address, None)
            controller._active_proxy_address = None

        controller._reset_proxy = fake_reset  # type: ignore[method-assign]

        asyncio.run(controller.async_set_control_enabled(False))
        controller.hass.loop._time += 3
        asyncio.run(controller.async_set_control_enabled(True))

        self.assertEqual(controller._release_guard_until, 130.0)
        self.assertEqual(controller._next_connect_after, 130.0)
        self.assertFalse(controller._last_release_was_short)
        self.assertEqual(
            controller.diagnostic_state()["release_guard_seconds"],
            27.0,
        )

    def test_release_during_connection_attempt_uses_short_guard(self) -> None:
        controller, light = self._make_controller()
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]
        controller._pending_clients[light.mac] = object()

        asyncio.run(controller.async_set_control_enabled(False))

        self.assertEqual(controller._release_guard_until, 145.0)
        self.assertIsNone(controller._last_release_uptime)
        self.assertTrue(controller._last_release_was_short)
        self.assertTrue(controller._last_release_in_progress)

    def test_expired_short_release_guard_does_not_delay_reenable(self) -> None:
        controller, light = self._make_controller()
        controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]
        controller._pending_clients[light.mac] = object()

        asyncio.run(controller.async_set_control_enabled(False))
        controller.hass.loop._time += 46
        asyncio.run(controller.async_set_control_enabled(True))

        self.assertEqual(controller._release_guard_remaining(), 0.0)
        self.assertEqual(controller._next_connect_after, 146.0)
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

        error = TimeoutError("Timeout waiting for connect response")
        controller._advance_candidate_cursor(light.mac, error)

        self.assertEqual(controller._ignored_stored_proxy, light.mac)
        self.assertIn(light.mac, controller._candidate_cooldown_until)
        self.assertIn(light.mac, controller._round_failed_candidates)

    def test_authorization_failure_rotates_from_last_successful_proxy(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._candidate_advertisement_seen_at.update(
            {light.mac: 100.0, other: 100.0}
        )
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("Insufficient authorization (8)"),
        )

        self.assertNotIn(light.mac, controller._authorization_recovery_attempted)
        self.assertEqual(controller._authorization_reconnect_after, 0.0)
        self.assertEqual(controller._ignored_stored_proxy, light.mac)
        self.assertIn(light.mac, controller._round_failed_candidates)
        self.assertEqual(controller._candidate_cursor, 1)

    def test_authorization_failure_retries_only_reachable_candidate_once(self) -> None:
        controller, light = self._make_controller()
        controller.config.proxy_candidates.extend(
            [
                "AA:BB:CC:44:55:66",
                "AA:BB:CC:77:88:99",
                "AA:BB:CC:AA:BB:CC",
            ]
        )
        controller._candidate_advertisement_seen_at[light.mac] = 100.0

        self.assertEqual(controller._candidate_round_members(), {light.mac})

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("Insufficient authorization (8)"),
        )

        self.assertIn(light.mac, controller._authorization_recovery_attempted)
        self.assertEqual(controller._authorization_reconnect_after, 108.0)
        self.assertEqual(controller._next_connect_after, 108.0)
        self.assertNotIn(light.mac, controller._round_failed_candidates)
        self.assertEqual(controller._candidate_cursor, 0)
        self.assertEqual(
            controller._connect_attempt_stats[
                "single_candidate_gatt_authorization_reconnects_scheduled"
            ],
            1,
        )

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("Insufficient authorization (8)"),
        )

        self.assertIn(light.mac, controller._round_failed_candidates)
        self.assertEqual(
            controller._connect_attempt_stats[
                "single_candidate_gatt_authorization_reconnects_scheduled"
            ],
            1,
        )

    def test_authorization_failure_does_not_retry_after_alternative_was_attempted(
        self,
    ) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._candidate_advertisement_seen_at[light.mac] = 100.0
        controller._ownership_attempted_candidates.update((light.mac, other))

        self.assertEqual(controller._candidate_round_members(), {light.mac})

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError("Insufficient authorization (8)"),
        )

        self.assertNotIn(light.mac, controller._authorization_recovery_attempted)
        self.assertEqual(controller._authorization_reconnect_after, 0.0)
        self.assertIn(light.mac, controller._round_failed_candidates)
        self.assertNotIn(
            "single_candidate_gatt_authorization_reconnects_scheduled",
            controller._connect_attempt_stats,
        )

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
        self.assertIn(light.mac, controller._candidate_cooldown_until)
        self.assertIn(light.mac, controller._round_failed_candidates)

    def test_baseline_connect_order_keeps_health_ranking(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        controller._has_fresh_ble_device = (  # type: ignore[method-assign]
            lambda _address, **_kwargs: True
        )
        controller._record_candidate_health_outcome(light.mac, "gatt_connect")
        controller._record_candidate_health_outcome(other, "stable")

        ordered = asyncio.run(
            controller._connect_candidate_order([light.mac, other])
        )

        self.assertEqual(ordered, [other])

    def test_baseline_connect_order_skips_missing_last_stable_proxy(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        controller._has_fresh_ble_device = (  # type: ignore[method-assign]
            lambda address, **_kwargs: address == other
        )

        ordered = asyncio.run(
            controller._connect_candidate_order([light.mac, other])
        )

        self.assertEqual(ordered, [other])

    def test_followup_attempt_uses_health_order_after_round_preference(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )
        controller._preferred_ownership_first_attempt_pending = False
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        controller._has_fresh_ble_device = (  # type: ignore[method-assign]
            lambda _address, **_kwargs: True
        )
        controller._record_candidate_health_outcome(light.mac, "gatt_connect")
        controller._record_candidate_health_outcome(other, "stable")

        ordered = asyncio.run(
            controller._connect_candidate_order([light.mac, other])
        )

        self.assertEqual(ordered[0], other)

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

    def test_missing_last_stable_proxy_yields_to_next_candidate(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )
        controller._has_fresh_ble_device = (  # type: ignore[method-assign]
            lambda _address, **_kwargs: False
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

    def test_single_reachable_gatt_failure_uses_advertisement_resync_delay(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller.config.proxy_candidates.extend(
            (
                "AA:BB:CC:44:55:66",
                "AA:BB:CC:44:55:77",
                "AA:BB:CC:44:55:88",
            )
        )
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda address: object() if address == light.mac else None
        )
        controller._candidate_failure_reason[light.mac] = "gatt_connect"
        controller._round_failed_candidates.add(light.mac)

        self.assertEqual(controller._candidate_round_members(), {light.mac})
        self.assertTrue(controller._full_candidate_round_failed())
        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.SINGLE_CANDIDATE_ADVERTISEMENT_RESYNC_RETRY_DELAY,
        )
        self.assertEqual(
            controller_module.SINGLE_CANDIDATE_ADVERTISEMENT_RESYNC_RETRY_DELAY,
            8,
        )

    def test_deferred_multi_candidate_round_repeats_full_radio_quiet_window(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        visible = {light.mac, other}
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda address: object() if address in visible else None
        )
        controller._candidate_probe_after_round[other] = (
            controller._candidate_round + 1
        )
        controller._candidate_failure_reason[light.mac] = "gatt_connect"
        controller._round_failed_candidates.add(light.mac)
        controller._consecutive_preopen_failures = 1

        self.assertEqual(controller._candidate_round_members(), {light.mac})
        self.assertEqual(
            controller._candidate_round_members(
                include_deferred=True,
                include_rssi_fallback=True,
            ),
            visible,
        )
        self.assertTrue(controller._full_candidate_round_failed())
        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.DEFERRED_MULTI_CANDIDATE_RECOVERY_DELAY,
        )
        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.DEFERRED_MULTI_CANDIDATE_RECOVERY_DELAY,
        )
        controller._consecutive_preopen_failures = 5
        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.DEFERRED_MULTI_CANDIDATE_RECOVERY_DELAY,
        )
        self.assertEqual(
            controller_module.DEFERRED_MULTI_CANDIDATE_RECOVERY_DELAY,
            30,
        )
        self.assertEqual(
            controller._connect_attempt_stats[
                "deferred_multi_candidate_quiet_windows"
            ],
            2,
        )

    def test_recent_ownership_alternative_survives_advertisement_history_gap(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda address: object() if address == light.mac else None
        )
        now = controller.hass.loop.time()
        controller._ownership_attempted_candidates.update((light.mac, other))
        controller._ownership_candidate_attempted_at.update(
            {light.mac: now, other: now - 20.0}
        )
        controller._candidate_probe_after_round[other] = (
            controller._candidate_round + 1
        )
        controller._candidate_failure_reason[light.mac] = "gatt_connect"
        controller._round_failed_candidates.add(light.mac)
        controller._consecutive_preopen_failures = 1

        self.assertEqual(controller._candidate_round_members(), {light.mac})
        self.assertEqual(
            controller._candidate_round_members(
                include_deferred=True,
                include_rssi_fallback=True,
            ),
            {light.mac},
        )
        self.assertTrue(controller._full_candidate_round_failed())
        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.SINGLE_CANDIDATE_ADVERTISEMENT_RESYNC_RETRY_DELAY,
        )
        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.SINGLE_CANDIDATE_ADVERTISEMENT_RESYNC_RETRY_DELAY,
        )
        self.assertNotIn(
            "ownership_evidence_quiet_windows",
            controller._connect_attempt_stats,
        )

    def test_expired_ownership_alternative_keeps_true_one_survivor_delay(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda address: object() if address == light.mac else None
        )
        now = controller.hass.loop.time()
        controller._ownership_attempted_candidates.update((light.mac, other))
        controller._ownership_candidate_attempted_at.update(
            {
                light.mac: now,
                other: now
                - controller_module.OWNERSHIP_ALTERNATIVE_EVIDENCE_SECONDS
                - 1.0,
            }
        )
        controller._candidate_failure_reason[light.mac] = "gatt_connect"
        controller._round_failed_candidates.add(light.mac)
        controller._consecutive_preopen_failures = 1

        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.SINGLE_CANDIDATE_ADVERTISEMENT_RESYNC_RETRY_DELAY,
        )

    def test_single_candidate_failure_storm_uses_full_radio_recovery(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller.config.proxy_candidates.extend(
            (
                "AA:BB:CC:44:55:66",
                "AA:BB:CC:44:55:77",
                "AA:BB:CC:44:55:88",
            )
        )
        # HA temporarily has no cached service info after advertisement history
        # is cleared, while only the candidate already attempted was reachable.
        controller._get_ble_device = lambda _address: None  # type: ignore[method-assign]
        controller._candidate_advertisement_seen_at[light.mac] = (
            controller.hass.loop.time()
        )
        controller._candidate_failure_reason[light.mac] = "gatt_connect"
        controller._round_failed_candidates.add(light.mac)
        controller._consecutive_preopen_failures = 2

        self.assertEqual(controller._candidate_round_members(), {light.mac})
        self.assertTrue(controller._full_candidate_round_failed())
        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.PREOPEN_STORM_RECOVERY_DELAY,
        )
        self.assertEqual(controller_module.PREOPEN_STORM_RECOVERY_DELAY, 30)
        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.PREOPEN_STORM_RECOVERY_DELAY,
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
        self.assertEqual(
            controller._maintain_retry_delay("ESP_GATT_CONN_CONN_CANCEL"),
            controller_module.PREFERRED_PROXY_GATT_RECOVERY_DELAY,
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

    def test_offline_fixtures_do_not_hold_reachable_candidate_round_open(
        self,
    ) -> None:
        controller, light = self._make_controller()
        online = "AA:BB:CC:44:55:66"
        offline_one = "AA:BB:CC:44:55:77"
        offline_two = "AA:BB:CC:44:55:88"
        controller.config.proxy_candidates.extend(
            (online, offline_one, offline_two)
        )
        visible = {light.mac, online}
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda address: object() if address in visible else None
        )
        controller._round_failed_candidates.update(visible)
        for address in (*visible, offline_one):
            controller._candidate_failure_reason[address] = "gatt_connect"
            controller._candidate_cooldown_until[address] = 999

        self.assertEqual(controller._candidate_round_members(), visible)
        self.assertTrue(controller._full_candidate_round_failed())
        self.assertEqual(
            controller._maintain_retry_delay("RuntimeError: no candidates"),
            12,
        )

        controller._restart_candidate_round()
        ordered = asyncio.run(
            controller._connect_candidate_order(controller._proxy_addresses())
        )

        self.assertEqual(controller._round_failed_candidates, set())
        self.assertEqual(len(ordered), 1)
        self.assertIn(ordered[0], visible)
        self.assertTrue(
            visible.isdisjoint(controller._candidate_cooldown_until)
        )
        self.assertIn(offline_one, controller._candidate_cooldown_until)

    def test_new_candidate_round_retries_last_stable_proxy_first(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType({"last_successful_proxy": light.mac})
        )
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        controller._candidate_cursor = 1
        controller._connect_failure_count = 2
        controller._ignored_stored_proxy = light.mac
        controller._round_failed_candidates.update((light.mac, other))
        controller._candidate_cooldown_until[light.mac] = 999
        controller._candidate_cooldown_until[other] = 999
        controller._candidate_failure_reason[light.mac] = "gatt_connect"
        controller._candidate_failure_reason[other] = "gatt_connect"
        controller._record_candidate_health_outcome(light.mac, "gatt_connect")
        controller._record_candidate_health_outcome(other, "gatt_connect")
        controller._preferred_ownership_first_attempt_pending = False

        controller._restart_candidate_round()

        self.assertEqual(controller._connect_failure_count, 0)
        self.assertEqual(controller._round_failed_candidates, set())
        self.assertIsNone(controller._ignored_stored_proxy)
        self.assertEqual(controller._candidate_cursor, 0)
        self.assertNotIn(light.mac, controller._candidate_cooldown_until)
        self.assertNotIn(other, controller._candidate_cooldown_until)
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
        self.assertEqual(
            controller.diagnostic_state()["connect_attempt_stats"][
                "reachable_preopen_cooldown_overrides"
            ],
            1,
        )

    def test_new_candidate_round_preserves_post_open_cooldown(self) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
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

    def test_repeated_nonpreferred_preopen_failure_skips_one_round(self) -> None:
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller, preferred = self._make_controller()
        fallback = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(fallback)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType(
                {"last_successful_proxy": preferred.mac}
            )
        )
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda _address: object()
        )
        controller._record_candidate_health_outcome(
            fallback,
            "gatt_connect",
        )

        original_enabled = controller_module.PREOPEN_FALLBACK_DEFERRAL_ENABLED
        controller_module.PREOPEN_FALLBACK_DEFERRAL_ENABLED = True
        try:
            controller._schedule_preopen_candidate_reprobe(
                fallback,
                failure_reason="gatt_connect",
            )

            self.assertNotIn(fallback, controller._candidate_probe_after_round)

            controller._record_candidate_health_outcome(
                fallback,
                "gatt_connect",
            )
            controller._schedule_preopen_candidate_reprobe(
                fallback,
                failure_reason="gatt_connect",
            )
        finally:
            controller_module.PREOPEN_FALLBACK_DEFERRAL_ENABLED = original_enabled

        self.assertEqual(controller._candidate_probe_after_round[fallback], 3)
        self.assertEqual(
            controller._candidate_round_members(),
            {preferred.mac},
        )

        controller._restart_candidate_round()

        self.assertEqual(controller._candidate_round, 2)
        self.assertEqual(
            controller._candidate_round_members(),
            {preferred.mac},
        )
        controller._restart_candidate_round()
        self.assertEqual(controller._candidate_round, 3)
        self.assertEqual(
            controller._candidate_round_members(),
            {preferred.mac, fallback},
        )
        stats = controller.diagnostic_state()["connect_attempt_stats"]
        self.assertEqual(stats["preopen_fallback_round_deferrals"], 1)
        self.assertEqual(stats["preopen_fallback_reprobes_released"], 1)

    def test_only_advertising_deferred_candidate_is_immediate_fallback(
        self,
    ) -> None:
        controller, preferred = self._make_controller()
        fallback = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(fallback)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType(
                {"last_successful_proxy": preferred.mac}
            )
        )
        controller._candidate_probe_after_round[fallback] = 3
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda address: object() if address == fallback else None
        )

        self.assertEqual(controller._candidate_round_members(), {fallback})
        self.assertEqual(
            asyncio.run(
                controller._connect_candidate_order(
                    controller._proxy_addresses()
                )
            ),
            [fallback],
        )

    def test_failed_preferred_is_valid_round_evidence_for_deferral(
        self,
    ) -> None:
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller, preferred = self._make_controller()
        fallback = "AA:BB:CC:44:55:66"
        peer = "AA:BB:CC:77:88:99"
        controller.config.proxy_candidates.extend((fallback, peer))
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType(
                {"last_successful_proxy": preferred.mac}
            )
        )
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda address: object() if address in {fallback, peer} else None
        )
        controller._round_failed_candidates.add(preferred.mac)
        controller._record_candidate_health_outcome(fallback, "gatt_connect")
        controller._record_candidate_health_outcome(fallback, "gatt_connect")

        original_enabled = controller_module.PREOPEN_FALLBACK_DEFERRAL_ENABLED
        controller_module.PREOPEN_FALLBACK_DEFERRAL_ENABLED = True
        try:
            controller._schedule_preopen_candidate_reprobe(
                fallback,
                failure_reason="gatt_connect",
            )
        finally:
            controller_module.PREOPEN_FALLBACK_DEFERRAL_ENABLED = original_enabled

        self.assertEqual(controller._candidate_probe_after_round[fallback], 3)
        self.assertEqual(
            controller._candidate_round_members(),
            {peer},
        )
        stats = controller.diagnostic_state()["connect_attempt_stats"]
        self.assertEqual(stats["preopen_fallback_round_deferrals"], 1)
        self.assertEqual(
            stats["preopen_fallback_deferrals_after_preferred_attempt"],
            1,
        )

    def test_preopen_round_deferral_is_enabled_by_default(self) -> None:
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller, preferred = self._make_controller()
        fallback = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(fallback)
        controller._entry = types.SimpleNamespace(
            options=MappingProxyType(
                {"last_successful_proxy": preferred.mac}
            )
        )
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda _address: object()
        )
        controller._record_candidate_health_outcome(fallback, "gatt_connect")
        controller._record_candidate_health_outcome(fallback, "gatt_connect")

        controller._schedule_preopen_candidate_reprobe(
            fallback,
            failure_reason="gatt_connect",
        )

        self.assertEqual(
            controller._candidate_probe_after_round[fallback],
            controller._candidate_round
            + controller_module.PREOPEN_FALLBACK_SKIP_ROUNDS
            + 1,
        )
        self.assertEqual(
            controller.diagnostic_state()["connect_attempt_stats"][
                "preopen_fallback_round_deferrals"
            ],
            1,
        )

    def test_bounded_rssi_tier_releases_full_candidate_fallback(self) -> None:
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        original_enabled = controller_module.RSSI_FOCUS_ENABLED
        controller_module.RSSI_FOCUS_ENABLED = True
        self.addCleanup(
            setattr,
            controller_module,
            "RSSI_FOCUS_ENABLED",
            original_enabled,
        )
        controller, strong = self._make_controller()
        weak = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(weak)
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda _address: object()
        )
        controller._candidate_rssi = (  # type: ignore[method-assign]
            lambda address: -65 if address == strong.mac else -82
        )
        controller._record_candidate_health_outcome(weak, "stable")

        self.assertEqual(controller._candidate_round_members(), {strong.mac})
        self.assertEqual(
            asyncio.run(
                controller._connect_candidate_order([strong.mac, weak])
            ),
            [strong.mac],
        )

        controller._round_failed_candidates.add(strong.mac)
        controller._restart_candidate_round()
        self.assertEqual(controller._rssi_focus_failed_rounds, 1)
        self.assertEqual(controller._candidate_round_members(), {strong.mac})
        self.assertEqual(
            asyncio.run(
                controller._connect_candidate_order([strong.mac, weak])
            ),
            [strong.mac],
        )

        controller._round_failed_candidates.add(strong.mac)
        controller._restart_candidate_round()
        self.assertEqual(controller._rssi_focus_failed_rounds, 2)
        self.assertEqual(
            controller._candidate_round_members(),
            {strong.mac, weak},
        )
        self.assertEqual(
            asyncio.run(
                controller._connect_candidate_order([strong.mac, weak])
            ),
            [weak],
        )

        controller._round_failed_candidates.update((strong.mac, weak))
        controller._restart_candidate_round()
        self.assertEqual(controller._rssi_focus_failed_rounds, 0)
        self.assertEqual(controller._candidate_round_members(), {strong.mac})
        stats = controller.diagnostic_state()["connect_attempt_stats"]
        self.assertEqual(stats["rssi_focus_failed_rounds"], 2)
        self.assertEqual(stats["rssi_full_fallback_rounds"], 1)
        self.assertEqual(stats["candidate_round_member_filters"], 2)

    def test_disabled_rssi_focus_keeps_all_fresh_visible_candidates(self) -> None:
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller, strong = self._make_controller()
        weak = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(weak)
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda _address: object()
        )
        controller._candidate_rssi = (  # type: ignore[method-assign]
            lambda address: -65 if address == strong.mac else -92
        )

        self.assertFalse(controller_module.RSSI_FOCUS_ENABLED)
        self.assertEqual(
            controller._candidate_round_members(),
            {strong.mac, weak},
        )

    def test_rssi_tier_does_not_split_similar_candidates(self) -> None:
        controller, first = self._make_controller()
        second = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(second)
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda _address: object()
        )
        controller._candidate_rssi = (  # type: ignore[method-assign]
            lambda address: -65 if address == first.mac else -70
        )

        self.assertEqual(
            controller._candidate_round_members(),
            {first.mac, second},
        )

    def test_rssi_tier_does_not_filter_without_fresh_advertisements(self) -> None:
        controller, first = self._make_controller()
        second = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(second)
        controller._get_ble_device = lambda _address: None  # type: ignore[method-assign]
        controller._candidate_rssi = (  # type: ignore[method-assign]
            lambda address: -65 if address == first.mac else -82
        )

        self.assertEqual(
            controller._candidate_round_members(),
            {first.mac, second},
        )

    def test_new_ownership_cycle_clears_candidate_round_deferral(self) -> None:
        controller, light = self._make_controller()
        controller._candidate_probe_after_round[light.mac] = 3
        controller._preferred_ownership_first_attempt_pending = False

        controller._reset_connection_recovery_state()

        self.assertEqual(controller._candidate_probe_after_round, {})
        self.assertTrue(controller._preferred_ownership_first_attempt_pending)

    def test_post_open_quarantine_does_not_hold_other_candidate_round_open(
        self,
    ) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        controller._candidate_post_open_quarantine_until[light.mac] = (
            controller.hass.loop.time() + 180
        )
        controller._candidate_cooldown_until[light.mac] = (
            controller.hass.loop.time() + 180
        )
        controller._round_failed_candidates.add(other)

        self.assertEqual(controller._candidate_round_members(), {other})
        self.assertTrue(controller._full_candidate_round_failed())

        controller._restart_candidate_round()

        self.assertIn(
            light.mac,
            controller._candidate_post_open_quarantine_until,
        )
        self.assertIn(light.mac, controller._candidate_cooldown_until)

    def test_expired_post_open_quarantine_does_not_preserve_new_gatt_cooldown(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        controller._candidate_post_open_quarantine_until[light.mac] = (
            controller.hass.loop.time() - 1
        )
        controller._candidate_cooldown_until[light.mac] = 999
        controller._candidate_failure_reason[light.mac] = "gatt_connect"
        controller._round_failed_candidates.add(light.mac)

        controller._restart_candidate_round()

        self.assertNotIn(
            light.mac,
            controller._candidate_post_open_quarantine_until,
        )
        self.assertNotIn(light.mac, controller._candidate_cooldown_until)

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
        controller._consecutive_preopen_failures = 8
        self.assertEqual(
            controller._maintain_retry_delay(),
            controller_module.PREOPEN_STORM_RECOVERY_DELAY,
        )
    def test_control_cycle_reset_clears_preopen_failure_streak(self) -> None:
        controller, light = self._make_controller()
        post_open = "AA:BB:CC:44:55:66"
        controller._consecutive_preopen_failures = 7
        controller._candidate_failure_reason[light.mac] = "gatt_connect"
        controller._candidate_failure_streak[light.mac] = 4
        controller._candidate_cooldown_until[light.mac] = 399
        controller._candidate_failure_reason[post_open] = "proxy_notification"
        controller._candidate_failure_streak[post_open] = 2
        controller._candidate_cooldown_until[post_open] = 280
        controller._candidate_post_open_quarantine_until[post_open] = 280

        controller._reset_connection_recovery_state()

        self.assertEqual(controller._consecutive_preopen_failures, 0)
        self.assertNotIn(light.mac, controller._candidate_failure_reason)
        self.assertNotIn(light.mac, controller._candidate_failure_streak)
        self.assertNotIn(light.mac, controller._candidate_cooldown_until)
        self.assertEqual(
            controller._candidate_failure_reason[post_open],
            "proxy_notification",
        )
        self.assertEqual(controller._candidate_failure_streak[post_open], 2)
        self.assertEqual(controller._candidate_cooldown_until[post_open], 280)
        self.assertEqual(
            controller._candidate_post_open_quarantine_until[post_open],
            280,
        )
        self.assertEqual(
            controller.diagnostic_state()["connect_attempt_stats"][
                "ownership_preopen_backoff_resets"
            ],
            1,
        )

    def test_powered_off_candidate_expires_from_connect_round_after_30_seconds(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        powered_off = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(powered_off)
        info_by_address = {
            light.mac: types.SimpleNamespace(
                address=light.mac,
                time=controller.hass.loop.time(),
                connectable=True,
            ),
            powered_off: types.SimpleNamespace(
                address=powered_off,
                time=(
                    controller.hass.loop.time()
                    - controller_module.ADVERTISEMENT_MAX_AGE
                    - 1
                ),
                connectable=True,
            ),
        }
        original_device = controller_module.bluetooth.async_ble_device_from_address
        original_info = controller_module.bluetooth.async_last_service_info
        controller_module.bluetooth.async_ble_device_from_address = (
            lambda _hass, address, **_kwargs: object()
        )
        controller_module.bluetooth.async_last_service_info = (
            lambda _hass, address, **_kwargs: info_by_address[address]
        )
        try:
            self.assertEqual(controller._candidate_round_members(), {light.mac})
        finally:
            controller_module.bluetooth.async_ble_device_from_address = original_device
            controller_module.bluetooth.async_last_service_info = original_info

        self.assertEqual(controller_module.ADVERTISEMENT_MAX_AGE, 30.0)

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

    def test_gatt_failure_cooldown_stays_short_across_retries(self) -> None:
        controller, light = self._make_controller()
        controller.config.proxy_candidates.append("AA:BB:CC:44:55:66")
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        error = RuntimeError("ESP_GATT_CONN_FAIL_ESTABLISH")

        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 10.0)
        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 10.0)
        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 10.0)
        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 10.0)
        self.assertEqual(controller._candidate_failure_streak[light.mac], 4)

    def test_post_open_failure_cooldown_still_grows_and_is_capped(self) -> None:
        controller, light = self._make_controller()
        controller.config.proxy_candidates.append("AA:BB:CC:44:55:66")
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        error = RuntimeError("Mesh Proxy notification setup failed")

        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 10.0)
        controller._candidate_cooldown_until.pop(light.mac)
        controller._candidate_post_open_quarantine_until.pop(light.mac)
        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 15.0)
        controller._candidate_cooldown_until.pop(light.mac)
        controller._candidate_post_open_quarantine_until.pop(light.mac)
        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 22.5)
        self.assertEqual(controller._candidate_failure_streak[light.mac], 3)

    def test_post_open_progress_resets_preopen_failure_streak(self) -> None:
        controller, light = self._make_controller()
        controller.config.proxy_candidates.append("AA:BB:CC:44:55:66")
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        gatt_error = RuntimeError("ESP_GATT_CONN_FAIL_ESTABLISH")
        notification_error = RuntimeError("Mesh Proxy notification setup failed")

        self.assertEqual(
            controller._cooldown_failed_candidate(light.mac, gatt_error),
            10.0,
        )
        self.assertEqual(
            controller._cooldown_failed_candidate(light.mac, gatt_error),
            10.0,
        )
        self.assertEqual(controller._candidate_failure_streak[light.mac], 2)

        controller._candidate_cooldown_until.pop(light.mac)
        self.assertEqual(
            controller._cooldown_failed_candidate(light.mac, notification_error),
            10.0,
        )
        self.assertEqual(controller._candidate_failure_streak[light.mac], 1)
        self.assertEqual(
            controller._candidate_failure_reason[light.mac],
            "proxy_notification",
        )

    def test_only_reachable_candidate_uses_short_bounded_cooldown(self) -> None:
        controller, light = self._make_controller()
        controller.config.proxy_candidates.extend(
            (
                "AA:BB:CC:44:55:66",
                "AA:BB:CC:44:55:77",
                "AA:BB:CC:44:55:88",
            )
        )
        controller._get_ble_device = (  # type: ignore[method-assign]
            lambda address: object() if address == light.mac else None
        )
        error = RuntimeError("ESP_GATT_CONN_FAIL_ESTABLISH")

        self.assertEqual(controller._candidate_round_members(), {light.mac})
        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 2.0)
        self.assertEqual(controller._cooldown_failed_candidate(light.mac, error), 2.0)
        self.assertEqual(controller._candidate_failure_streak[light.mac], 2)

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
        controller.config.proxy_candidates.append(other)
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

    def test_passive_scan_stale_result_does_not_return_cached_device(self) -> None:
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

    def test_connection_resync_and_normal_waits_stay_passive(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        fresh_device = object()
        fresh_info = types.SimpleNamespace(
            address=light.mac,
            time=controller.hass.loop.time(),
            device=fresh_device,
            connectable=True,
        )
        newer_info = types.SimpleNamespace(
            address=light.mac,
            time=controller.hass.loop.time() + 0.1,
            device=fresh_device,
            connectable=True,
        )
        scan_modes: list[str] = []
        cleared_addresses: list[str] = []
        advertisement_results = [fresh_info, fresh_info, newer_info]

        async def fake_process_advertisements(
            _hass: object,
            _matcher: object,
            _context: object,
            scan_mode: str,
            _timeout: float,
        ) -> object:
            scan_modes.append(scan_mode)
            return advertisement_results.pop(0)

        original_device = controller_module.bluetooth.async_ble_device_from_address
        original_info = controller_module.bluetooth.async_last_service_info
        original_process = getattr(
            controller_module.bluetooth,
            "async_process_advertisements",
            None,
        )
        original_clear_history = getattr(
            controller_module.bluetooth,
            "async_clear_advertisement_history",
            None,
        )
        controller_module.bluetooth.async_ble_device_from_address = (
            lambda *_args, **_kwargs: None
        )
        controller_module.bluetooth.async_last_service_info = (
            lambda *_args, **_kwargs: fresh_info
        )
        controller_module.bluetooth.async_process_advertisements = (
            fake_process_advertisements
        )
        controller_module.bluetooth.async_clear_advertisement_history = (
            lambda _hass, address: cleared_addresses.append(address)
        )
        try:
            regular_result = asyncio.run(
                controller._async_get_ble_device(
                    light.mac,
                    allow_scan=True,
                )
            )
            resync_result = asyncio.run(
                controller._async_get_ble_device(
                    light.mac,
                    resync_advertisement=True,
                    allow_scan=True,
                )
            )
        finally:
            controller_module.bluetooth.async_ble_device_from_address = original_device
            controller_module.bluetooth.async_last_service_info = original_info
            if original_process is None:
                delattr(controller_module.bluetooth, "async_process_advertisements")
            else:
                controller_module.bluetooth.async_process_advertisements = original_process
            if original_clear_history is None:
                delattr(
                    controller_module.bluetooth,
                    "async_clear_advertisement_history",
                )
            else:
                controller_module.bluetooth.async_clear_advertisement_history = (
                    original_clear_history
                )

        self.assertIs(regular_result, fresh_device)
        self.assertIs(resync_result, fresh_device)
        self.assertEqual(scan_modes, ["passive", "passive", "passive"])
        self.assertEqual(cleared_addresses, [light.mac])

    def test_resync_uses_recent_same_scanner_device_after_bounded_wait(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        source = "AA:BB:CC:DD:EE:FF"
        cached_device = object()
        baseline_info = types.SimpleNamespace(
            address=light.mac,
            time=controller.hass.loop.time(),
            device=object(),
            connectable=True,
            source=source,
        )
        scanner_device = types.SimpleNamespace(
            scanner=types.SimpleNamespace(source=source),
            ble_device=cached_device,
        )

        async def fake_process_advertisements(*_args: object, **_kwargs: object):
            controller.hass.loop._time += 0.1
            raise TimeoutError

        original_device = controller_module.bluetooth.async_ble_device_from_address
        original_info = controller_module.bluetooth.async_last_service_info
        original_process = getattr(
            controller_module.bluetooth,
            "async_process_advertisements",
            None,
        )
        original_clear_history = getattr(
            controller_module.bluetooth,
            "async_clear_advertisement_history",
            None,
        )
        original_scanner_devices = getattr(
            controller_module.bluetooth,
            "async_scanner_devices_by_address",
            None,
        )
        original_timeout = controller_module.DISCOVERY_TIMEOUT
        controller_module.bluetooth.async_ble_device_from_address = (
            lambda *_args, **_kwargs: None
        )
        controller_module.bluetooth.async_last_service_info = (
            lambda *_args, **_kwargs: baseline_info
        )
        controller_module.bluetooth.async_process_advertisements = (
            fake_process_advertisements
        )
        controller_module.bluetooth.async_clear_advertisement_history = (
            lambda *_args, **_kwargs: None
        )
        controller_module.bluetooth.async_scanner_devices_by_address = (
            lambda *_args, **_kwargs: [scanner_device]
        )
        controller_module.DISCOVERY_TIMEOUT = 0.2
        controller._current_connect_trace = {"address": light.mac}
        try:
            result = asyncio.run(
                controller._async_get_ble_device(
                    light.mac,
                    resync_advertisement=True,
                    allow_scan=True,
                )
            )
        finally:
            controller_module.bluetooth.async_ble_device_from_address = original_device
            controller_module.bluetooth.async_last_service_info = original_info
            if original_process is None:
                delattr(controller_module.bluetooth, "async_process_advertisements")
            else:
                controller_module.bluetooth.async_process_advertisements = (
                    original_process
                )
            if original_clear_history is None:
                delattr(
                    controller_module.bluetooth,
                    "async_clear_advertisement_history",
                )
            else:
                controller_module.bluetooth.async_clear_advertisement_history = (
                    original_clear_history
                )
            if original_scanner_devices is None:
                delattr(
                    controller_module.bluetooth,
                    "async_scanner_devices_by_address",
                )
            else:
                controller_module.bluetooth.async_scanner_devices_by_address = (
                    original_scanner_devices
                )
            controller_module.DISCOVERY_TIMEOUT = original_timeout

        self.assertIs(result, cached_device)
        self.assertEqual(
            controller._connect_attempt_stats["preopen_scanner_cache_fallbacks"],
            1,
        )
        self.assertEqual(
            controller._current_connect_trace["advertisement_resync_fallback"][
                "result"
            ],
            "same_scanner_cache",
        )

    def test_resync_rejects_stale_scanner_device(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        source = "AA:BB:CC:DD:EE:FF"
        baseline_info = types.SimpleNamespace(
            address=light.mac,
            time=(
                controller.hass.loop.time()
                - controller_module.PREOPEN_SCANNER_CACHE_FALLBACK_MAX_AGE
                - 1
            ),
            device=object(),
            connectable=True,
            source=source,
        )
        scanner_device = types.SimpleNamespace(
            scanner=types.SimpleNamespace(source="00:11:22:44:55:66"),
            ble_device=object(),
        )

        async def fake_process_advertisements(*_args: object, **_kwargs: object):
            controller.hass.loop._time += 0.1
            raise TimeoutError

        original_info = controller_module.bluetooth.async_last_service_info
        original_process = getattr(
            controller_module.bluetooth,
            "async_process_advertisements",
            None,
        )
        original_clear_history = getattr(
            controller_module.bluetooth,
            "async_clear_advertisement_history",
            None,
        )
        original_scanner_devices = getattr(
            controller_module.bluetooth,
            "async_scanner_devices_by_address",
            None,
        )
        original_timeout = controller_module.DISCOVERY_TIMEOUT
        controller_module.bluetooth.async_last_service_info = (
            lambda *_args, **_kwargs: baseline_info
        )
        controller_module.bluetooth.async_process_advertisements = (
            fake_process_advertisements
        )
        controller_module.bluetooth.async_clear_advertisement_history = (
            lambda *_args, **_kwargs: None
        )
        controller_module.bluetooth.async_scanner_devices_by_address = (
            lambda *_args, **_kwargs: [scanner_device]
        )
        controller_module.DISCOVERY_TIMEOUT = 0.2
        try:
            result = asyncio.run(
                controller._async_get_ble_device(
                    light.mac,
                    resync_advertisement=True,
                    allow_scan=True,
                )
            )
        finally:
            controller_module.bluetooth.async_last_service_info = original_info
            if original_process is None:
                delattr(controller_module.bluetooth, "async_process_advertisements")
            else:
                controller_module.bluetooth.async_process_advertisements = (
                    original_process
                )
            if original_clear_history is None:
                delattr(
                    controller_module.bluetooth,
                    "async_clear_advertisement_history",
                )
            else:
                controller_module.bluetooth.async_clear_advertisement_history = (
                    original_clear_history
                )
            if original_scanner_devices is None:
                delattr(
                    controller_module.bluetooth,
                    "async_scanner_devices_by_address",
                )
            else:
                controller_module.bluetooth.async_scanner_devices_by_address = (
                    original_scanner_devices
                )
            controller_module.DISCOVERY_TIMEOUT = original_timeout

        self.assertIsNone(result)
        self.assertNotIn(
            "preopen_scanner_cache_fallbacks",
            controller._connect_attempt_stats,
        )

    def test_scanner_cache_requires_same_source(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        scanner_device = types.SimpleNamespace(
            scanner=types.SimpleNamespace(source="00:11:22:44:55:66"),
            ble_device=object(),
        )
        original_scanner_devices = getattr(
            controller_module.bluetooth,
            "async_scanner_devices_by_address",
            None,
        )
        controller_module.bluetooth.async_scanner_devices_by_address = (
            lambda *_args, **_kwargs: [scanner_device]
        )
        try:
            result = controller._scanner_cached_ble_device(
                light.mac,
                "AA:BB:CC:DD:EE:FF",
            )
        finally:
            if original_scanner_devices is None:
                delattr(
                    controller_module.bluetooth,
                    "async_scanner_devices_by_address",
                )
            else:
                controller_module.bluetooth.async_scanner_devices_by_address = (
                    original_scanner_devices
                )

        self.assertIsNone(result)

    def test_advertisement_wake_callback_uses_passive_scanning(self) -> None:
        controller, _light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        registrations: list[tuple[object, str]] = []

        def fake_register_callback(
            _hass: object,
            _callback: object,
            matcher: object,
            scan_mode: str,
        ) -> object:
            registrations.append((matcher, scan_mode))
            return lambda: None

        original_register = getattr(
            controller_module.bluetooth,
            "async_register_callback",
            None,
        )
        controller_module.bluetooth.async_register_callback = fake_register_callback
        try:
            controller._register_advertisement_callback()
        finally:
            if original_register is None:
                delattr(controller_module.bluetooth, "async_register_callback")
            else:
                controller_module.bluetooth.async_register_callback = original_register

        self.assertEqual(len(registrations), 1)
        matcher, scan_mode = registrations[0]
        self.assertEqual(
            matcher,
            {"service_uuid": controller_module.MESH_PROXY_SERVICE_UUID},
        )
        self.assertEqual(scan_mode, "passive")

    def test_external_active_scanner_is_drained_read_only(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeScanner:
            def __init__(self) -> None:
                self._modes = ["active", "active", "passive", "passive"]

            @property
            def current_mode(self) -> str:
                if len(self._modes) > 1:
                    return self._modes.pop(0)
                return self._modes[0]

        scanner = FakeScanner()
        original_scanner_by_source = getattr(
            controller_module.bluetooth,
            "async_scanner_by_source",
            None,
        )
        original_info = controller_module.bluetooth.async_last_service_info
        original_poll = controller_module.SCANNER_MODE_POLL_INTERVAL
        original_settle = controller_module.POST_ACTIVE_SCAN_SETTLE_SECONDS
        controller_module.bluetooth.async_scanner_by_source = (
            lambda _hass, source: scanner if source == "proxy-source" else None
        )
        controller_module.bluetooth.async_last_service_info = (
            lambda *_args, **_kwargs: types.SimpleNamespace(source="proxy-source")
        )
        controller_module.SCANNER_MODE_POLL_INTERVAL = 0
        controller_module.POST_ACTIVE_SCAN_SETTLE_SECONDS = 0
        controller._current_connect_trace = {
            "address": light.mac,
            "started": controller.hass.loop.time(),
            "stages": {"candidate_selected": 0.0},
        }
        try:
            waited = asyncio.run(
                controller._wait_for_source_scanner_passive(light.mac)
            )
        finally:
            if original_scanner_by_source is None:
                delattr(controller_module.bluetooth, "async_scanner_by_source")
            else:
                controller_module.bluetooth.async_scanner_by_source = (
                    original_scanner_by_source
                )
            controller_module.bluetooth.async_last_service_info = original_info
            controller_module.SCANNER_MODE_POLL_INTERVAL = original_poll
            controller_module.POST_ACTIVE_SCAN_SETTLE_SECONDS = original_settle

        self.assertTrue(waited)
        stats = controller.diagnostic_state()["connect_attempt_stats"]
        self.assertEqual(stats["scanner_active_waits"], 1)
        self.assertEqual(stats["scanner_passive_wait_completions"], 1)
        self.assertNotIn("scanner_active_wait_timeouts", stats)
        self.assertEqual(
            controller._current_connect_trace["scanner_wait"],
            {
                "initial_mode": "active",
                "final_mode": "passive",
                "elapsed_seconds": 0.0,
                "timed_out": False,
            },
        )

    def test_connect_reacquires_advertisement_after_active_scanner(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        first_device = object()
        refreshed_device = object()
        calls: list[bool] = []

        async def fake_get_device(
            _address: str,
            *,
            resync_advertisement: bool = False,
            allow_scan: bool = False,
        ) -> object:
            self.assertTrue(allow_scan)
            calls.append(resync_advertisement)
            return first_device if len(calls) == 1 else refreshed_device

        async def fake_wait(_address: str) -> bool:
            return True

        controller._async_get_ble_device = fake_get_device  # type: ignore[method-assign]
        controller._wait_for_source_scanner_passive = fake_wait  # type: ignore[method-assign]

        original_wait_policy = controller_module.WAIT_FOR_SOURCE_SCANNER_PASSIVE
        controller_module.WAIT_FOR_SOURCE_SCANNER_PASSIVE = True
        try:
            result = asyncio.run(
                controller._prepare_ble_device_for_connect(
                    light.mac,
                    resync_advertisement=False,
                )
            )
        finally:
            controller_module.WAIT_FOR_SOURCE_SCANNER_PASSIVE = original_wait_policy

        self.assertIs(result, refreshed_device)
        self.assertEqual(calls, [False, False])

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

    def test_conn_cancel_waits_for_proxy_slot_close(self) -> None:
        controller, _light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller._last_maintain_error = "Error ESP_GATT_CONN_CONN_CANCEL"

        self.assertEqual(controller._last_error_hint(), "proxy_slot_closing")
        self.assertIn("connection-slot close race", controller._connection_advice())
        self.assertEqual(
            controller._maintain_retry_delay(controller._last_maintain_error),
            controller_module.OUT_OF_SLOTS_RETRY_DELAY,
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

    def test_bootstrap_table_rejection_retries_same_candidate_uncached(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            is_connected = True

            def __init__(self) -> None:
                self.clear_cache_calls = 0

            async def start_notify(self, *_args: object) -> None:
                raise RuntimeError(
                    "Bluetooth GATT Error address=AA:BB:CC:11:22:33 handle=53 "
                    "error=135 description=Illegal parameter"
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
        controller._bootstrap_gatt_cache_sources.add(light.mac)

        with self.assertRaisesRegex(RuntimeError, "bootstrap GATT handle table"):
            asyncio.run(controller._enable_proxy_notifications_for_session(session))

        self.assertNotIn(light.mac, controller._bootstrap_gatt_cache_sources)
        self.assertIn(light.mac, controller._bootstrap_gatt_cache_rejected)
        self.assertIn(light.mac, controller._service_cache_bypass)
        self.assertIn(light.mac, controller._cccd_reconnect_pending)
        self.assertNotIn(light.mac, controller._cached_cccd_retry_attempted)
        self.assertEqual(client.clear_cache_calls, 1)
        self.assertEqual(
            controller._connect_attempt_stats["bootstrap_gatt_cache_fallbacks"],
            1,
        )
        self.assertEqual(
            controller.diagnostic_state()["bootstrap_gatt_cache_rejected"],
            [light.mac],
        )

        controller._last_maintain_error = (
            "RuntimeError: Mesh Proxy CCCD notification setup failed on the "
            "bootstrap GATT handle table; reconnect required and the next "
            "service discovery will bypass cache: BleakError: Bluetooth GATT "
            "Error handle=53 error=135 description=Illegal parameter"
        )
        self.assertEqual(
            controller._last_error_hint(),
            "bootstrap_gatt_profile_rejected",
        )
        self.assertIn("full service discovery", controller._connection_advice())

    def test_bootstrap_table_rejection_skips_handoff_to_untested_peer(
        self,
    ) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._service_cache_bypass.add(light.mac)
        controller._cccd_reconnect_pending.add(light.mac)
        controller._bootstrap_gatt_cache_rejected.add(light.mac)
        controller._has_fresh_ble_device = (  # type: ignore[method-assign]
            lambda address, **_kwargs: address == other
        )

        controller._advance_candidate_cursor(
            light.mac,
            RuntimeError(
                "Mesh Proxy CCCD notification setup failed on the bootstrap "
                "GATT handle table"
            ),
        )

        self.assertEqual(controller._candidate_cursor, 0)
        self.assertNotIn(light.mac, controller._bootstrap_gatt_cache_rejected)
        self.assertNotIn(light.mac, controller._cccd_reconnect_pending)
        self.assertIn(light.mac, controller._service_cache_bypass)
        self.assertNotIn(light.mac, controller._candidate_cooldown_until)
        self.assertNotIn(light.mac, controller._round_failed_candidates)

    def test_bootstrap_session_authorization_error_keeps_authorization_path(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            is_connected = False

            async def start_notify(self, *_args: object) -> None:
                raise RuntimeError("Insufficient authorization (8)")

        session = controller_module._ProxySession(
            address=light.mac,
            client=FakeClient(),
            proxy_data_in=object(),
            proxy_data_out=object(),
            status_notifications_supported=False,
            notification_callback=lambda *_args: None,
        )
        controller._sessions[light.mac] = session
        controller._bootstrap_gatt_cache_sources.add(light.mac)

        with self.assertRaisesRegex(RuntimeError, "authorization error"):
            asyncio.run(controller._enable_proxy_notifications_for_session(session))

        self.assertNotIn(light.mac, controller._bootstrap_gatt_cache_rejected)
        self.assertNotIn(light.mac, controller._service_cache_bypass)
        self.assertNotIn(light.mac, controller._cccd_reconnect_pending)

    def test_bootstrap_success_keeps_normal_notification_path(self) -> None:
        controller, light = self._make_controller()
        module = importlib.import_module("custom_components.amaran_ble.controller")
        client = types.SimpleNamespace(
            is_connected=True, start_notify=AsyncMock(), clear_cache=AsyncMock(),
        )
        session = module._ProxySession(
            address=light.mac, client=client, proxy_data_out=object(),
            notification_callback=lambda *_args: None,
        )
        controller._bootstrap_gatt_cache_sources.add(light.mac)

        self.assertTrue(asyncio.run(controller._enable_proxy_notifications_for_session(session)))

        self.assertTrue(session.notify_started)
        self.assertTrue(session.status_notifications_supported)
        client.clear_cache.assert_not_awaited()
        self.assertFalse(controller._bootstrap_gatt_cache_rejected)
        self.assertFalse(controller._service_cache_bypass)
        self.assertNotIn("bootstrap_gatt_cache_fallbacks", controller._connect_attempt_stats)

    def test_bootstrap_timeout_preserves_existing_cached_retry(self) -> None:
        controller, light = self._make_controller()
        module = importlib.import_module("custom_components.amaran_ble.controller")
        client = types.SimpleNamespace(
            is_connected=False, start_notify=AsyncMock(side_effect=TimeoutError()),
            clear_cache=AsyncMock(),
        )
        session = module._ProxySession(
            address=light.mac, client=client, proxy_data_out=object(),
            notification_callback=lambda *_args: None,
        )
        controller._bootstrap_gatt_cache_sources.add(light.mac)

        with self.assertRaisesRegex(RuntimeError, "preserving the known"):
            asyncio.run(controller._enable_proxy_notifications_for_session(session))

        client.clear_cache.assert_not_awaited()
        self.assertFalse(controller._bootstrap_gatt_cache_rejected)
        self.assertFalse(controller._service_cache_bypass)
        self.assertIn(light.mac, controller._cached_cccd_retry_attempted)
        self.assertNotIn("bootstrap_gatt_cache_fallbacks", controller._connect_attempt_stats)

    def test_bootstrap_fallback_requests_uncached_connection_and_recovers(self) -> None:
        controller, light = self._make_controller()
        module = importlib.import_module("custom_components.amaran_ble.controller")
        client = types.SimpleNamespace(
            is_connected=True,
            start_notify=AsyncMock(side_effect=[RuntimeError("Illegal parameter (135)"), None]),
            clear_cache=AsyncMock(return_value=True),
        )
        session = module._ProxySession(
            address=light.mac, client=client, proxy_data_out=object(),
            notification_callback=lambda *_args: None,
        )
        controller._bootstrap_gatt_cache_sources.add(light.mac)

        async def exercise():
            with self.assertRaisesRegex(RuntimeError, "bootstrap GATT handle table"):
                await controller._enable_proxy_notifications_for_session(session)
            controller._advance_candidate_cursor(light.mac, RuntimeError("reconnect required"))
            with patch.object(module, "establish_connection", AsyncMock(return_value=client)) as connect:
                await controller._establish_bleak_client(object(), light.mac, lambda *_args: None)
                self.assertFalse(connect.call_args.kwargs["use_services_cache"])
            self.assertTrue(await controller._enable_proxy_notifications_for_session(session))

        asyncio.run(exercise())
        client.clear_cache.assert_awaited_once()
        self.assertEqual(controller._connect_attempt_stats["bootstrap_gatt_cache_fallbacks"], 1)
        self.assertFalse(controller._bootstrap_gatt_cache_sources)
        self.assertFalse(controller._bootstrap_gatt_cache_rejected)
        self.assertFalse(controller._service_cache_bypass)
        self.assertFalse(controller._cccd_reconnect_pending)
        self.assertFalse(controller._uncached_recovery_attempted)

    def test_bootstrap_fallback_does_not_loop_on_discovered_table_error(self) -> None:
        controller, light = self._make_controller()
        module = importlib.import_module("custom_components.amaran_ble.controller")
        client = types.SimpleNamespace(
            is_connected=False,
            start_notify=AsyncMock(side_effect=RuntimeError("Illegal parameter (135)")),
            clear_cache=AsyncMock(return_value=True),
        )
        session = module._ProxySession(
            address=light.mac, client=client, proxy_data_out=object(),
            notification_callback=lambda *_args: None,
        )
        controller._bootstrap_gatt_cache_sources.add(light.mac)

        async def exercise():
            with self.assertRaisesRegex(RuntimeError, "bootstrap GATT handle table"):
                await controller._enable_proxy_notifications_for_session(session)
            controller._advance_candidate_cursor(light.mac, RuntimeError("reconnect required"))
            with patch.object(module, "establish_connection", AsyncMock(return_value=client)) as connect:
                await controller._establish_bleak_client(object(), light.mac, lambda *_args: None)
                self.assertFalse(connect.call_args.kwargs["use_services_cache"])
            with self.assertRaisesRegex(RuntimeError, "terminal GATT error") as failure:
                await controller._enable_proxy_notifications_for_session(session)
            controller._advance_candidate_cursor(light.mac, failure.exception)

        asyncio.run(exercise())
        client.clear_cache.assert_awaited_once()
        self.assertEqual(controller._connect_attempt_stats["bootstrap_gatt_cache_fallbacks"], 1)
        self.assertIn(light.mac, controller._round_failed_candidates)
        self.assertFalse(controller._cccd_reconnect_pending)
        self.assertFalse(controller._service_cache_bypass)

    def test_release_clears_bootstrap_rejection_flag(self) -> None:
        controller, light = self._make_controller()
        controller._bootstrap_gatt_cache_rejected.add(light.mac)

        controller._reset_connection_recovery_state()

        self.assertEqual(controller._bootstrap_gatt_cache_rejected, set())
        self.assertEqual(
            controller.diagnostic_state()["bootstrap_gatt_cache_rejected"],
            [],
        )

    def test_authorization_notification_error_requests_fresh_gatt_object(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            is_connected = False

            async def start_notify(self, *_args: object) -> None:
                raise RuntimeError("Insufficient authorization (8)")

        session = controller_module._ProxySession(
            address=light.mac,
            client=FakeClient(),
            proxy_data_in=object(),
            proxy_data_out=object(),
            status_notifications_supported=False,
            notification_callback=lambda *_args: None,
        )

        with self.assertRaisesRegex(RuntimeError, "rotate to another visible"):
            asyncio.run(controller._enable_proxy_notifications_for_session(session))

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

    def test_imported_mesh_subscribes_only_proxy_notification(self) -> None:
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

        self.assertEqual(calls, [(proxy_characteristic, callback)])
        self.assertFalse(session.provisioning_notify_started)
        self.assertTrue(session.notify_started)
        self.assertTrue(session.status_notifications_supported)

    def test_unused_provisioning_notification_is_never_requested(self) -> None:
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

        self.assertEqual(calls, [proxy_characteristic])
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

        async def fake_set_connection_params(**kwargs: int) -> None:
            events.append(
                "params:"
                f"{kwargs['min_interval']}-{kwargs['max_interval']}:"
                f"{kwargs['latency']}:{kwargs['timeout']}"
            )

        client = types.SimpleNamespace(
            is_connected=True,
            services=FakeServices(),
            set_connection_params=fake_set_connection_params,
        )

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
        original_connect_delay = controller_module.OFFICIAL_CONNECT_DELAY
        controller_module.GATT_NOTIFICATION_SETTLE_SECONDS = 0
        controller_module.OFFICIAL_CONNECT_DELAY = 0
        try:
            asyncio.run(controller._connect_session(session))
        finally:
            controller_module.GATT_NOTIFICATION_SETTLE_SECONDS = original_settle
            controller_module.OFFICIAL_CONNECT_DELAY = original_connect_delay

        self.assertEqual(events, ["notifications", "login:True"])
        self.assertIs(session.provisioning_data_out, provisioning_characteristic)
        self.assertTrue(session.mesh_ready)
        self.assertNotIn(
            "connection_params_requests",
            controller._connect_attempt_stats,
        )

    def test_connection_parameter_failure_is_nonfatal(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            async def set_connection_params(self, **_kwargs: int) -> None:
                raise RuntimeError("backend rejected request")

        session = controller_module._ProxySession(
            address=light.mac,
            client=FakeClient(),
        )

        original_enabled = controller_module.POST_CONNECT_CONNECTION_PARAMS_ENABLED
        controller_module.POST_CONNECT_CONNECTION_PARAMS_ENABLED = True
        try:
            asyncio.run(
                controller._request_post_connect_connection_params(session)
            )
        finally:
            controller_module.POST_CONNECT_CONNECTION_PARAMS_ENABLED = original_enabled

        self.assertEqual(
            controller._connect_attempt_stats["connection_params_requests"],
            1,
        )
        self.assertEqual(
            controller._connect_attempt_stats[
                "connection_params_request_failures"
            ],
            1,
        )

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
        original_connect_delay = controller_module.OFFICIAL_CONNECT_DELAY
        controller_module.GATT_NOTIFICATION_SETTLE_SECONDS = 0
        controller_module.OFFICIAL_CONNECT_DELAY = 0
        try:
            asyncio.run(controller._connect_session(session))
        finally:
            controller_module.GATT_NOTIFICATION_SETTLE_SECONDS = original_settle
            controller_module.OFFICIAL_CONNECT_DELAY = original_connect_delay

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

    def test_cold_start_connector_prefers_cached_gatt_services(self) -> None:
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

    def test_reacquired_ownership_attempts_prefer_cached_gatt_services(
        self,
    ) -> None:
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
            controller._ensure_maintain_task = lambda: None  # type: ignore[method-assign]
            asyncio.run(controller.async_set_control_enabled(False))
            asyncio.run(controller.async_set_control_enabled(True))
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

    def test_mesh_verified_gatt_cache_is_shared_with_peer_candidates(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        peer = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(peer)

        class FakeCache:
            def __init__(self) -> None:
                self.services: dict[int, object] = {}
                self.mtus: dict[int, int] = {}

            def get_gatt_services_cache(self, address: int) -> object | None:
                return self.services.get(address)

            def set_gatt_services_cache(
                self,
                address: int,
                services: object,
            ) -> None:
                self.services[address] = services

            def get_gatt_mtu_cache(self, address: int) -> int | None:
                return self.mtus.get(address)

            def set_gatt_mtu_cache(self, address: int, mtu: int) -> None:
                self.mtus[address] = mtu

        class FakeBackend:
            def __init__(self, cache: FakeCache, address: int) -> None:
                self._cache = cache
                self._address_as_int = address

        FakeBackend.__module__ = "homeassistant.bluetooth.remote"
        cache = FakeCache()
        source_address = controller._address_as_int(light.mac)
        services = object()
        cache.services[source_address] = services
        cache.mtus[source_address] = 247
        client = types.SimpleNamespace(_backend=FakeBackend(cache, source_address))
        session = controller_module._ProxySession(
            address=light.mac,
            client=client,
            mesh_ready=True,
        )

        controller._capture_verified_gatt_cache(session)

        peer_address = controller._address_as_int(peer)
        self.assertIs(cache.services[peer_address], services)
        self.assertEqual(cache.mtus[peer_address], 247)
        self.assertEqual(controller._shared_gatt_cache_sources[peer], light.mac)
        diagnostics = controller.diagnostic_state()
        self.assertEqual(diagnostics["verified_gatt_cache_source"], light.mac)
        self.assertEqual(diagnostics["shared_gatt_cache_sources"][peer], light.mac)

    def test_verified_gatt_cache_primes_new_esphome_backend_before_connect(
        self,
    ) -> None:
        controller, light = self._make_controller()
        peer = "AA:BB:CC:44:55:66"
        services = object()
        controller._verified_gatt_cache_template = (services, 247, light.mac)

        class FakeCache:
            def __init__(self) -> None:
                self.services: dict[int, object] = {}
                self.mtus: dict[int, int] = {}

            def get_gatt_services_cache(self, address: int) -> object | None:
                return self.services.get(address)

            def set_gatt_services_cache(
                self,
                address: int,
                value: object,
            ) -> None:
                self.services[address] = value

            def get_gatt_mtu_cache(self, address: int) -> int | None:
                return self.mtus.get(address)

            def set_gatt_mtu_cache(self, address: int, mtu: int) -> None:
                self.mtus[address] = mtu

        class FakeBackend:
            def __init__(self, cache: FakeCache, address: int) -> None:
                self._cache = cache
                self._address_as_int = address

        FakeBackend.__module__ = "homeassistant.bluetooth.remote"
        cache = FakeCache()
        peer_address = controller._address_as_int(peer)
        client = types.SimpleNamespace(_backend=FakeBackend(cache, peer_address))
        controller._current_connect_trace = {
            "address": peer,
            "started": controller.hass.loop.time(),
            "stages": {},
        }

        self.assertTrue(controller._prime_verified_gatt_cache(client, peer))
        self.assertIs(cache.services[peer_address], services)
        self.assertEqual(cache.mtus[peer_address], 247)
        self.assertEqual(controller._shared_gatt_cache_sources[peer], light.mac)
        self.assertTrue(controller._current_connect_trace["backend_cache_primed"])

    def test_cold_esphome_backend_gets_telink_mesh_proxy_bootstrap_cache(
        self,
    ) -> None:
        controller, _light = self._make_controller()
        peer = "AA:BB:CC:44:55:66"
        services = object()

        class FakeCache:
            def __init__(self) -> None:
                self.services: dict[int, object] = {}
                self.mtus: dict[int, int] = {}

            def get_gatt_services_cache(self, address: int) -> object | None:
                return self.services.get(address)

            def set_gatt_services_cache(
                self,
                address: int,
                value: object,
            ) -> None:
                self.services[address] = value

            def get_gatt_mtu_cache(self, address: int) -> int | None:
                return self.mtus.get(address)

            def set_gatt_mtu_cache(self, address: int, mtu: int) -> None:
                self.mtus[address] = mtu

        class FakeBackend:
            def __init__(self, cache: FakeCache, address: int) -> None:
                self._cache = cache
                self._address_as_int = address

        FakeBackend.__module__ = "bleak_esphome.backend.client"
        cache = FakeCache()
        peer_address = controller._address_as_int(peer)
        client = types.SimpleNamespace(_backend=FakeBackend(cache, peer_address))
        controller._build_telink_mesh_proxy_bootstrap_services = lambda: services
        controller._current_connect_trace = {
            "address": peer,
            "started": controller.hass.loop.time(),
            "stages": {},
        }

        self.assertTrue(
            controller._prime_telink_mesh_proxy_bootstrap_cache(client, peer)
        )
        self.assertIs(cache.services[peer_address], services)
        self.assertEqual(
            cache.mtus[peer_address],
            importlib.import_module(
                "custom_components.amaran_ble.controller"
            ).TELINK_MESH_PROXY_BOOTSTRAP_MTU,
        )
        self.assertIn(peer, controller._bootstrap_gatt_cache_sources)
        self.assertEqual(
            controller._current_connect_trace["bootstrap_gatt_cache_profile"],
            "telink_mesh_proxy_v1",
        )

    def test_selected_esphome_backend_is_primed_after_ha_creates_it(self) -> None:
        self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        created: list[object] = []

        class FakeBackend:
            def __init__(self, marker: str) -> None:
                self.marker = marker

        FakeBackend.__module__ = "bleak_esphome.backend.client"
        wrapped = types.SimpleNamespace(client=FakeBackend)

        self.assertTrue(
            controller_module._AmaranBleakClient._wrap_selected_backend_factory(
                wrapped,
                created.append,
            )
        )
        backend = wrapped.client("selected")

        self.assertIs(created[0], backend)
        self.assertEqual(backend.marker, "selected")

    def test_direct_esphome_backend_exposes_gatt_cache_context(self) -> None:
        controller, _light = self._make_controller()

        class FakeCache:
            def get_gatt_services_cache(self, _address: int) -> None:
                return None

            def set_gatt_services_cache(
                self,
                _address: int,
                _services: object,
            ) -> None:
                return None

            def get_gatt_mtu_cache(self, _address: int) -> None:
                return None

            def set_gatt_mtu_cache(self, _address: int, _mtu: int) -> None:
                return None

        class FakeBackend:
            def __init__(self) -> None:
                self._cache = FakeCache()
                self._address_as_int = 123

        FakeBackend.__module__ = "bleak_esphome.backend.client"
        backend = FakeBackend()

        self.assertEqual(
            controller._esphome_gatt_cache_context(backend),
            (backend._cache, 123),
        )

    def test_fresh_discovery_clears_only_selected_target_local_cache(self) -> None:
        controller, light = self._make_controller()
        peer = "AA:BB:CC:44:55:66"

        class FakeCache:
            def __init__(self) -> None:
                self.services: dict[int, object] = {}
                self.mtus: dict[int, int] = {}

            def get_gatt_services_cache(self, address: int) -> object | None:
                return self.services.get(address)

            def set_gatt_services_cache(
                self,
                address: int,
                value: object,
            ) -> None:
                self.services[address] = value

            def clear_gatt_services_cache(self, address: int) -> None:
                self.services.pop(address, None)

            def get_gatt_mtu_cache(self, address: int) -> int | None:
                return self.mtus.get(address)

            def set_gatt_mtu_cache(self, address: int, mtu: int) -> None:
                self.mtus[address] = mtu

            def clear_gatt_mtu_cache(self, address: int) -> None:
                self.mtus.pop(address, None)

        class FakeBackend:
            def __init__(self, cache: FakeCache, address: int) -> None:
                self._cache = cache
                self._address_as_int = address

        FakeBackend.__module__ = "bleak_esphome.backend.client"
        cache = FakeCache()
        target_address = controller._address_as_int(light.mac)
        peer_address = controller._address_as_int(peer)
        cache.services[target_address] = object()
        cache.services[peer_address] = object()
        cache.mtus[target_address] = 247
        cache.mtus[peer_address] = 247
        client = types.SimpleNamespace(
            _backend=FakeBackend(cache, target_address)
        )
        controller._shared_gatt_cache_sources[light.mac] = peer
        controller._current_connect_trace = {
            "address": light.mac,
            "started": controller.hass.loop.time(),
            "stages": {},
        }

        self.assertTrue(
            controller._discard_target_local_gatt_cache(client, light.mac)
        )
        self.assertNotIn(target_address, cache.services)
        self.assertNotIn(target_address, cache.mtus)
        self.assertIn(peer_address, cache.services)
        self.assertIn(peer_address, cache.mtus)
        self.assertNotIn(light.mac, controller._shared_gatt_cache_sources)
        self.assertTrue(
            controller._current_connect_trace[
                "target_local_gatt_cache_cleared"
            ]
        )

    def test_failed_shared_gatt_cache_falls_back_to_service_discovery(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeClient:
            def __init__(self) -> None:
                self.clear_cache_calls = 0

            async def clear_cache(self) -> bool:
                self.clear_cache_calls += 1
                return True

        client = FakeClient()
        controller._shared_gatt_cache_sources[light.mac] = "AA:BB:CC:44:55:66"
        session = controller_module._ProxySession(
            address=light.mac,
            client=client,
        )

        asyncio.run(controller._discard_failed_shared_gatt_cache(session))

        self.assertEqual(client.clear_cache_calls, 1)
        self.assertNotIn(light.mac, controller._shared_gatt_cache_sources)
        self.assertIn(light.mac, controller._shared_gatt_cache_incompatible)
        self.assertIn(light.mac, controller._service_cache_bypass)

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

    def test_connect_attempt_diagnostics_count_source_scanner_mode(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        now = controller.hass.loop.time()
        controller._last_service_info = (  # type: ignore[method-assign]
            lambda _address: types.SimpleNamespace(
                rssi=-69,
                time=now - 0.1,
                source="proxy-a",
                connectable=True,
            )
        )
        original_scanner_by_source = getattr(
            controller_module.bluetooth,
            "async_scanner_by_source",
            None,
        )
        controller_module.bluetooth.async_scanner_by_source = (
            lambda _hass, source: (
                types.SimpleNamespace(current_mode="passive")
                if source == "proxy-a"
                else None
            )
        )
        controller._current_connect_trace = {
            "address": light.mac,
            "started": now,
            "requested_cache_mode": "with_cache",
            "requested_cache_reason": "normal_cache",
            "scanner_at_selection": {"source": "proxy-a", "mode": "passive"},
            "stages": {"candidate_selected": 0.0},
        }
        try:
            controller._mark_connect_advertisement_ready(light.mac)
            controller._record_connect_attempt(
                light.mac,
                "failed",
                20.0,
                "ESP_GATT_CONN_FAIL_ESTABLISH",
            )
        finally:
            if original_scanner_by_source is None:
                delattr(controller_module.bluetooth, "async_scanner_by_source")
            else:
                controller_module.bluetooth.async_scanner_by_source = (
                    original_scanner_by_source
                )

        item = controller.diagnostic_state()["recent_connect_attempts"][-1]
        self.assertEqual(
            item["scanner_at_connect"],
            {"source": "proxy-a", "mode": "passive"},
        )
        stats = controller.diagnostic_state()["connect_attempt_stats"]
        self.assertEqual(stats["scanner_passive_attempts"], 1)
        self.assertEqual(stats["scanner_passive_failed"], 1)

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

    def test_connector_labels_preopen_uncached_recovery(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        calls: dict[str, object] = {}
        connected_client = types.SimpleNamespace(is_connected=True)
        controller._service_cache_bypass.add(light.mac)
        controller._candidate_failure_reason[light.mac] = "gatt_connect"
        controller._current_connect_trace = {
            "address": light.mac,
            "started": controller.hass.loop.time(),
            "stages": {},
        }

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
        self.assertEqual(
            controller._current_connect_trace["requested_cache_reason"],
            "gatt_connect_recovery",
        )

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

    def test_backend_connector_owns_establish_lifecycle(self) -> None:
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
            await asyncio.sleep(0.02)
            return client

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

        self.assertEqual(len(created), 1)
        self.assertIs(result, created[0])
        self.assertEqual(created[0].disconnect_calls, 0)
        self.assertIsNone(controller_module.GATT_ESTABLISH_DEADLINE)
        self.assertNotIn(light.mac, controller._pending_clients)

    def test_backend_connect_timeout_uses_gatt_cooldown(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller._proxy_addresses = lambda: [  # type: ignore[method-assign]
            light.mac,
            "AA:BB:CC:44:55:66",
        ]
        error = RuntimeError(
            "Timeout waiting for connect response while connecting to "
            f"{light.mac}"
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
                resync_advertisement: bool = False,
            ) -> object:
                self.assertFalse(resync_advertisement)
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

    def test_preopen_failure_requests_bounded_advertisement_resync(
        self,
    ) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        controller.config.proxy_candidates.append(other)
        controller._control_enabled = True
        controller._candidate_failure_reason[light.mac] = "gatt_connect"
        controller._get_ble_device = lambda _address: object()  # type: ignore[method-assign]
        controller._has_fresh_ble_device = (  # type: ignore[method-assign]
            lambda _address, **_kwargs: True
        )
        resync_values: list[bool] = []

        async def run_test() -> None:
            async def fake_order(_addresses: list[str]) -> list[str]:
                return [light.mac]

            async def fake_ensure_proxy(
                address: str,
                *,
                resync_advertisement: bool = False,
            ) -> object:
                self.assertEqual(address, light.mac)
                resync_values.append(resync_advertisement)
                return types.SimpleNamespace(address=address)

            async def fake_disconnect_inactive(*, keep: str) -> None:
                self.assertEqual(keep, light.mac)

            controller._connect_candidate_order = fake_order  # type: ignore[method-assign]
            controller._ensure_proxy = fake_ensure_proxy  # type: ignore[method-assign]
            controller._disconnect_inactive_sessions = (  # type: ignore[method-assign]
                fake_disconnect_inactive
            )
            await controller._connect_active_proxy_locked()

        asyncio.run(run_test())

        self.assertEqual(
            controller._candidate_round_members(),
            {light.mac, other},
        )
        self.assertEqual(resync_values, [True])
        self.assertEqual(
            controller.diagnostic_state()["connect_attempt_stats"][
                "gatt_failure_advertisement_resyncs"
            ],
            1,
        )

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

    def test_amaran_client_uses_backend_owned_connect_timeout(self) -> None:
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
        self.assertIsNone(controller_module.GATT_ESTABLISH_DEADLINE)

    def test_diagnostics_expose_hybrid_connection_profile(self) -> None:
        controller, _light = self._make_controller()

        profile = controller.diagnostic_state()["connection_profile"]

        self.assertEqual(
            profile["name"],
            "esphome_beta107_current_scanner_slot_recovery",
        )
        self.assertTrue(profile["esphome_allocation_release_gate"])
        self.assertTrue(profile["esphome_persistent_slot_recovery"])
        self.assertEqual(
            profile["esphome_allocation_release_timeout_seconds"],
            25.0,
        )
        self.assertEqual(
            profile["esphome_slot_recovery_recheck_seconds"],
            5.0,
        )
        self.assertTrue(profile["single_candidate_connector_slot_retry"])
        self.assertEqual(
            profile["single_candidate_connector_slot_retry_min_failures"],
            2,
        )
        self.assertEqual(
            profile["single_candidate_connector_slot_retry_max_attempts"],
            4,
        )
        self.assertEqual(
            profile["single_candidate_connector_slot_retry_membership"],
            "broad_visible_and_bounded_ownership_cycle",
        )
        self.assertEqual(profile["ownership_alternative_evidence_seconds"], 90.0)
        self.assertEqual(
            profile["preopen_scanner_cache_fallback_max_age_seconds"],
            8.0,
        )
        self.assertEqual(
            profile["deferred_multi_candidate_recovery_delay_seconds"],
            30,
        )
        self.assertTrue(profile["prime_telink_mesh_proxy_bootstrap_cache"])
        self.assertEqual(profile["telink_mesh_proxy_bootstrap_mtu"], 23)
        self.assertFalse(profile["gatt_authorization_same_candidate_retry"])
        self.assertTrue(
            profile[
                "single_candidate_gatt_authorization_same_candidate_retry"
            ]
        )
        self.assertEqual(
            profile["gatt_authorization_reconnect_settle_seconds"],
            8.0,
        )
        self.assertEqual(
            profile["advertisement_callback_scanning_mode"],
            "passive",
        )
        self.assertTrue(profile["wait_for_source_scanner_passive"])
        self.assertEqual(
            profile["candidate_policy"],
            "all_fresh_visible_no_rssi_focus",
        )
        self.assertTrue(profile["preopen_fallback_deferral_enabled"])
        self.assertFalse(profile["force_fresh_gatt_discovery"])
        self.assertEqual(
            profile["preopen_advertisement_resync_scanning_mode"],
            "passive",
        )
        self.assertEqual(
            profile["gatt_cache_scope"],
            "telink_mesh_proxy_bootstrap_then_mesh_verified",
        )
        self.assertTrue(
            profile["share_verified_gatt_cache_between_candidates"]
        )
        self.assertFalse(profile["rssi_focus_enabled"])
        self.assertEqual(profile["rssi_focus_window_db"], 6)
        self.assertEqual(profile["rssi_focus_max_failed_rounds"], 2)
        self.assertEqual(profile["notification_setup_timeout_seconds"], 30.0)
        self.assertEqual(profile["source_pre_connect_delay_seconds"], 1.0)
        self.assertEqual(profile["pre_connect_delay_seconds"], 0.0)
        self.assertEqual(profile["source_physical_connect_timeout_seconds"], 9.0)
        self.assertEqual(profile["backend_connect_timeout_seconds"], 45.0)
        self.assertEqual(profile["source_service_discovery_timeout_seconds"], 9.0)
        self.assertIsNone(profile["combined_establish_deadline_seconds"])
        self.assertTrue(profile["preopen_advertisement_resync"])
        self.assertEqual(profile["source_service_ready_delay_seconds"], 0.5)
        self.assertEqual(profile["service_ready_delay_seconds"], 3.0)
        self.assertEqual(profile["proxy_filter_delay_seconds"], 0.2)
        self.assertEqual(profile["filter_status_deadline_seconds"], 8.0)
        self.assertEqual(profile["requested_mtu"], 517)
        self.assertTrue(profile["mtu_managed_by_backend"])
        self.assertFalse(profile["post_connect_connection_params_enabled"])
        self.assertEqual(profile["post_connect_min_interval_units"], 9)
        self.assertEqual(profile["post_connect_max_interval_units"], 12)
        self.assertEqual(profile["post_connect_latency"], 0)
        self.assertEqual(
            profile["post_connect_supervision_timeout_units"],
            2000,
        )
        self.assertEqual(
            profile["post_connect_params_request_timeout_seconds"],
            3.0,
        )

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

    def test_amaran_client_exposes_slot_cancel_for_connector_retry(self) -> None:
        self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        async def failing_connect(_client: object, **_kwargs: object) -> object:
            raise controller_module.BleakError("ESP_GATT_CONN_CONN_CANCEL")

        original_connect = controller_module.BleakClientWithServiceCache.connect
        controller_module.BleakClientWithServiceCache.connect = failing_connect
        try:
            client = controller_module._AmaranBleakClient(
                object(),
                amaran_retry_transient_slot_errors=True,
            )
            with self.assertRaisesRegex(
                controller_module.BleakError,
                "ESP_GATT_CONN_CONN_CANCEL",
            ):
                asyncio.run(client.connect(timeout=20.0))
        finally:
            controller_module.BleakClientWithServiceCache.connect = original_connect

    def test_amaran_client_bounds_same_client_slot_cancel_retries(self) -> None:
        self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        async def failing_connect(_client: object, **_kwargs: object) -> object:
            raise controller_module.BleakError("ESP_GATT_CONN_CONN_CANCEL")

        original_connect = controller_module.BleakClientWithServiceCache.connect
        controller_module.BleakClientWithServiceCache.connect = failing_connect
        try:
            client = controller_module._AmaranBleakClient(
                object(),
                amaran_retry_transient_slot_errors=True,
                amaran_retry_transient_slot_errors_max_attempts=4,
            )
            for _attempt in range(3):
                with self.assertRaises(controller_module.BleakError):
                    asyncio.run(client.connect(timeout=20.0))
            with self.assertRaisesRegex(
                RuntimeError,
                "retry budget exhausted after 4 attempts",
            ) as raised:
                asyncio.run(client.connect(timeout=20.0))
        finally:
            controller_module.BleakClientWithServiceCache.connect = original_connect

        self.assertIsInstance(raised.exception.__cause__, controller_module.BleakError)

    def test_connector_slot_retry_requires_one_repeatedly_failing_candidate(
        self,
    ) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"

        controller._candidate_round_members = (  # type: ignore[method-assign]
            lambda **_kwargs: {light.mac}
        )
        self.assertFalse(controller._should_use_connector_slot_retry(light.mac))
        controller._record_candidate_health_outcome(light.mac, "gatt_connect")
        controller._record_candidate_health_outcome(
            light.mac,
            "gatt_or_service_discovery",
        )
        self.assertTrue(controller._should_use_connector_slot_retry(light.mac))

        controller._candidate_round_members = (  # type: ignore[method-assign]
            lambda **_kwargs: {light.mac, other}
        )
        self.assertFalse(controller._should_use_connector_slot_retry(light.mac))

    def test_connector_slot_retry_counts_deferred_candidate_as_reachable(
        self,
    ) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"
        calls: list[dict[str, bool]] = []

        controller._record_candidate_health_outcome(light.mac, "gatt_connect")
        controller._record_candidate_health_outcome(
            light.mac,
            "gatt_or_service_discovery",
        )

        def candidate_members(**kwargs: bool) -> set[str]:
            calls.append(kwargs)
            if kwargs.get("include_deferred"):
                return {light.mac, other}
            return {light.mac}

        controller._candidate_round_members = candidate_members  # type: ignore[method-assign]
        controller._current_connect_trace = {"address": light.mac}

        self.assertFalse(controller._should_use_connector_slot_retry(light.mac))
        self.assertEqual(
            calls,
            [
                {},
                {
                    "include_deferred": True,
                    "include_rssi_fallback": True,
                },
            ],
        )
        self.assertEqual(
            controller._current_connect_trace["connector_retry_round_members"],
            [light.mac],
        )
        self.assertEqual(
            controller._current_connect_trace[
                "connector_retry_broad_round_members"
            ],
            sorted((light.mac, other)),
        )
        self.assertEqual(
            controller._connect_attempt_stats[
                "connector_internal_slot_retry_multi_candidate_suppressions"
            ],
            1,
        )

    def test_connector_slot_retry_rejects_attempted_stale_alternative(
        self,
    ) -> None:
        controller, light = self._make_controller()
        other = "AA:BB:CC:44:55:66"

        controller._record_candidate_health_outcome(light.mac, "gatt_connect")
        controller._record_candidate_health_outcome(
            light.mac,
            "gatt_or_service_discovery",
        )
        controller._candidate_round_members = (  # type: ignore[method-assign]
            lambda **_kwargs: {light.mac}
        )
        controller._ownership_attempted_candidates.update((light.mac, other))
        controller._ownership_candidate_attempted_at.update(
            {light.mac: 100.0, other: 100.0}
        )
        controller._current_connect_trace = {"address": light.mac}

        self.assertFalse(controller._should_use_connector_slot_retry(light.mac))
        self.assertEqual(
            controller._current_connect_trace[
                "connector_retry_attempted_alternatives"
            ],
            [other],
        )
        self.assertEqual(
            controller._connect_attempt_stats[
                "connector_internal_slot_retry_ownership_suppressions"
            ],
            1,
        )

        controller._reset_connection_recovery_state()
        self.assertTrue(controller._should_use_connector_slot_retry(light.mac))

    def test_connector_slot_retry_accepts_expired_stale_alternative(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"

        controller._record_candidate_health_outcome(light.mac, "gatt_connect")
        controller._record_candidate_health_outcome(
            light.mac,
            "gatt_or_service_discovery",
        )
        controller._candidate_round_members = (  # type: ignore[method-assign]
            lambda **_kwargs: {light.mac}
        )
        controller._ownership_attempted_candidates.update((light.mac, other))
        controller._ownership_candidate_attempted_at.update(
            {
                light.mac: controller.hass.loop.time(),
                other: (
                    controller.hass.loop.time()
                    - controller_module.OWNERSHIP_ALTERNATIVE_EVIDENCE_SECONDS
                    - 1
                ),
            }
        )
        controller._current_connect_trace = {"address": light.mac}

        self.assertTrue(controller._should_use_connector_slot_retry(light.mac))
        self.assertEqual(
            controller._current_connect_trace[
                "connector_retry_expired_attempted_alternatives"
            ],
            [other],
        )
        self.assertEqual(
            controller._connect_attempt_stats[
                "connector_internal_slot_retry_expired_ownership_releases"
            ],
            1,
        )

    def test_fresh_alternative_still_blocks_expired_ownership_evidence(
        self,
    ) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        other = "AA:BB:CC:44:55:66"

        controller._record_candidate_health_outcome(light.mac, "gatt_connect")
        controller._record_candidate_health_outcome(
            light.mac,
            "gatt_or_service_discovery",
        )

        def candidate_members(**kwargs: bool) -> set[str]:
            if kwargs.get("include_deferred"):
                return {light.mac, other}
            return {light.mac}

        controller._candidate_round_members = candidate_members  # type: ignore[method-assign]
        controller._ownership_attempted_candidates.update((light.mac, other))
        controller._ownership_candidate_attempted_at.update(
            {
                light.mac: controller.hass.loop.time(),
                other: (
                    controller.hass.loop.time()
                    - controller_module.OWNERSHIP_ALTERNATIVE_EVIDENCE_SECONDS
                    - 1
                ),
            }
        )
        controller._current_connect_trace = {"address": light.mac}

        self.assertFalse(controller._should_use_connector_slot_retry(light.mac))
        self.assertEqual(
            controller._current_connect_trace[
                "connector_retry_expired_attempted_alternatives"
            ],
            [other],
        )
        self.assertEqual(
            controller._connect_attempt_stats[
                "connector_internal_slot_retry_multi_candidate_suppressions"
            ],
            1,
        )

    def test_establish_client_records_connector_internal_gatt_attempts(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        calls: dict[str, object] = {}
        connected_client = types.SimpleNamespace(is_connected=True)
        controller._current_connect_trace = {
            "address": light.mac,
            "connector_internal_slot_retry": True,
            "connector_gatt_attempts": 0,
        }

        async def fake_establish(
            _client_class: type,
            _device: object,
            _name: str,
            **kwargs: object,
        ) -> object:
            calls.update(kwargs)
            kwargs["amaran_connect_attempt_started"]()
            kwargs["amaran_connect_attempt_started"]()
            kwargs["amaran_client_created"](connected_client)
            return connected_client

        original = controller_module.establish_connection
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
            controller_module.establish_connection = original

        self.assertIs(result, connected_client)
        self.assertTrue(calls["amaran_retry_transient_slot_errors"])
        self.assertEqual(
            calls["amaran_retry_transient_slot_errors_max_attempts"],
            4,
        )
        self.assertEqual(
            controller._current_connect_trace["connector_gatt_attempts"],
            2,
        )
        self.assertEqual(
            controller._connect_attempt_stats["connector_internal_gatt_attempts"],
            2,
        )

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

    def test_stalled_client_disposal_is_bounded_and_releases_guard(self) -> None:
        controller, _light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        async def run_test() -> None:
            blocker = asyncio.Event()
            disconnect_task = asyncio.create_task(blocker.wait())
            original_timeout = controller_module.CLIENT_CLEANUP_DRAIN_TIMEOUT
            controller_module.CLIENT_CLEANUP_DRAIN_TIMEOUT = 0.01
            try:
                cleanup = asyncio.create_task(
                    controller._drain_aborted_connect([disconnect_task])
                )
                controller._track_candidate_cleanup_task(cleanup)
                await asyncio.wait_for(cleanup, timeout=1.0)
                await asyncio.sleep(0)
            finally:
                controller_module.CLIENT_CLEANUP_DRAIN_TIMEOUT = original_timeout

            self.assertTrue(disconnect_task.cancelled())
            self.assertTrue(
                await controller._wait_candidate_cleanup_tasks(timeout=0)
            )

        asyncio.run(run_test())

        state = controller.diagnostic_state()
        self.assertFalse(state["connection_cleanup_in_progress"])
        self.assertEqual(
            state["connect_attempt_stats"]["forced_client_cleanup_cancellations"],
            1,
        )
        self.assertGreaterEqual(
            controller._next_connect_after,
            controller.hass.loop.time()
            + controller_module.RELEASE_RECONNECT_COOLDOWN,
        )
        self.assertGreaterEqual(
            controller._release_guard_until,
            controller.hass.loop.time()
            + controller_module.RELEASE_RECONNECT_COOLDOWN,
        )

    def _make_replaced_esphome_tracker(self):
        controller, light = self._make_controller()
        module = importlib.import_module("custom_components.amaran_ble.controller")
        source = "AA:BB:CC:DD:EE:01"
        old = types.SimpleNamespace(
            mac_address=source, available=True,
            ble_connections_free=2, ble_connections_limit=3,
            ble_allocations=[controller._address_as_int(light.mac)],
        )
        allocations = types.SimpleNamespace(adapter=source, slots=3, free=3, allocated=[])
        scanner = types.SimpleNamespace(
            source=source,
            _bluetooth_device=types.SimpleNamespace(available=True),
            get_allocations=Mock(return_value=allocations),
        )
        return controller, light, module, old, scanner

    def test_replaced_slot_tracker_recovers_even_if_old_available_is_true(self) -> None:
        controller, light, module, old, scanner = self._make_replaced_esphome_tracker()
        for available in (False, True):
            with self.subTest(old_available=available), patch.object(
                module.bluetooth, "async_scanner_by_source", return_value=scanner, create=True,
            ) as lookup:
                old.available = available
                controller._pending_esphome_slot_releases[light.mac] = old
                self.assertTrue(asyncio.run(controller._wait_for_pending_esphome_slot_recovery(timeout=0)))
                self.assertFalse(controller._pending_esphome_slot_releases)
                self.assertTrue(controller._esphome_slot_recovery_resync_pending)
                self.assertIsNone(controller._esphome_slot_recovery_started)
                lookup.assert_called_with(controller.hass, old.mac_address)
                self.assertEqual(old.ble_connections_free, 2)
                self.assertEqual(controller._last_esphome_slot_release["snapshot_status"], "current_scanner")

    def test_initial_release_uses_current_scanner_not_stale_backend(self) -> None:
        controller, light, module, old, scanner = self._make_replaced_esphome_tracker()
        client = types.SimpleNamespace(_amaran_backend=types.SimpleNamespace(_bluetooth_device=old))
        with patch.object(module.bluetooth, "async_scanner_by_source", return_value=scanner, create=True):
            self.assertTrue(asyncio.run(controller._wait_for_esphome_slot_release(client, light.mac, timeout=0)))
        self.assertFalse(controller._pending_esphome_slot_releases)
        self.assertEqual(controller._last_esphome_slot_release["result"], "confirmed")

    def test_current_busy_allocation_blocks_even_if_old_tracker_is_free(self) -> None:
        controller, light, module, old, scanner = self._make_replaced_esphome_tracker()
        old.ble_connections_free, old.ble_allocations = 3, []
        scanner.get_allocations.return_value.free = 2
        scanner.get_allocations.return_value.allocated = [light.mac]
        controller._pending_esphome_slot_releases[light.mac] = old
        with patch.object(module.bluetooth, "async_scanner_by_source", return_value=scanner, create=True):
            with self.assertRaisesRegex(RuntimeError, "slot recovery is still pending"):
                asyncio.run(controller._wait_for_pending_esphome_slot_recovery(timeout=0))
        self.assertIn(light.mac, controller._pending_esphome_slot_releases)
        self.assertTrue(controller._last_esphome_slot_release["target_allocated"])

    def test_initial_release_rechecks_unknown_then_ready_scanner(self) -> None:
        controller, light, module, old, scanner = self._make_replaced_esphome_tracker()
        client = types.SimpleNamespace(_amaran_backend=types.SimpleNamespace(_bluetooth_device=old))
        old.wait_for_ble_connections_free = AsyncMock(side_effect=AssertionError("retired tracker"))
        free = scanner.get_allocations.return_value
        busy = types.SimpleNamespace(adapter=old.mac_address, slots=3, free=0, allocated=[light.mac])
        scanner.get_allocations.side_effect = [None, busy, free]
        with patch.object(module.bluetooth, "async_scanner_by_source", return_value=scanner, create=True):
            with patch.object(module, "ESPHOME_SLOT_RELEASE_POLL_INTERVAL", 0.001):
                self.assertTrue(asyncio.run(controller._wait_for_esphome_slot_release(client, light.mac, timeout=1)))
        old.wait_for_ble_connections_free.assert_not_awaited()
        self.assertFalse(controller._pending_esphome_slot_releases)

    def test_initial_release_unknown_snapshot_preserves_pending(self) -> None:
        controller, light, module, old, scanner = self._make_replaced_esphome_tracker()
        client = types.SimpleNamespace(_amaran_backend=types.SimpleNamespace(_bluetooth_device=old))
        scanner.get_allocations.return_value = None
        with patch.object(module.bluetooth, "async_scanner_by_source", return_value=scanner, create=True):
            self.assertFalse(asyncio.run(controller._wait_for_esphome_slot_release(client, light.mac, timeout=0.001)))
        self.assertIn(light.mac, controller._pending_esphome_slot_releases)
        self.assertEqual(controller._last_esphome_slot_release["snapshot_status"], "allocations_unknown")

    def test_unknown_current_allocations_never_use_old_free_slot(self) -> None:
        controller, light, module, old, scanner = self._make_replaced_esphome_tracker()
        old.ble_connections_free, old.ble_allocations = 3, []
        invalid = types.SimpleNamespace(adapter=old.mac_address, slots=0, free=0, allocated=[])
        wrong_source = types.SimpleNamespace(adapter="AA:BB:CC:DD:EE:02", slots=3, free=3, allocated=[])
        for value, expected in ((None, "allocations_unknown"), (invalid, "allocations_unknown"), (wrong_source, "source_mismatch")):
            with self.subTest(expected=expected):
                scanner.get_allocations.return_value = value
                controller._pending_esphome_slot_releases[light.mac] = old
                with patch.object(module.bluetooth, "async_scanner_by_source", return_value=scanner, create=True):
                    with self.assertRaisesRegex(RuntimeError, "slot recovery is still pending"):
                        asyncio.run(controller._wait_for_pending_esphome_slot_recovery(timeout=0))
                self.assertEqual(controller._last_esphome_slot_release["snapshot_status"], expected)
                self.assertIsNone(controller._last_esphome_slot_release["free"])

    def test_missing_scanner_blocks_then_recovers_after_registration(self) -> None:
        controller, light, module, old, scanner = self._make_replaced_esphome_tracker()
        controller._pending_esphome_slot_releases[light.mac] = old
        with patch.object(module.bluetooth, "async_scanner_by_source", return_value=None, create=True) as lookup:
            with self.assertRaisesRegex(RuntimeError, "scanner_unavailable"):
                asyncio.run(controller._wait_for_pending_esphome_slot_recovery(timeout=0))
            controller.hass.loop._time += 120
            self.assertEqual(controller.diagnostic_state()["esphome_slot_recovery_wait_seconds"], 120)
            lookup.return_value = scanner
            self.assertTrue(asyncio.run(controller._wait_for_pending_esphome_slot_recovery(timeout=0)))
        self.assertFalse(controller._pending_esphome_slot_releases)

    def test_slot_recovery_refreshes_scanner_during_same_wait(self) -> None:
        controller, light, module, old, scanner = self._make_replaced_esphome_tracker()
        controller._pending_esphome_slot_releases[light.mac] = old
        with patch.object(module.bluetooth, "async_scanner_by_source", side_effect=[None, scanner], create=True):
            with patch.object(module, "ESPHOME_SLOT_RELEASE_POLL_INTERVAL", 0.001):
                self.assertTrue(asyncio.run(controller._wait_for_pending_esphome_slot_recovery(timeout=1)))
        self.assertFalse(controller._pending_esphome_slot_releases)

    def test_slot_lookup_failure_remains_pending_without_gatt(self) -> None:
        controller, light, module, old, scanner = self._make_replaced_esphome_tracker()
        controller._pending_esphome_slot_releases[light.mac] = old
        controller._ensure_proxy = AsyncMock()
        original_wait = controller._wait_for_pending_esphome_slot_recovery
        controller._wait_for_pending_esphome_slot_recovery = lambda: original_wait(timeout=0)
        with patch.object(module.bluetooth, "async_scanner_by_source", side_effect=RuntimeError("not ready"), create=True):
            with self.assertRaisesRegex(RuntimeError, "lookup_failed"):
                asyncio.run(controller._connect_active_proxy_locked())
        controller._ensure_proxy.assert_not_awaited()
        self.assertIn(light.mac, controller._pending_esphome_slot_releases)

    def test_recovery_does_not_accept_different_proxy_scanner(self) -> None:
        controller, light, module, old, scanner = self._make_replaced_esphome_tracker()
        scanner.source = "AA:BB:CC:DD:EE:02"
        controller._pending_esphome_slot_releases[light.mac] = old
        with patch.object(module.bluetooth, "async_scanner_by_source", return_value=scanner, create=True):
            with self.assertRaisesRegex(RuntimeError, "source_mismatch"):
                asyncio.run(controller._wait_for_pending_esphome_slot_recovery(timeout=0))
        scanner.get_allocations.assert_not_called()

    def test_slot_recovery_hint_explains_no_new_gatt(self) -> None:
        controller, _light = self._make_controller()
        controller._last_maintain_error = "ESPHome BLE slot recovery is still pending"
        self.assertEqual(controller._last_error_hint(), "proxy_slot_recovery")
        self.assertIn("no new GATT", controller._connection_advice())

    def test_pending_esphome_slot_recovery_blocks_gatt_until_reusable(self) -> None:
        controller, light = self._make_controller()

        class FakeSlotDevice:
            def __init__(self) -> None:
                self.ble_connections_free = 0
                self.ble_connections_limit = 1
                self.ble_allocations = [controller._address_as_int(light.mac)]

        async def run_test() -> None:
            device = FakeSlotDevice()
            controller._pending_esphome_slot_releases[light.mac] = device

            with self.assertRaisesRegex(
                RuntimeError,
                "slot recovery is still pending",
            ):
                await controller._wait_for_pending_esphome_slot_recovery(
                    timeout=0.001,
                )

            self.assertIn(
                light.mac,
                controller._pending_esphome_slot_releases,
            )
            self.assertFalse(
                controller._esphome_slot_recovery_resync_pending
            )

            device.ble_allocations = []
            device.ble_connections_free = 1
            self.assertTrue(
                await controller._wait_for_pending_esphome_slot_recovery(
                    timeout=0.001,
                )
            )
            self.assertFalse(controller._pending_esphome_slot_releases)
            self.assertTrue(
                controller._esphome_slot_recovery_resync_pending
            )

        asyncio.run(run_test())

        state = controller.diagnostic_state()
        self.assertEqual(
            state["last_esphome_slot_release"]["result"],
            "recovered",
        )
        self.assertEqual(state["pending_esphome_slot_releases"], [])

    def test_active_proxy_does_not_open_gatt_while_slot_recovery_is_pending(
        self,
    ) -> None:
        controller, _light = self._make_controller()

        async def run_test() -> None:
            controller._wait_for_pending_esphome_slot_recovery = AsyncMock(
                side_effect=RuntimeError(
                    "ESPHome BLE slot recovery is still pending"
                )
            )
            controller._ensure_proxy = AsyncMock()

            with self.assertRaisesRegex(
                RuntimeError,
                "slot recovery is still pending",
            ):
                await controller._connect_active_proxy_locked()

            controller._ensure_proxy.assert_not_awaited()

        asyncio.run(run_test())

    def test_recovered_slot_forces_fresh_advertisement_before_gatt(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        async def run_test() -> None:
            controller._esphome_slot_recovery_resync_pending = True
            controller._connect_candidate_order = AsyncMock(
                return_value=[light.mac]
            )
            session = controller_module._ProxySession(address=light.mac)
            controller._ensure_proxy = AsyncMock(return_value=session)

            self.assertIs(
                await controller._connect_active_proxy_locked(),
                session,
            )
            controller._ensure_proxy.assert_awaited_once_with(
                light.mac,
                resync_advertisement=True,
            )
            self.assertEqual(
                controller._connect_attempt_stats[
                    "slot_recovery_advertisement_resyncs"
                ],
                1,
            )

        asyncio.run(run_test())

    def test_esphome_slot_release_waits_for_target_allocation_to_clear(self) -> None:
        controller, light = self._make_controller()

        class FakeSlotDevice:
            def __init__(self) -> None:
                self.ble_connections_free = 0
                self.ble_connections_limit = 1
                self.ble_allocations = [controller._address_as_int(light.mac)]
                self.released = asyncio.Event()

            async def wait_for_ble_connections_free(self, _timeout: float) -> int:
                await self.released.wait()
                return self.ble_connections_free

        async def run_test() -> None:
            device = FakeSlotDevice()
            client = types.SimpleNamespace(
                _amaran_backend=types.SimpleNamespace(
                    _bluetooth_device=device,
                )
            )

            async def release() -> None:
                await asyncio.sleep(0)
                device.ble_allocations = []
                device.ble_connections_free = 1
                device.released.set()

            release_task = asyncio.create_task(release())
            self.assertTrue(
                await controller._wait_for_esphome_slot_release(
                    client,
                    light.mac,
                    timeout=1.0,
                )
            )
            await release_task

        asyncio.run(run_test())

        state = controller.diagnostic_state()
        self.assertEqual(
            state["last_esphome_slot_release"]["result"],
            "confirmed",
        )
        self.assertEqual(
            state["connect_attempt_stats"]["esphome_slot_release_confirmed"],
            1,
        )

    def test_esphome_slot_release_requires_a_reusable_free_slot(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )

        class FakeSlotDevice:
            ble_connections_free = 0
            ble_connections_limit = 1
            ble_allocations: list[int] = []

        async def run_test() -> None:
            client = types.SimpleNamespace(
                _amaran_backend=types.SimpleNamespace(
                    _bluetooth_device=FakeSlotDevice(),
                )
            )
            self.assertFalse(
                await controller._wait_for_esphome_slot_release(
                    client,
                    light.mac,
                    timeout=0.001,
                )
            )

        asyncio.run(run_test())

        state = controller.diagnostic_state()
        self.assertEqual(
            state["last_esphome_slot_release"]["result"],
            "timeout",
        )
        self.assertGreaterEqual(
            controller._next_connect_after,
            controller.hass.loop.time()
            + controller_module.RELEASE_RECONNECT_COOLDOWN,
        )

        self.assertGreaterEqual(
            controller._release_guard_until,
            controller.hass.loop.time()
            + controller_module.RELEASE_RECONNECT_COOLDOWN,
        )
        self.assertIn(
            light.mac,
            controller._pending_esphome_slot_releases,
        )
        self.assertTrue(state["connection_cleanup_in_progress"])

    def test_non_esphome_cleanup_keeps_beta77_fallback_behavior(self) -> None:
        controller, light = self._make_controller()

        result = asyncio.run(
            controller._wait_for_esphome_slot_release(
                object(),
                light.mac,
                timeout=0.001,
            )
        )

        self.assertIsNone(result)
        state = controller.diagnostic_state()
        self.assertEqual(
            state["last_esphome_slot_release"]["result"],
            "unsupported",
        )

    def test_cleanup_waits_for_remote_esphome_slot_release(self) -> None:
        controller, light = self._make_controller()

        class FakeSlotDevice:
            def __init__(self) -> None:
                self.ble_connections_free = 0
                self.ble_connections_limit = 1
                self.ble_allocations = [controller._address_as_int(light.mac)]
                self.released = asyncio.Event()

            async def wait_for_ble_connections_free(self, _timeout: float) -> int:
                await self.released.wait()
                return self.ble_connections_free

        class FakeClient:
            def __init__(self, device: FakeSlotDevice) -> None:
                self.is_connected = True
                self._amaran_backend = types.SimpleNamespace(
                    _bluetooth_device=device,
                )

            async def disconnect(self) -> None:
                self.is_connected = False

        async def run_test() -> None:
            device = FakeSlotDevice()
            client = FakeClient(device)

            async def release() -> None:
                await asyncio.sleep(0)
                device.ble_allocations = []
                device.ble_connections_free = 1
                device.released.set()

            release_task = asyncio.create_task(release())
            self.assertTrue(await controller._cleanup_client(client, light.mac))
            await release_task

        asyncio.run(run_test())
        self.assertEqual(
            controller.diagnostic_state()["last_esphome_slot_release"]["result"],
            "confirmed",
        )

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
        controller._status_seen_in_session.add(light.mesh_address)
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
        controller._status_seen_in_session.add(light.mesh_address)
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

    def test_initial_census_keeps_nonresponding_fixture_unavailable(self) -> None:
        controller, light = self._make_controller()
        controller_module = importlib.import_module(
            "custom_components.amaran_ble.controller"
        )
        controller._set_mesh_available(True)
        attempts = 0

        async def fake_refresh_all_statuses(*, timeout: float) -> dict[int, object]:
            nonlocal attempts
            _ = timeout
            attempts += 1
            return {}

        controller.async_refresh_all_statuses = (  # type: ignore[method-assign]
            fake_refresh_all_statuses
        )
        original_delay = controller_module.INITIAL_STATUS_REFRESH_DELAY
        controller_module.INITIAL_STATUS_REFRESH_DELAY = 0
        try:
            asyncio.run(controller._initial_refresh_all())
        finally:
            controller_module.INITIAL_STATUS_REFRESH_DELAY = original_delay

        self.assertEqual(attempts, controller_module.INITIAL_STATUS_REFRESH_ATTEMPTS)
        self.assertFalse(controller.is_light_available(light.mac))
        self.assertTrue(
            controller.diagnostic_state()["initial_status_census_complete"]
        )

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
