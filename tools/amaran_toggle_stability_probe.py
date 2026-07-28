from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import amaran_reboot_stability_probe as probe


DEFAULT_LIGHTS = ("light.amaran_t2c_1", "light.amaran_t2c_2")
CONTROL_SWITCH = "switch.amaran_ble_control"
CONNECTION_SENSOR = "sensor.amaran_ble"


def short_state(state: dict) -> str:
    return str(state.get("state", "missing"))


def selected_light_state(state: dict) -> dict:
    """Return the light fields needed to verify one control action."""
    attrs = state.get("attributes") or {}
    return {
        "state": state.get("state"),
        "error": state.get("error"),
        "brightness": attrs.get("brightness"),
        "color_mode": attrs.get("color_mode"),
        "color_temp_kelvin": attrs.get("color_temp_kelvin"),
        "hs_color": attrs.get("hs_color"),
        "last_updated": state.get("last_updated"),
    }


def action_matches(state: dict, service: str, payload: dict) -> bool:
    """Check that Home Assistant reports the requested light state."""
    if service == "turn_off":
        return state.get("state") == "off"
    if state.get("state") != "on":
        return False
    attrs = state.get("attributes") or {}
    brightness = payload.get("brightness")
    if brightness is not None:
        observed = attrs.get("brightness")
        if observed is None or abs(int(observed) - int(brightness)) > 3:
            return False
    kelvin = payload.get("color_temp_kelvin")
    if kelvin is not None:
        observed = attrs.get("color_temp_kelvin")
        if (
            attrs.get("color_mode") != "color_temp"
            or observed is None
            or abs(int(observed) - int(kelvin)) > 50
        ):
            return False
    requested_hs = payload.get("hs_color")
    if requested_hs is not None:
        observed_hs = attrs.get("hs_color")
        if attrs.get("color_mode") != "hs" or not observed_hs:
            return False
        hue_delta = abs(float(observed_hs[0]) - float(requested_hs[0])) % 360
        hue_delta = min(hue_delta, 360 - hue_delta)
        if hue_delta > 2 or abs(float(observed_hs[1]) - float(requested_hs[1])) > 3:
            return False
    return True


def fixture_capabilities(state: dict) -> dict:
    """Return the color capabilities advertised by one HA light entity."""
    attrs = state.get("attributes") or {}
    modes = {
        str(mode)
        for mode in (attrs.get("supported_color_modes") or [])
        if mode is not None
    }
    return {
        "supported_color_modes": sorted(modes),
        "supports_color_temp": "color_temp" in modes,
        "supports_hs": "hs" in modes,
        "min_kelvin": attrs.get("min_color_temp_kelvin"),
        "max_kelvin": attrs.get("max_color_temp_kelvin"),
    }


def clamp_kelvin(value: int, capabilities: dict) -> int:
    """Clamp a test CCT value to the fixture range reported by HA."""
    minimum = capabilities.get("min_kelvin")
    maximum = capabilities.get("max_kelvin")
    if minimum is not None:
        value = max(value, int(minimum))
    if maximum is not None:
        value = min(value, int(maximum))
    return value


def build_action_sequence(
    state: dict,
    *,
    brightness_pct: int | None,
    brightness_toggle_pct: int | None,
    full_actions: bool,
) -> list[tuple[str, dict]]:
    """Build only commands supported by the selected fixture."""
    capabilities = fixture_capabilities(state)
    if brightness_pct is not None:
        assert brightness_toggle_pct is not None
        brightness = round(brightness_pct * 255 / 100)
        toggle_brightness = round(brightness_toggle_pct * 255 / 100)
        sequence: list[tuple[str, dict]] = [
            ("turn_on", {"brightness": brightness}),
            ("turn_on", {"brightness": toggle_brightness}),
        ]
        if capabilities["supports_color_temp"]:
            sequence.append(
                (
                    "turn_on",
                    {
                        "brightness": brightness,
                        "color_temp_kelvin": clamp_kelvin(3200, capabilities),
                    },
                )
            )
        if capabilities["supports_hs"]:
            sequence.append(
                (
                    "turn_on",
                    {
                        "brightness": toggle_brightness,
                        "hs_color": [0, 100],
                    },
                )
            )
        if capabilities["supports_color_temp"]:
            sequence.append(
                (
                    "turn_on",
                    {
                        "brightness": brightness,
                        "color_temp_kelvin": clamp_kelvin(4500, capabilities),
                    },
                )
            )
        return sequence

    sequence = [
        ("turn_off", {}),
        ("turn_on", {"brightness": 128}),
    ]
    if full_actions and capabilities["supports_color_temp"]:
        sequence.append(
            (
                "turn_on",
                {
                    "brightness": 64,
                    "color_temp_kelvin": clamp_kelvin(3200, capabilities),
                },
            )
        )
    if full_actions and capabilities["supports_hs"]:
        sequence.append(
            (
                "turn_on",
                {
                    "brightness": 96,
                    "hs_color": [0, 100],
                },
            )
        )
    if full_actions and capabilities["supports_color_temp"]:
        sequence.append(
            (
                "turn_on",
                {
                    "brightness": 128,
                    "color_temp_kelvin": clamp_kelvin(4500, capabilities),
                },
            )
        )
    return sequence


