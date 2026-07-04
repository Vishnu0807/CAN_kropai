"""
STEP 1 — Schema & Protocol Setup

Defines the JSON contract and protocol constants used by every later step.
No CAN bus, no encoding/decoding logic here yet — just the shared vocabulary.

Depends on: can_utilities.py (must be in the same folder — copy it from the
KropAI-firmware-test-v3 repo root).
"""

from enum import IntEnum
from can_utilities import BoardType, Opcode, SUBGROUP_BROADCAST  # sanity-check import works


# ---------------------------------------------------------------------------
# 1. Board / action vocabulary (JSON-facing names -> internal enums)
# ---------------------------------------------------------------------------

BOARD_NAME_TO_TYPE = {
    "main": BoardType.MAIN,
    "relay": BoardType.RELAY,
    "led": BoardType.LED,
    "sensor": BoardType.SENSOR,
}
BOARD_TYPE_TO_NAME = {v: k for k, v in BOARD_NAME_TO_TYPE.items()}

# Actions supported per board (JSON "action" field).
# This is the contract Calvin should confirm before Step 2 encoding is written.
SUPPORTED_ACTIONS = {
    "relay": ["set_relay", "dose_pair", "check_node", "restart"],
    "led": ["set_level", "check_node", "restart"],
    "sensor": ["check_node", "restart"],
    "main": ["check_node", "restart"],
}


# ---------------------------------------------------------------------------
# 2. Response / status classification (for Step 3 decoding + ACK handling)
# ---------------------------------------------------------------------------

class ResponseStatus(IntEnum):
    ACK = 0          # command received and executed successfully
    NACK = 1         # command received but rejected/invalid
    TIMEOUT = 2       # no response received within expected window (host-side only)
    DECODE_ERROR = 3  # frame received but couldn't be parsed


# ACK decision: reuse existing CHECK_RESPONSE (0x02) opcode as the ACK signal
# instead of adding a new opcode to firmware. Board already sends this;
# decode_payload() already parses it into status_mask + bits.
#
# Flow: host sends command, waits ACK_TIMEOUT_SECONDS, and if a
# CHECK_RESPONSE frame arrives from the same board_type + node_id within
# that window, treats it as ACK, otherwise TIMEOUT.
#
# This is a soft ACK (implicit, via next status frame) rather than a
# dedicated per-command guarantee. Only flag to Calvin if this proves
# unreliable in real testing - a hard guarantee would need a new opcode
# or sequence number added to firmware.

ACK_OPCODE = Opcode.CHECK_RESPONSE
ACK_TIMEOUT_SECONDS = 2.0


# ---------------------------------------------------------------------------
# 3. JSON schema — documented as dict templates (not enforced yet, Step 4 does that)
# ---------------------------------------------------------------------------

# Outgoing command (JSON -> CAN)
COMMAND_SCHEMA_EXAMPLE = {
    "board": "led",                # required, one of BOARD_NAME_TO_TYPE keys
    "node_id": 1,                  # required, int 0-126, or "broadcast"
    "action": "set_level",         # required, must be in SUPPORTED_ACTIONS[board]
    "params": {"brightness": 180}, # required if action needs data, else {}
    "priority": "normal",          # optional, "normal" | "emergency", default "normal"
}

# Incoming telemetry/ack (CAN -> JSON)
TELEMETRY_SCHEMA_EXAMPLE = {
    "board": "sensor",
    "node_id": 3,
    "priority": "normal",
    "type": "liquid",              # from decode_payload()'s existing "type" field
    "ph": 6.8,
    "orp": 254.8,
    "ec": 12.0,
}

ACK_SCHEMA_EXAMPLE = {
    "board": "relay",
    "node_id": 1,
    "status": "ack",                # "ack" | "nack" | "timeout" | "decode_error"
    "in_response_to": "set_relay",  # best-effort, may be null if unknown
}

# Error response shape returned by our own functions on failure (Step 4 will build this out)
ERROR_SCHEMA_EXAMPLE = {
    "error": True,
    "message": "Unknown board type: pump",
    "input": {"board": "pump", "node_id": 1, "action": "set_level"},
}


# ---------------------------------------------------------------------------
# 4. Quick self-check — confirms opcode table still matches what we assume
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Boards recognized:", list(BOARD_NAME_TO_TYPE.keys()))
    print("Supported actions:", SUPPORTED_ACTIONS)
    print("\nOpcode table currently in can_utilities.py:")
    for op in Opcode:
        print(f"  {op.name:<20} = {hex(op.value)}")
    print(f"\nBroadcast subgroup value: {hex(SUBGROUP_BROADCAST)}")
