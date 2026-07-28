from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_ENTITIES = ("light.amaran_t2c_1", "light.amaran_t2c_2")
UNAVAILABLE_STATES = {"unavailable", "unknown", "missing", "api_error"}
DEFAULT_QR = os.environ.get("AMARAN_HA_TOKEN_QR")
DEFAULT_HA_URL = os.environ.get("AMARAN_HA_URL")
DEFAULT_SSH_HOST = os.environ.get("AMARAN_HA_SSH_HOST")
DEFAULT_SSH_KEY = os.environ.get("AMARAN_HA_SSH_KEY")
DEFAULT_TOKEN_ENV = "AMARAN_HA_TOKEN"


def now_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def decode_token(qr_path: Path) -> str:
    from PIL import Image
    import zxingcpp

    barcodes = zxingcpp.read_barcodes(Image.open(qr_path))
    if not barcodes:
        raise RuntimeError(f"Unable to decode HA token QR: {qr_path}")
    return barcodes[0].text.strip()


def resolve_token(qr_path: str | None, token_env: str) -> str:
    """Read a HA token from an environment variable or a QR image."""
    token = os.environ.get(token_env, "").strip()
    if token:
        return token
    if qr_path:
        return decode_token(Path(qr_path))
    raise RuntimeError(
        f"Set {token_env} or provide --qr / AMARAN_HA_TOKEN_QR"
    )