def build_restore_action(state: dict) -> tuple[str, dict]:
    """Build a best-effort HA service call that restores one light snapshot."""
    if state.get("state") != "on":
        return "turn_off", {}

    attrs = state.get("attributes") or {}
    payload: dict = {}
    if attrs.get("brightness") is not None:
        payload["brightness"] = int(attrs["brightness"])
    color_mode = attrs.get("color_mode")
    if color_mode == "color_temp" and attrs.get("color_temp_kelvin") is not None:
        payload["color_temp_kelvin"] = int(attrs["color_temp_kelvin"])
    elif color_mode == "hs" and attrs.get("hs_color") is not None:
        payload["hs_color"] = list(attrs["hs_color"])
    return "turn_on", payload


def call_and_verify(
    ha: probe.HomeAssistantApi,
    entity_id: str,
    service: str,
    payload: dict,
    timeout: float = 35,
    verify_timeout: float = 5.0,
    verify_poll: float = 0.25,
) -> dict:
    """Run one light action and bounded-poll until HA reports the request."""
    result = probe.call_light_action(ha, entity_id, service, payload, timeout)
    verification_started = time.monotonic()
    observations: list[dict] = []
    verified = False
    while True:
        state = ha.get_state(entity_id)
        observation = selected_light_state(state)
        if not observations or observation != observations[-1]:
            observations.append(observation)
        verified = bool(result.get("ok")) and action_matches(
            state,
            service,
            payload,
        )
        if verified or time.monotonic() - verification_started >= verify_timeout:
            break
        time.sleep(max(verify_poll, 0.05))

    result["requested"] = dict(payload)
    result["observed_first"] = observations[0]
    result["observed"] = observations[-1]
    result["verification_observations"] = observations
    result["verification_checks"] = len(observations)
    result["verification_elapsed"] = round(
        time.monotonic() - verification_started,
        3,
    )
    result["verified"] = verified
    return result


