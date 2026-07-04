"""
STEP 5 -- Bus Integration (python-can)

Wires the Step 2 encoder and Step 3 decoder to an actual CAN bus using
python-can. Supports:
  - "virtual" bustype -> in-process fake bus, needs ZERO hardware, good for
    today's testing.
  - "socketcan" -> real Linux CAN interface (can0) once boards/adapter exist.
  - "slcan" -> USB-CAN adapters like the one in the KropAI setup.

Depends on: can_utilities.py, step1_schema.py, step2_encoder.py,
step3_decoder.py (all in the same folder), and the `python-can` package
(pip install python-can).
"""

import time
import can

from step1_schema import ACK_TIMEOUT_SECONDS
from step2_encoder import json_to_can, JsonCanError
from step3_decoder import can_to_json, is_ack_frame, CanDecodeError


class CanBridge:
    """
    High-level JSON <-> CAN bridge over a real python-can Bus.

    Usage:
        bridge = CanBridge(channel="can0", bustype="socketcan")
        result = bridge.send_command({"board": "led", "node_id": 1,
                                       "action": "set_level",
                                       "params": {"brightness": 180}})
        print(result)   # {"sent": {...}, "ack": {...} or None}

        for telemetry in bridge.listen():
            print(telemetry)
    """

    def __init__(self, channel="vcan0", bustype="virtual"):
        self.bus = can.interface.Bus(channel=channel, bustype=bustype)

    def send_command(self, command: dict, wait_for_ack: bool = True) -> dict:
        """
        Encode + send a JSON command. Optionally waits ACK_TIMEOUT_SECONDS
        for a CHECK_RESPONSE frame from the same board+node as a soft ACK.

        Returns:
            {"sent": {"can_id": ..., "data": "hex"}, "ack": {...} | None,
             "error": None}
          or, on failure:
            {"sent": None, "ack": None, "error": "message"}
        """
        try:
            can_id, data = json_to_can(command)
        except JsonCanError as e:
            return {"sent": None, "ack": None, "error": str(e)}

        msg = can.Message(arbitration_id=can_id, data=data, is_extended_id=False)

        try:
            self.bus.send(msg)
        except can.CanError as e:
            return {"sent": None, "ack": None, "error": f"Bus send failed: {e}"}

        result = {
            "sent": {"can_id": hex(can_id), "data": data.hex()},
            "ack": None,
            "error": None,
        }

        if wait_for_ack:
            result["ack"] = self._wait_for_ack(can_id)

        return result

    def _wait_for_ack(self, sent_can_id: int, timeout: float = ACK_TIMEOUT_SECONDS):
        """
        Listens for a CHECK_RESPONSE frame from the same board_type+node_id
        as the command we just sent. Returns decoded ACK dict, or a
        {"status": "timeout"} dict if nothing arrives in time.
        """
        from can_utilities import decode_can_id
        _, sent_board_type, sent_node_id = decode_can_id(sent_can_id)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = self.bus.recv(timeout=max(remaining, 0))
            if msg is None:
                break
            if not is_ack_frame(msg.arbitration_id, msg.data):
                continue
            _, board_type, node_id = decode_can_id(msg.arbitration_id)
            if board_type == sent_board_type and node_id == sent_node_id:
                try:
                    return can_to_json(msg.arbitration_id, msg.data)
                except CanDecodeError as e:
                    return {"status": "decode_error", "error": str(e)}

        return {"status": "timeout"}

    def listen(self, timeout=1.0):
        """
        Generator yielding decoded JSON for every incoming frame.
        Use this for a standalone telemetry logger (separate from
        send_command's ack-waiting loop).
        """
        while True:
            msg = self.bus.recv(timeout=timeout)
            if msg is None:
                continue
            try:
                yield can_to_json(msg.arbitration_id, msg.data)
            except CanDecodeError as e:
                yield {"error": str(e), "raw_id": hex(msg.arbitration_id),
                       "raw_data": msg.data.hex()}

    def close(self):
        self.bus.shutdown()


# ---------------------------------------------------------------------------
# Self-test using python-can's built-in VIRTUAL bus -- no hardware needed.
# Two bridge instances on the same virtual channel simulate "host" and
# "board" talking to each other.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from can_utilities import build_can_id, Priority, BoardType, Opcode

    CHANNEL = "test_channel_1"

    host = CanBridge(channel=CHANNEL, bustype="virtual")
    fake_board = CanBridge(channel=CHANNEL, bustype="virtual")

    print("--- Test 1: send command, no board replies -> expect timeout ---")
    result = host.send_command(
        {"board": "led", "node_id": 1, "action": "set_level", "params": {"brightness": 180}},
        wait_for_ack=True,
    )
    print(result)
    assert result["ack"]["status"] == "timeout"
    print("PASS\n")

    print("--- Test 2: send command, fake board replies with ACK ---")
    import threading

    def fake_board_responder():
        # Wait for the command, then reply with a CHECK_RESPONSE (ACK)
        msg = fake_board.bus.recv(timeout=2.0)
        if msg is not None:
            ack_id = build_can_id(Priority.NORMAL, BoardType.LED, 1)
            ack_msg = can.Message(arbitration_id=ack_id,
                                   data=bytes([Opcode.CHECK_RESPONSE, 0x01]),
                                   is_extended_id=False)
            fake_board.bus.send(ack_msg)

    t = threading.Thread(target=fake_board_responder, daemon=True)
    t.start()

    result = host.send_command(
        {"board": "led", "node_id": 1, "action": "set_level", "params": {"brightness": 200}},
        wait_for_ack=True,
    )
    print(result)
    assert result["ack"]["status"] == "ack"
    print("PASS\n")

    print("--- Test 3: invalid command -> no send attempted ---")
    result = host.send_command({"board": "pump", "action": "set_level", "params": {}})
    print(result)
    assert result["error"] is not None
    print("PASS\n")

    host.close()
    fake_board.close()
    print("All Step 5 self-tests passed.")