class HomeAssistantApi:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        data = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode())

    def wait_until_ready(self, timeout: float) -> float:
        start = time.monotonic()
        last_error = ""
        while time.monotonic() - start < timeout:
            try:
                self.request_json("GET", "/api/", timeout=5)
                return time.monotonic() - start
            except Exception as err:  # noqa: BLE001
                last_error = f"{type(err).__name__}: {err}"
                time.sleep(2)
        raise TimeoutError(f"Home Assistant API did not become ready: {last_error}")

    def set_amaran_log_level(self, level: str) -> None:
        loggers = {"custom_components.amaran_ble": level}
        if level == "debug":
            loggers.update(
                {
                    "habluetooth": level,
                    "bleak_esphome": level,
                    "bleak_retry_connector": level,
                    "aioesphomeapi": level,
                }
            )
        self.request_json(
            "POST",
            "/api/services/logger/set_level",
            loggers,
            timeout=10,
        )

    def get_state(self, entity_id: str) -> dict[str, Any]:
        try:
            return self.request_json("GET", f"/api/states/{entity_id}", timeout=8)
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return {"entity_id": entity_id, "state": "missing", "error": "404"}
            return {
                "entity_id": entity_id,
                "state": "api_error",
                "error": f"HTTP {err.code}",
            }
        except Exception as err:  # noqa: BLE001
            return {
                "entity_id": entity_id,
                "state": "api_error",
                "error": f"{type(err).__name__}: {err}",
            }

    def call_service(
        self,
        domain: str,
        service: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> tuple[float, Any]:
        start = time.monotonic()
        result = self.request_json(
            "POST",
            f"/api/services/{domain}/{service}",
            payload,
            timeout=timeout,
        )
        return time.monotonic() - start, result


def state_short(state: dict[str, Any]) -> str:
    label = str(state.get("state", "missing"))
    attrs = state.get("attributes") or {}
    if label == "on":
        brightness = attrs.get("brightness")
        kelvin = attrs.get("color_temp_kelvin")
        pieces = [label]
        if brightness is not None:
            pieces.append(f"b={brightness}")
        if kelvin is not None:
            pieces.append(f"k={kelvin}")
        return " ".join(pieces)
    if "error" in state:
        return f"{label}({state['error']})"
    return label


def states_are_ready(states: dict[str, dict[str, Any]]) -> bool:
    return all(str(item.get("state")) not in UNAVAILABLE_STATES for item in states.values())


def restart_ha(args: argparse.Namespace) -> float:
    cmd = [
        "ssh",
        "-i",
        args.ssh_key,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={args.ssh_timeout}",
        "-p",
        str(args.ssh_port),
        f"{args.ssh_user}@{args.ssh_host}",
        "ha core restart",
    ]
    start = time.monotonic()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=args.restart_timeout,
        check=False,
    )
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        raise RuntimeError(
            "ha core restart failed: "
            f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    return elapsed


def wait_for_ready_states(
    ha: HomeAssistantApi,
    entities: tuple[str, ...],
    timeout: float,
    poll: float,
) -> tuple[float | None, list[dict[str, Any]], dict[str, dict[str, Any]]]:
    start = time.monotonic()
    history: list[dict[str, Any]] = []
    last_labels: tuple[str, ...] | None = None
    last_states: dict[str, dict[str, Any]] = {}
    while time.monotonic() - start < timeout:
        elapsed = time.monotonic() - start
        states = {entity: ha.get_state(entity) for entity in entities}
        labels = tuple(state_short(states[entity]) for entity in entities)
        last_states = states
        history.append(
            {
                "elapsed": round(elapsed, 3),
                "states": {
                    entity: {
                        "state": states[entity].get("state"),
                        "last_updated": states[entity].get("last_updated"),
                        "short": state_short(states[entity]),
                    }
                    for entity in entities
                },
            }
        )
        if labels != last_labels:
            print(f"  +{elapsed:6.1f}s states: " + " | ".join(labels), flush=True)
            last_labels = labels
        if states_are_ready(states):
            return elapsed, history, states
        time.sleep(poll)
    return None, history, last_states


def call_light_action(
    ha: HomeAssistantApi,
    entity_id: str,
    service: str,
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    request = {"entity_id": entity_id}
    request.update(payload)
    start_label = now_label()
    try:
        elapsed, _ = ha.call_service("light", service, request, timeout=timeout)
        time.sleep(1.5)
        state = ha.get_state(entity_id)
        print(
            f"    {service:8s} {entity_id}: ok in {elapsed:.3f}s -> {state_short(state)}",
            flush=True,
        )
        return {
            "entity_id": entity_id,
            "service": service,
            "ok": True,
            "elapsed": round(elapsed, 3),
            "started_at": start_label,
            "state": {
                "state": state.get("state"),
                "short": state_short(state),
                "last_updated": state.get("last_updated"),
            },
        }
    except Exception as err:  # noqa: BLE001
        elapsed = time.monotonic()
        print(
            f"    {service:8s} {entity_id}: failed ({type(err).__name__}: {err})",
            flush=True,
        )
        return {
            "entity_id": entity_id,
            "service": service,
            "ok": False,
            "started_at": start_label,
            "error": f"{type(err).__name__}: {err}",
            "monotonic": elapsed,
        }


def run_cycle(args: argparse.Namespace, ha: HomeAssistantApi, cycle_index: int) -> dict[str, Any]:
    print(f"\n=== Cycle {cycle_index} started at {now_label()} ===", flush=True)
    cycle: dict[str, Any] = {
        "cycle": cycle_index,
        "started_at": now_label(),
        "entities": list(args.entities),
    }
    if not args.no_restart:
        print("Restarting Home Assistant Core...", flush=True)
        restart_elapsed = restart_ha(args)
        cycle["restart_elapsed"] = round(restart_elapsed, 3)
        print(f"  restart command returned in {restart_elapsed:.1f}s", flush=True)

    api_elapsed = ha.wait_until_ready(args.api_timeout)
    cycle["api_ready_elapsed"] = round(api_elapsed, 3)
    print(f"  API ready after {api_elapsed:.1f}s", flush=True)

    try:
        ha.set_amaran_log_level("debug")
    except Exception as err:  # noqa: BLE001
        print(f"  Could not set debug log level: {type(err).__name__}: {err}", flush=True)
        cycle["set_debug_error"] = f"{type(err).__name__}: {err}"

    if args.ensure_control_on:
        try:
            elapsed, _ = ha.call_service(
                "switch",
                "turn_on",
                {"entity_id": args.control_switch},
                timeout=15,
            )
            cycle["control_switch_on_elapsed"] = round(elapsed, 3)
            print(f"  Control switch on after {elapsed:.1f}s", flush=True)
        except Exception as err:  # noqa: BLE001
            cycle["control_switch_on_error"] = f"{type(err).__name__}: {err}"
            print(
                f"  Could not turn control switch on: {type(err).__name__}: {err}",
                flush=True,
            )

    ready_elapsed, history, ready_states = wait_for_ready_states(
        ha,
        args.entities,
        args.ready_timeout,
        args.poll,
    )
    cycle["state_history"] = history
    cycle["ready_elapsed"] = None if ready_elapsed is None else round(ready_elapsed, 3)
    cycle["ready_states"] = {
        entity: {
            "state": ready_states.get(entity, {}).get("state"),
            "short": state_short(ready_states.get(entity, {})),
            "last_updated": ready_states.get(entity, {}).get("last_updated"),
        }
        for entity in args.entities
    }
    if ready_elapsed is None:
        print(f"  Not ready within {args.ready_timeout:.0f}s", flush=True)
        return cycle

    print(f"  Both entities ready after {ready_elapsed:.1f}s", flush=True)
    time.sleep(args.settle)

    actions: list[dict[str, Any]] = []
    for entity in args.entities:
        actions.append(call_light_action(ha, entity, "turn_off", {}, args.service_timeout))
        actions.append(
            call_light_action(
                ha,
                entity,
                "turn_on",
                {"brightness": args.test_brightness},
                args.service_timeout,
            )
        )
    cycle["actions"] = actions
    cycle["ended_at"] = now_label()
    return cycle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--qr",
        default=DEFAULT_QR,
        help="Path to a QR image containing a Home Assistant long-lived access token. "
        "Can also be set with AMARAN_HA_TOKEN_QR.",
    )
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help="Environment variable containing a HA token. The token is never "
        "written to the result file.",
    )
    parser.add_argument(
        "--ha-url",
        default=DEFAULT_HA_URL,
        help="Home Assistant base URL. Can also be set with AMARAN_HA_URL.",
    )
    parser.add_argument(
        "--ssh-host",
        default=DEFAULT_SSH_HOST,
        help="Home Assistant SSH host for restart cycles. Can also be set with AMARAN_HA_SSH_HOST.",
    )
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument(
        "--ssh-key",
        default=DEFAULT_SSH_KEY,
        help="SSH private key path for restart cycles. Can also be set with AMARAN_HA_SSH_KEY.",
    )
    parser.add_argument("--ssh-timeout", type=int, default=10)
    parser.add_argument("--restart-timeout", type=int, default=180)
    parser.add_argument("--api-timeout", type=int, default=180)
    parser.add_argument("--ready-timeout", type=int, default=600)
    parser.add_argument("--service-timeout", type=int, default=45)
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--settle", type=float, default=3.0)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--cycle-gap", type=float, default=20.0)
    parser.add_argument("--test-brightness", type=int, default=128)
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--ensure-control-on", action="store_true")
    parser.add_argument("--control-switch", default="switch.amaran_ble_control")
    parser.add_argument("--output")
    parser.add_argument("--entities", nargs="+", default=list(DEFAULT_ENTITIES))
    args = parser.parse_args()
    args.entities = tuple(args.entities)

    if not args.ha_url:
        parser.error("--ha-url or AMARAN_HA_URL is required")
    if not args.no_restart:
        if not args.ssh_host:
            parser.error("--ssh-host or AMARAN_HA_SSH_HOST is required for restart cycles")
        if not args.ssh_key:
            parser.error("--ssh-key or AMARAN_HA_SSH_KEY is required for restart cycles")

    try:
        token = resolve_token(args.qr, args.token_env)
    except RuntimeError as err:
        parser.error(str(err))
    ha = HomeAssistantApi(args.ha_url, token)
    results: list[dict[str, Any]] = []

    try:
        for cycle_index in range(1, args.cycles + 1):
            results.append(run_cycle(args, ha, cycle_index))
            if cycle_index != args.cycles:
                print(f"Waiting {args.cycle_gap:.0f}s before next cycle...", flush=True)
                time.sleep(args.cycle_gap)
    finally:
        try:
            ha.set_amaran_log_level("info")
        except Exception:
            pass

    output = {
        "started_at": results[0]["started_at"] if results else now_label(),
        "finished_at": now_label(),
        "cycles": results,
    }
    output_path = args.output
    if not output_path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = f"amaran-reboot-stability-{stamp}.json"
    Path(output_path).write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nSaved result: {output_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
