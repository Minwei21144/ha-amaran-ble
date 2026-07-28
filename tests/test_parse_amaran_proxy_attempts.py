from pathlib import Path

from tools.parse_amaran_proxy_attempts import parse_log


def test_parse_log_tracks_overlapping_proxy_slots(tmp_path: Path) -> None:
    log = tmp_path / "proxy.log"
    log.write_text(
        "\n".join(
            (
                "[10:00:00][I][bluetooth_proxy:059]: [0] [02:00:00:00:00:01] Connecting v3 with cache",
                "[10:00:45][D][esp32_ble_client:157]: [0] [02:00:00:00:00:01] Disconnect before connected, disconnect scheduled",
                "[10:01:00][I][bluetooth_proxy:059]: [1] [02:00:00:00:00:02] Connecting v3 with cache",
                "[10:01:10][D][esp32_ble_client:212]: [0] [02:00:00:00:00:01] ESP_GATTC_CONNECT_EVT",
                "[10:01:10][I][esp32_ble_client:344]: [0] [02:00:00:00:00:01] Connection open",
                "[10:01:10][D][bluetooth_proxy.connection:154]: [0] [02:00:00:00:00:01] Close, reason=0x00, freeing slot",
                "[10:01:20][D][bluetooth_proxy.connection:383]: [1] [02:00:00:00:00:02] Disconnect, reason=0x100",
                "[10:01:20][W][esp32_ble_client:224]: [1] [02:00:00:00:00:02] Connection open error, status=133",
            )
        ),
        encoding="utf-8",
    )

    attempts = parse_log(log, [], [])

    assert len(attempts) == 2
    first, second = attempts
    assert first.slot == 0
    assert first.physical_open_seconds - first.start_seconds == 70
    assert first.open_seconds - first.start_seconds == 70
    assert first.disconnect_scheduled_seconds - first.start_seconds == 45
    assert first.close_reason == "0x00"
    assert first.outcome(50) == "long_normal_session"
    assert "late_open_after_disconnect_schedule" in first.notes
    assert second.slot == 1
    assert second.disconnect_reason == "0x100"
    assert second.open_error_status == 133
    assert second.outcome(50) == "gatt_open_failed"


def test_parse_log_classifies_late_physical_open_cleanup(tmp_path: Path) -> None:
    log = tmp_path / "proxy.log"
    log.write_text(
        "\n".join(
            (
                "[10:00:00][I][bluetooth_proxy:059]: [0] [02:00:00:00:00:01] Connecting v3 with cache",
                "[10:00:45][D][esp32_ble_client:157]: [0] [02:00:00:00:00:01] Disconnect before connected, disconnect scheduled",
                "[10:01:18][D][esp32_ble_client:212]: [0] [02:00:00:00:00:01] ESP_GATTC_CONNECT_EVT",
                "[10:01:18][D][bluetooth_proxy.connection:154]: [0] [02:00:00:00:00:01] Close, reason=0x00, freeing slot",
            )
        ),
        encoding="utf-8",
    )

    attempt = parse_log(log, [], [])[0]

    assert attempt.physical_open_seconds - attempt.start_seconds == 78
    assert attempt.open_seconds is None
    assert attempt.outcome(50) == "late_physical_open_cleanup"
    assert "late_open_after_disconnect_schedule" in attempt.notes


def test_parse_log_only_ends_attempt_when_its_slot_is_reused(tmp_path: Path) -> None:
    log = tmp_path / "proxy.log"
    log.write_text(
        "\n".join(
            (
                "[10:00:00][I][bluetooth_proxy:059]: [0] [02:00:00:00:00:01] Connecting v3 with cache",
                "[10:00:05][I][bluetooth_proxy:059]: [1] [02:00:00:00:00:02] Connecting v3 with cache",
                "[10:00:10][I][bluetooth_proxy:059]: [0] [02:00:00:00:00:01] Connecting v3 with cache",
            )
        ),
        encoding="utf-8",
    )

    attempts = parse_log(log, [], [])

    assert len(attempts) == 3
    assert attempts[0].end_seconds - attempts[0].start_seconds == 10
    assert attempts[0].notes == ["ended_at_slot_reuse"]
    assert attempts[1].end_seconds is None
    assert attempts[2].slot == 0