def save_result(
    output_path: str,
    results: list[dict],
    *,
    complete: bool,
    final_release: dict | None = None,
    initial_state: dict | None = None,
    run_error: str | None = None,
) -> None:
    """Checkpoint completed cycles so a network interruption does not erase them."""
    output = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "complete": complete,
        "initial_state": initial_state,
        "cycles": results,
        "final_release": final_release,
        "run_error": run_error,
    }
    Path(output_path).write_text(json.dumps(output, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qr")
    parser.add_argument(
        "--token-env",
        default=probe.DEFAULT_TOKEN_ENV,
        help="Environment variable containing a HA token. The token is never "
        "written to the result file.",
    )
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--release-wait", type=float, default=20.0)
    parser.add_argument("--ready-timeout", type=float, default=240.0)
    parser.add_argument("--hold-seconds", type=float, default=0.0)
    parser.add_argument("--keep-current", action="store_true")
    parser.add_argument("--skip-actions", action="store_true")
    parser.add_argument("--full-actions", action="store_true")
    parser.add_argument("--brightness-pct", type=int)
    parser.add_argument("--brightness-toggle-pct", type=int)
    parser.add_argument("--require-stable", action="store_true")
    parser.add_argument("--poll", type=float, default=0.25)
    parser.add_argument("--verify-timeout", type=float, default=5.0)
    parser.add_argument("--verify-poll", type=float, default=0.25)
    parser.add_argument("--entities", nargs="+", default=list(DEFAULT_LIGHTS))
    parser.add_argument(
        "--restore-state",
        action="store_true",
        help="Restore the initial control-switch and light states after the run.",
    )
    parser.add_argument("--output")
    args = parser.parse_args()
    args.entities = tuple(args.entities)
    low_brightness_values = (
        args.brightness_pct,
        args.brightness_toggle_pct,
    )
    if any(value is not None for value in low_brightness_values):
        if not all(
            value is not None and 1 <= value <= 100
            for value in low_brightness_values
        ):
            parser.error(
                "--brightness-pct and --brightness-toggle-pct must both be 1..100"
            )
    output_path = args.output or (
        f"amaran-toggle-stability-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )

    try:
        token = probe.resolve_token(args.qr, args.token_env)
    except RuntimeError as err:
        parser.error(str(err))
    ha = probe.HomeAssistantApi(args.ha_url, token)
    results: list[dict] = []
    initial_control = ha.get_state(CONTROL_SWITCH)
    initial_lights = {
        entity_id: ha.get_state(entity_id) for entity_id in args.entities
    }
    initial_state = {
        "control": {
            "state": initial_control.get("state"),
            "error": initial_control.get("error"),
            "last_updated": initial_control.get("last_updated"),
        },
        "lights": {
            entity_id: selected_light_state(state)
            for entity_id, state in initial_lights.items()
        },
        "capabilities": {
            entity_id: fixture_capabilities(state)
            for entity_id, state in initial_lights.items()
        },
    }

    for cycle_number in range(1, args.cycles + 1):
        print(f"\n=== Toggle cycle {cycle_number} ===", flush=True)
        if not args.keep_current:
            ha.call_service(
                "switch",
                "turn_off",
                {"entity_id": CONTROL_SWITCH},
                timeout=30,
            )
            time.sleep(args.release_wait)

        started = time.monotonic()
        if not args.keep_current:
            ha.call_service(
                "switch",
                "turn_on",
                {"entity_id": CONTROL_SWITCH},
                timeout=30,
            )
        transitions: list[dict] = []
        previous: tuple[str, ...] | None = None
        first_connected_elapsed: float | None = None
        connected_elapsed: float | None = None

        while time.monotonic() - started < args.ready_timeout:
            elapsed = time.monotonic() - started
            connection = ha.get_state(CONNECTION_SENSOR)
            light_states = [ha.get_state(entity_id) for entity_id in args.entities]
            labels = (
                short_state(connection),
                *(short_state(state) for state in light_states),
            )
            if labels != previous:
                print(f"  +{elapsed:5.1f}s " + " | ".join(labels), flush=True)
                transitions.append(
                    {
                        "elapsed": round(elapsed, 3),
                        "connection": labels[0],
                        "lights": list(labels[1:]),
                    }
                )
                previous = labels
            connected_and_available = labels[0] == "connected" and all(
                state not in probe.UNAVAILABLE_STATES for state in labels[1:]
            )
            if connected_and_available:
                if first_connected_elapsed is None:
                    first_connected_elapsed = elapsed
                attrs = connection.get("attributes") or {}
                stable_ready = bool(attrs.get("connection_stable")) and bool(
                    attrs.get("status_notifications_supported")
                )
                if not args.require_stable or stable_ready:
                    connected_elapsed = elapsed
                    break
            time.sleep(args.poll)

        actions: list[dict] = []
        hold_samples: list[dict] = []
        hold_disconnects = 0
        if connected_elapsed is not None:
            if not args.skip_actions:
                for entity_id in args.entities:
                    action_sequence = build_action_sequence(
                        ha.get_state(entity_id),
                        brightness_pct=args.brightness_pct,
                        brightness_toggle_pct=args.brightness_toggle_pct,
                        full_actions=args.full_actions,
                    )
                    for service, payload in action_sequence:
                        actions.append(
                            call_and_verify(
                                ha,
                                entity_id,
                                service,
                                payload,
                                verify_timeout=args.verify_timeout,
                                verify_poll=args.verify_poll,
                            )
                        )
            hold_started = time.monotonic()
            hold_previous: tuple[str, ...] | None = None
            hold_last_recorded = -30.0
            while time.monotonic() - hold_started < args.hold_seconds:
                hold_elapsed = time.monotonic() - hold_started
                connection = ha.get_state(CONNECTION_SENSOR)
                light_states = [ha.get_state(entity_id) for entity_id in args.entities]
                labels = (
                    short_state(connection),
                    *(short_state(state) for state in light_states),
                )
                if labels != hold_previous or hold_elapsed - hold_last_recorded >= 30.0:
                    attrs = connection.get("attributes") or {}
                    print(
                        f"  hold +{hold_elapsed:6.1f}s "
                        + " | ".join(labels)
                        + f" | active={attrs.get('active_proxy')} "
                        + f"uptime={attrs.get('connected_seconds')}",
                        flush=True,
                    )
                    hold_samples.append(
                        {
                            "elapsed": round(hold_elapsed, 3),
                            "connection": labels[0],
                            "lights": list(labels[1:]),
                            "active_proxy": attrs.get("active_proxy"),
                            "connected_seconds": attrs.get("connected_seconds"),
                        }
                    )
                    if (
                        hold_previous is not None
                        and hold_previous[0] == "connected"
                        and labels[0] != "connected"
                    ):
                        hold_disconnects += 1
                    hold_previous = labels
                    hold_last_recorded = hold_elapsed
                time.sleep(max(args.poll, 0.25))
        results.append(
            {
                "cycle": cycle_number,
                "first_connected_elapsed": (
                    None
                    if first_connected_elapsed is None
                    else round(first_connected_elapsed, 3)
                ),
                "connected_elapsed": (
                    None if connected_elapsed is None else round(connected_elapsed, 3)
                ),
                "transitions": transitions,
                "actions": actions,
                "hold_seconds": args.hold_seconds,
                "hold_disconnects": hold_disconnects,
                "hold_samples": hold_samples,
            }
        )
        save_result(
            output_path,
            results,
            complete=False,
            initial_state=initial_state,
        )

    final_release = {
        "requested": False,
        "mode": "none",
        "control": None,
        "connection": None,
        "elapsed": None,
        "error": None,
        "restore_actions": [],
    }
    if args.restore_state:
        cleanup_started = time.monotonic()
        final_release["requested"] = True
        final_release["mode"] = "restore_initial_state"
        try:
            if short_state(ha.get_state(CONTROL_SWITCH)) != "on":
                ha.call_service(
                    "switch",
                    "turn_on",
                    {"entity_id": CONTROL_SWITCH},
                    timeout=30,
                )
            while time.monotonic() - cleanup_started < args.ready_timeout:
                connection = ha.get_state(CONNECTION_SENSOR)
                light_states = [
                    ha.get_state(entity_id) for entity_id in args.entities
                ]
                if (
                    short_state(connection) == "connected"
                    and all(
                        short_state(state) not in probe.UNAVAILABLE_STATES
                        for state in light_states
                    )
                ):
                    break
                time.sleep(max(args.poll, 0.25))

            for entity_id in args.entities:
                service, payload = build_restore_action(initial_lights[entity_id])
                final_release["restore_actions"].append(
                    call_and_verify(
                        ha,
                        entity_id,
                        service,
                        payload,
                        verify_timeout=args.verify_timeout,
                        verify_poll=args.verify_poll,
                    )
                )

            if short_state(initial_control) != "on":
                ha.call_service(
                    "switch",
                    "turn_off",
                    {"entity_id": CONTROL_SWITCH},
                    timeout=30,
                )
                while time.monotonic() - cleanup_started < args.ready_timeout + 45:
                    control = ha.get_state(CONTROL_SWITCH)
                    connection = ha.get_state(CONNECTION_SENSOR)
                    if (
                        short_state(control) == "off"
                        and short_state(connection) == "released"
                    ):
                        break
                    time.sleep(max(args.poll, 0.25))

            final_release["control"] = short_state(ha.get_state(CONTROL_SWITCH))
            final_release["connection"] = short_state(
                ha.get_state(CONNECTION_SENSOR)
            )
        except Exception as err:  # noqa: BLE001 - preserve cleanup diagnostics.
            final_release["error"] = f"{type(err).__name__}: {err}"
        final_release["elapsed"] = round(
            time.monotonic() - cleanup_started,
            3,
        )
        print(
            "\nFinal restore: "
            f"control={final_release['control']} "
            f"connection={final_release['connection']} "
            f"elapsed={final_release['elapsed']}s",
            flush=True,
        )
    elif not args.keep_current:
        release_started = time.monotonic()
        final_release["requested"] = True
        final_release["mode"] = "release"
        try:
            ha.call_service(
                "switch",
                "turn_off",
                {"entity_id": CONTROL_SWITCH},
                timeout=30,
            )
            while time.monotonic() - release_started < 45:
                control = ha.get_state(CONTROL_SWITCH)
                connection = ha.get_state(CONNECTION_SENSOR)
                final_release["control"] = short_state(control)
                final_release["connection"] = short_state(connection)
                if (
                    final_release["control"] == "off"
                    and final_release["connection"] == "released"
                ):
                    break
                time.sleep(max(args.poll, 0.25))
        except Exception as err:  # noqa: BLE001 - preserve the test result on cleanup failure.
            final_release["error"] = f"{type(err).__name__}: {err}"
        final_release["elapsed"] = round(
            time.monotonic() - release_started,
            3,
        )
        print(
            "\nFinal release: "
            f"control={final_release['control']} "
            f"connection={final_release['connection']} "
            f"elapsed={final_release['elapsed']}s",
            flush=True,
        )

    save_result(
        output_path,
        results,
        complete=True,
        final_release=final_release,
        initial_state=initial_state,
    )
    print(f"\nSaved result: {output_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
