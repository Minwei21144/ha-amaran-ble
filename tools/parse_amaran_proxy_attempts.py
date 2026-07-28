"""Parse ESPHome Bluetooth Proxy logs into one row per BLE attempt."""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path


TIME_RE = re.compile(r"^\[(?P<time>\d{2}:\d{2}:\d{2})\]")
START_RE = re.compile(
    r"\[(?P<slot>\d+)\] "
    r"\[(?P<address>(?:[0-9A-F]{2}:){5}[0-9A-F]{2})\] "
    r"Connecting v3 (?P<cache>with|without) cache"
)
DEVICE_EVENT_RE = re.compile(
    r"\[(?P<slot>\d+)\] "
    r"\[(?P<address>(?:[0-9A-F]{2}:){5}[0-9A-F]{2})\]"
)
MTU_OK_RE = re.compile(r"cfg_mtu status (?P<status>\d+), mtu (?P<mtu>\d+)")
MTU_ERROR_RE = re.compile(
    r"cfg_mtu failed, mtu (?P<mtu>\d+), status (?P<status>\d+)"
)
DESCRIPTOR_ERROR_RE = re.compile(
    r"Error writing char/descriptor for handle (?P<handle>0x[0-9A-Fa-f]+), "
    r"status=(?P<status>\d+)"
)
DISCONNECT_RE = re.compile(r"Disconnect, reason=(?P<reason>0x[0-9A-Fa-f]+)")
CLOSE_RE = re.compile(
    r"Close, reason=(?P<reason>0x[0-9A-Fa-f]+), freeing slot"
)
OPEN_ERROR_RE = re.compile(r"Connection open error, status=(?P<status>\d+)")


def _seconds(value: str) -> int:
    hour, minute, second = (int(part) for part in value.split(":"))
    return hour * 3600 + minute * 60 + second


def _elapsed(start: int, event: int | None) -> str:
    return "" if event is None else str(event - start)


@dataclass
class Attempt:
    number: int
    slot: int
    start_text: str
    start_seconds: int
    address: str
    cache: str
    version: str = ""
    phase: str = ""
    physical_open_seconds: int | None = None
    open_seconds: int | None = None
    service_seconds: int | None = None
    mtu_seconds: int | None = None
    mtu_status: int | None = None
    mtu: int | None = None
    descriptor_seconds: int | None = None
    descriptor_handle: str = ""
    descriptor_status: int | None = None
    open_error_status: int | None = None
    disconnect_scheduled_seconds: int | None = None
    disconnect_reason: str = ""
    close_reason: str = ""
    end_seconds: int | None = None
    notes: list[str] = field(default_factory=list)

    def outcome(self, ready_threshold: int) -> str:
        if self.open_seconds is None:
            if self.physical_open_seconds is not None and self.close_reason == "0x00":
                return "late_physical_open_cleanup"
            if self.open_error_status is not None or self.disconnect_reason:
                return "gatt_open_failed"
            return "incomplete"
        if self.descriptor_status is not None:
            return "cccd_descriptor_failed"
        if self.mtu_status not in (None, 0):
            return "mtu_failed"
        reason = self.close_reason or self.disconnect_reason
        if reason and reason.lower() != "0x00":
            return "post_open_disconnected"
        duration = None
        if self.end_seconds is not None:
            duration = self.end_seconds - self.start_seconds
        if reason.lower() == "0x00" and duration is not None:
            if duration >= ready_threshold:
                return "long_normal_session"
            return "short_normal_cleanup"
        return "open_without_terminal_event"

    def row(self, ready_threshold: int) -> dict[str, object]:
        return {
            "attempt": self.number,
            "slot": self.slot,
            "version": self.version,
            "phase": self.phase,
            "start": self.start_text,
            "address": self.address,
            "actual_cache": self.cache,
            "physical_open_s": _elapsed(
                self.start_seconds, self.physical_open_seconds
            ),
            "open_s": _elapsed(self.start_seconds, self.open_seconds),
            "service_discovery_s": _elapsed(
                self.start_seconds, self.service_seconds
            ),
            "mtu_s": _elapsed(self.start_seconds, self.mtu_seconds),
            "mtu_status": "" if self.mtu_status is None else self.mtu_status,
            "mtu": "" if self.mtu is None else self.mtu,
            "descriptor_s": _elapsed(
                self.start_seconds, self.descriptor_seconds
            ),
            "descriptor_handle": self.descriptor_handle,
            "descriptor_status": (
                "" if self.descriptor_status is None else self.descriptor_status
            ),
            "open_error_status": (
                "" if self.open_error_status is None else self.open_error_status
            ),
            "disconnect_scheduled_s": _elapsed(
                self.start_seconds, self.disconnect_scheduled_seconds
            ),
            "late_open_after_disconnect_schedule": (
                (self.physical_open_seconds or self.open_seconds) is not None
                and self.disconnect_scheduled_seconds is not None
                and (self.physical_open_seconds or self.open_seconds)
                >= self.disconnect_scheduled_seconds
            ),
            "disconnect_reason": self.disconnect_reason,
            "close_reason": self.close_reason,
            "slot_s": _elapsed(self.start_seconds, self.end_seconds),
            "outcome": self.outcome(ready_threshold),
            "notes": "; ".join(self.notes),
        }


