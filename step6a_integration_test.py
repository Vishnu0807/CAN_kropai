"""
STEP 6A -- Simulated Multi-Message Integration Test

Realistic scenario on one virtual bus:
  - Three "fake boards" (relay, sensor, led) running in background threads.
  - Sensor board pushes unsolicited telemetry every second, unprompted.
  - Relay board responds to commands with a CHECK_RESPONSE ack.
  - LED board responds to commands with a CHECK_RESPONSE ack.
  - Host (SafeCanBridge) sends a handful of mixed commands to different
    boards, while a separate listener thread is picking up the sensor's
    unsolicited telemetry at the same time.

Goal: prove send_command()'s ack-waiting and listen_once()'s passive
listening can coexist on the same bus without one starving or corrupting
the other, and that everything decodes correctly under mixed traffic.

Depends on all previous step files being in the same folder, plus
python-can (pip install python-can).
"""

import time
import threading

import can

from can_utilities import build_can_id, Priority, BoardType, Opcode
from step4_error_handling import SafeCanBridge, safe_decode

CHANNEL = "step6a_test_channel"


# ---------------------------------------------------------------------------
# Fake board simulators (run in background threads, act like real STM32 nodes)
# ---------------------------------------------------------------------------

def fake_relay_board(stop_event):
    bus = can.interface.Bus(channel=CHANNEL, bustype="virtual")
    node_id = 1
    while not stop_event.is_set():
        msg = bus.recv(timeout=0.5)
        if msg is None:
            continue
        _, board_type, target_node = _decode_id_quiet(msg.arbitration_id)
        if board_type != BoardType.RELAY or target_node not in (node_id, 0x7F):
            continue
        # Respond to any relay command with an ACK (status_mask = fixed dummy value)
        ack_id = build_can_id(Priority.NORMAL, BoardType.RELAY, node_id)
        ack = can.Message(arbitration_id=ack_id,
                           data=bytes([Opcode.CHECK_RESPONSE, 0b00000101]),
                           is_extended_id=False)
        bus.send(ack)
    bus.shutdown()


def fake_led_board(stop_event):
    bus = can.interface.Bus(channel=CHANNEL, bustype="virtual")
    node_id = 1
    while not stop_event.is_set():
        msg = bus.recv(timeout=0.5)
        if msg is None:
            continue
        _, board_type, target_node = _decode_id_quiet(msg.arbitration_id)
        if board_type != BoardType.LED or target_node not in (node_id, 0x7F):
            continue
        ack_id = build_can_id(Priority.NORMAL, BoardType.LED, node_id)
        ack = can.Message(arbitration_id=ack_id,
                           data=bytes([Opcode.CHECK_RESPONSE, 0x01]),
                           is_extended_id=False)
        bus.send(ack)
    bus.shutdown()


def fake_sensor_board(stop_event):
    """Pushes unsolicited telemetry every ~0.4s, unrelated to any command."""
    bus = can.interface.Bus(channel=CHANNEL, bustype="virtual")
    node_id = 2
    sensor_id = build_can_id(Priority.NORMAL, BoardType.SENSOR, node_id)
    count = 0
    while not stop_event.is_set():
        payload = bytes([Opcode.SENSOR_LIQUID, 65 + (count % 5), 0xF4, 0x09, 0x00, 0x78, 0x00, 0x00])
        bus.send(can.Message(arbitration_id=sensor_id, data=payload, is_extended_id=False))
        count += 1
        time.sleep(0.4)
    bus.shutdown()


def _decode_id_quiet(can_id):
    from can_utilities import decode_can_id
    return decode_can_id(can_id)


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def main():
    stop_event = threading.Event()

    threads = [
        threading.Thread(target=fake_relay_board, args=(stop_event,), daemon=True),
        threading.Thread(target=fake_led_board, args=(stop_event,), daemon=True),
        threading.Thread(target=fake_sensor_board, args=(stop_event,), daemon=True),
    ]
    for t in threads:
        t.start()

    time.sleep(0.3)  # let fake boards spin up

    host = SafeCanBridge(channel=CHANNEL, bustype="virtual")
    assert host.is_ready()

    telemetry_seen = []
    telemetry_stop = threading.Event()

    def telemetry_listener():
        # Separate listener socket, purely passive -- proves it doesn't
        # collide with send_command()'s own recv() calls on the host side.
        bus = can.interface.Bus(channel=CHANNEL, bustype="virtual")
        while not telemetry_stop.is_set():
            msg = bus.recv(timeout=0.3)
            if msg is None:
                continue
            decoded = safe_decode(msg.arbitration_id, msg.data)
            if decoded.get("board") == "sensor":
                telemetry_seen.append(decoded)
        bus.shutdown()

    listener_thread = threading.Thread(target=telemetry_listener, daemon=True)
    listener_thread.start()

    print("--- Sending mixed commands while telemetry streams in background ---\n")

    commands = [
        {"board": "relay", "node_id": 1, "action": "set_relay", "params": {"relay_mask": 3}},
        {"board": "led", "node_id": 1, "action": "set_level", "params": {"brightness": 120}},
        {"board": "relay", "node_id": 1, "action": "dose_pair",
         "params": {"pair": "1_2", "vol_a": 2.0, "vol_b": 1.5}},
        {"board": "led", "node_id": 1, "action": "set_level", "params": {"brightness": 255}},
        {"board": "relay", "node_id": 99, "action": "set_relay", "params": {"relay_mask": 1}},  # wrong node -> expect timeout
    ]

    results = []
    for cmd in commands:
        r = host.send_command(cmd)
        results.append(r)
        print(f"CMD  {cmd}")
        print(f"  -> ack_status={r.get('ack_status')}  ok={r['ok']}\n")
        time.sleep(0.2)

    time.sleep(1.0)  # let a bit more telemetry accumulate

    stop_event.set()
    telemetry_stop.set()
    host.close()
    time.sleep(0.3)

    print(f"--- Unsolicited sensor telemetry received during test: {len(telemetry_seen)} frames ---")
    for t in telemetry_seen[:3]:
        print(" ", t)
    if len(telemetry_seen) > 3:
        print(f"  ... and {len(telemetry_seen) - 3} more")

    # ---- Assertions ----
    assert results[0]["ack_status"] == "ack", "relay set_relay should have ack'd"
    assert results[1]["ack_status"] == "ack", "led set_level (1) should have ack'd"
    assert results[2]["ack_status"] == "ack", "relay dose_pair should have ack'd"
    assert results[3]["ack_status"] == "ack", "led set_level (2) should have ack'd"
    assert results[4]["ack_status"] == "timeout", "wrong node_id should NOT get an ack"
    assert len(telemetry_seen) > 0, "should have received unsolicited sensor telemetry"

    print("\nALL STEP 6A ASSERTIONS PASSED -- commands, acks, and independent")
    print("telemetry streaming all worked correctly on the same shared bus.")


if __name__ == "__main__":
    main()
