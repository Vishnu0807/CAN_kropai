"""
STEP 3 -- CAN -> JSON Decoder

Takes a raw incoming CAN frame (arbitration_id, data bytes) and converts it
into a clean JSON-serializable dict -- telemetry, relay status, or an ACK
(via the CHECK_RESPONSE opcode, per the Step 1 ACK decision).

Depends on:
  - can_utilities.py   (decode_can_id, decode_payload -- repo's existing logic)
  - step1_schema.py    (board vocabulary, ACK_OPCODE)
"""

from can_utilities import decode_can_id, decode_payload, Opcode
from step1_schema import BOARD_TYPE_TO_NAME, ACK_OPCODE


class CanDecodeError(ValueError):
    """Raised when a frame can't be decoded. Caught properly in Step 4."""
    pass


def can_to_json(can_id: int, data: bytes) -> dict:
    """
    Convert one raw CAN frame into a JSON-serializable dict.

    Returns one of two shapes:
      - ACK/status frame (opcode == CHECK_RESPONSE):
        {"board": ..., "node_id": ..., "priority": ..., "status": "ack",
         "status_mask": "0x..", "bits": [...], ...}
      - Telemetry/other frame:
        {"board": ..., "node_id": ..., "priority": ..., **decode_payload() output}

    Raises CanDecodeError on malformed/unparseable frames -- caller (Step 4)
    decides whether to log, retry, or surface upstream.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 1:
        raise CanDecodeError(f"Empty or invalid payload for CAN ID {can_id:#05x}")

    try:
        is_emergency, board_type, node_id = decode_can_id(can_id)
    except Exception as e:
        raise CanDecodeError(f"Failed to decode CAN ID {can_id:#05x}: {e}")

    board_name = BOARD_TYPE_TO_NAME.get(board_type, f"unknown_{board_type}")

    try:
        decoded_payload = decode_payload(bytes(data))
    except Exception as e:
        raise CanDecodeError(
            f"Failed to decode payload for board={board_name} node={node_id} "
            f"data={bytes(data).hex()}: {e}"
        )

    result = {
        "board": board_name,
        "node_id": node_id,
        "priority": "emergency" if is_emergency else "normal",
    }

    opcode = data[0]
    if opcode == ACK_OPCODE:
        result.update(_format_ack(decoded_payload))
    else:
        result.update(decoded_payload)

    return result


def _format_ack(decoded_payload: dict) -> dict:
    """
    decode_payload() already turns CHECK_RESPONSE frames into:
    {"type": "check_response", "opcode": 2, "status_mask": "0x..",
     "bits": [...], possibly "extra_mask"/"extra_bits"}

    We relabel this as an ACK per the Step 1 decision (soft ACK via
    CHECK_RESPONSE), while keeping all original fields for anyone who
    wants the raw bitmask too.
    """
    return {
        "status": "ack",
        **decoded_payload,
    }


def is_ack_frame(can_id: int, data: bytes) -> bool:
    """Quick check without full decode -- useful for Step 5's ack-matching logic."""
    if not data:
        return False
    return data[0] == ACK_OPCODE


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from can_utilities import build_can_id, Priority, BoardType

    print("--- Valid frames ---")

    # Liquid sensor reading
    fid = build_can_id(Priority.NORMAL, BoardType.SENSOR, 3)
    fdata = bytes([Opcode.SENSOR_LIQUID, 68, 0xF4, 0x09, 0x00, 0x78, 0x00, 0x00])
    print(can_to_json(fid, fdata))

    # Env sensor reading
    fid = build_can_id(Priority.NORMAL, BoardType.SENSOR, 3)
    fdata = bytes([Opcode.SENSOR_ENV, 0x1E, 0x00, 0x58, 0x02, 0x10, 0x27])
    print(can_to_json(fid, fdata))

    # Relay periodic status
    fid = build_can_id(Priority.NORMAL, BoardType.RELAY, 1)
    fdata = bytes([Opcode.RELAY_STATE, 0b00010101])
    print(can_to_json(fid, fdata))

    # ACK / CHECK_RESPONSE frame (e.g. after a set_relay command)
    fid = build_can_id(Priority.NORMAL, BoardType.RELAY, 1)
    fdata = bytes([Opcode.CHECK_RESPONSE, 0b00010101, 0b00000011])
    result = can_to_json(fid, fdata)
    print(result)
    print(f"  is_ack_frame -> {is_ack_frame(fid, fdata)}")

    # Emergency frame
    fid = build_can_id(Priority.EMERGENCY, BoardType.LED, 1)
    fdata = bytes([Opcode.CHECK_NODE])
    print(can_to_json(fid, fdata))

    print("\n--- Invalid frames (should raise CanDecodeError) ---")
    bad_frames = [
        (0x999, b""),                          # empty payload
        (0x999, bytes([0x99, 0x01, 0x02])),     # unknown opcode -> decode_payload returns "unknown" type, not an error actually
        (build_can_id(Priority.NORMAL, BoardType.SENSOR, 1), bytes([Opcode.SENSOR_LIQUID, 0x01])),  # too short for liquid payload
    ]
    for fid, fdata in bad_frames:
        try:
            r = can_to_json(fid, fdata)
            print(f"NOTE (not an error, decode_payload tolerated it): ID={fid:#05x} DATA={fdata.hex()} -> {r}")
        except CanDecodeError as e:
            print(f"OK - correctly rejected: ID={fid:#05x} DATA={fdata.hex()} -> {e}")