def _parse_segments(values: list[str]) -> list[tuple[int, int, str]]:
    segments: list[tuple[int, int, str]] = []
    for value in values:
        range_text, separator, label = value.partition("=")
        if not separator or not label:
            raise ValueError(f"Invalid segment {value!r}; expected START-END=LABEL")
        start_text, separator, end_text = range_text.partition("-")
        if not separator:
            raise ValueError(f"Invalid segment range {range_text!r}")
        segments.append((int(start_text), int(end_text), label))
    return segments


def _label_for(number: int, segments: list[tuple[int, int, str]]) -> str:
    for start, end, label in segments:
        if start <= number <= end:
            return label
    return ""


def parse_log(
    path: Path,
    version_segments: list[tuple[int, int, str]],
    phase_segments: list[tuple[int, int, str]],
) -> list[Attempt]:
    attempts: list[Attempt] = []
    active_by_slot: dict[int, Attempt] = {}
    previous_clock: int | None = None
    day_offset = 0

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        time_match = TIME_RE.match(raw_line)
        if time_match is None:
            continue
        clock = _seconds(time_match.group("time"))
        if previous_clock is not None and clock + day_offset < previous_clock - 43200:
            day_offset += 86400
        event_seconds = clock + day_offset
        previous_clock = event_seconds

        start_match = START_RE.search(raw_line)
        if start_match is not None:
            slot = int(start_match.group("slot"))
            previous = active_by_slot.get(slot)
            if previous is not None and previous.end_seconds is None:
                previous.end_seconds = event_seconds
                previous.notes.append("ended_at_slot_reuse")
            number = len(attempts) + 1
            current = Attempt(
                number=number,
                slot=slot,
                start_text=time_match.group("time"),
                start_seconds=event_seconds,
                address=start_match.group("address"),
                cache=start_match.group("cache"),
                version=_label_for(number, version_segments),
                phase=_label_for(number, phase_segments),
            )
            attempts.append(current)
            active_by_slot[slot] = current
            continue

        device_match = DEVICE_EVENT_RE.search(raw_line)
        if device_match is None:
            continue
        slot = int(device_match.group("slot"))
        current = active_by_slot.get(slot)
        if (
            current is None
            or current.address != device_match.group("address")
        ):
            continue

        if "ESP_GATTC_CONNECT_EVT" in raw_line:
            current.physical_open_seconds = (
                current.physical_open_seconds or event_seconds
            )
        if "Connection open" in raw_line and "error" not in raw_line:
            current.open_seconds = current.open_seconds or event_seconds
        if "Service discovery complete" in raw_line:
            current.service_seconds = current.service_seconds or event_seconds

        if match := MTU_OK_RE.search(raw_line):
            current.mtu_seconds = current.mtu_seconds or event_seconds
            current.mtu_status = int(match.group("status"))
            current.mtu = int(match.group("mtu"))
        elif match := MTU_ERROR_RE.search(raw_line):
            current.mtu_seconds = current.mtu_seconds or event_seconds
            current.mtu_status = int(match.group("status"))
            current.mtu = int(match.group("mtu"))

        if match := DESCRIPTOR_ERROR_RE.search(raw_line):
            current.descriptor_seconds = event_seconds
            current.descriptor_handle = match.group("handle").lower()
            current.descriptor_status = int(match.group("status"))
        if match := OPEN_ERROR_RE.search(raw_line):
            current.open_error_status = int(match.group("status"))
        if "Disconnect before connected, disconnect scheduled" in raw_line:
            current.disconnect_scheduled_seconds = event_seconds
        if (
            (current.physical_open_seconds or current.open_seconds) is not None
            and current.disconnect_scheduled_seconds is not None
            and (current.physical_open_seconds or current.open_seconds)
            >= current.disconnect_scheduled_seconds
            and "late_open_after_disconnect_schedule" not in current.notes
        ):
            current.notes.append("late_open_after_disconnect_schedule")
        if match := DISCONNECT_RE.search(raw_line):
            current.disconnect_reason = match.group("reason").lower()
            current.end_seconds = current.end_seconds or event_seconds
        if match := CLOSE_RE.search(raw_line):
            current.close_reason = match.group("reason").lower()
            current.end_seconds = event_seconds

    return attempts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--version-segment",
        "--segment",
        dest="version_segment",
        action="append",
        default=[],
        help="Label an inclusive attempt range with a version: START-END=LABEL",
    )
    parser.add_argument(
        "--phase-segment",
        action="append",
        default=[],
        help="Label an inclusive attempt range with a test phase: START-END=LABEL",
    )
    parser.add_argument(
        "--ready-threshold",
        type=int,
        default=50,
        help="Seconds used to distinguish a long normal session from cleanup",
    )
    args = parser.parse_args()

    version_segments = _parse_segments(args.version_segment)
    phase_segments = _parse_segments(args.phase_segment)
    attempts = parse_log(args.log, version_segments, phase_segments)
    rows = [attempt.row(args.ready_threshold) for attempt in attempts]
    if not rows:
        raise SystemExit("No 'Connecting v3' attempts found")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Parsed {len(rows)} attempts into {args.output}")


if __name__ == "__main__":
    main()
