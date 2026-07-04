"""
STEP 2 -- Core JSON -> CAN Encoder

Takes a command dict (matching the schema from step1_schema.py) and
produces (can_id, data_bytes) ready to send on the bus.

Depends on:
  - can_utilities.py   (repo's existing ID/opcode logic)
  - step1_schema.py    (board/action vocabulary, ACK constants)
"""

from can_utilities import Priority, build_can_id, Opcode, SUBGROUP_BROADCAST
from step1_schema import BOARD_NAME_TO_TYPE, SUPPORTED_ACTIONS


class JsonCanError(ValueError):
    """Raised for any malformed or unsupported command. Caught properly in Step 4."""
    pass


def json_to_can(command: dict):
    """
    Convert a command dict into (can_id: int, data: bytes).

    Expected shape (see step1_schema.COMMAND_SCHEMA_EXAMPLE):
    {
        "board": "relay" | "led" | "sensor" | "main",
        "node_id": int (0-126) or "broadcast",
        "action": str,
        "params": {...},          # optional, defaults to {}
        "priority": "normal" | "emergency"   # optional, default "normal"
    }
    """
    _validate_top_level(command)

    board = command["board"].lower()
    action = command["action"].lower()
    params = command.get("params", {})

    if board not in BOARD_NAME_TO_TYPE:
        raise JsonCanError(f"Unknown board type: '{board}'. Must be one of {list(BOARD_NAME_TO_TYPE)}")

    if action not in SUPPORTED_ACTIONS.get(board, []):
        raise JsonCanError(
            f"Action '{action}' not supported for board '{board}'. "
            f"Supported: {SUPPORTED_ACTIONS.get(board)}"
        )

    board_type = BOARD_NAME_TO_TYPE[board]

    node_id = command.get("node_id", 0)
    node_id = _resolve_node_id(node_id)

    priority_field = command.get("priority", "normal").lower()
    if priority_field not in ("normal", "emergency"):
        raise JsonCanError(f"Invalid priority '{priority_field}', must be 'normal' or 'emergency'")
    priority = Priority.EMERGENCY if priority_field == "emergency" else Priority.NORMAL

    can_id = build_can_id(priority, board_type, node_id)
    data = _encode_action(board, action, params)

    return can_id, data


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_top_level(command: dict):
    if not isinstance(command, dict):
        raise JsonCanError(f"Command must be a dict/JSON object, got {type(command).__name__}")

    for field in ("board", "action"):
        if field not in command:
            raise JsonCanError(f"Missing required field: '{field}'")
        if not isinstance(command[field], str):
            raise JsonCanError(f"Field '{field}' must be a string")


def _resolve_node_id(node_id):
    if isinstance(node_id, str) and node_id.lower() == "broadcast":
        return SUBGROUP_BROADCAST
    if not isinstance(node_id, int):
        raise JsonCanError(f"node_id must be an int (0-126) or 'broadcast', got {node_id!r}")
    if not (0 <= node_id <= 126):
        raise JsonCanError(f"node_id must be between 0 and 126, got {node_id}")
    return node_id


# ---------------------------------------------------------------------------
# Per-board, per-action encoding
# ---------------------------------------------------------------------------

def _encode_action(board: str, action: str, params: dict) -> bytes:
    if board == "relay":
        return _encode_relay(action, params)
    if board == "led":
        return _encode_led(action, params)
    if board == "sensor":
        return _encode_generic(action, params)
    if board == "main":
        return _encode_generic(action, params)

    # Should be unreachable given earlier validation, but keep as a safety net.
    raise JsonCanError(f"No encoder implemented for board '{board}'")


def _encode_relay(action: str, params: dict) -> bytes:
    if action == "set_relay":
        mask = params.get("relay_mask")
        if mask is None:
            raise JsonCanError("relay 'set_relay' requires params.relay_mask")
        if not isinstance(mask, int) or not (0 <= mask <= 0xFF):
            raise JsonCanError(f"relay_mask must be an int 0-255, got {mask!r}")
        return bytes([Opcode.RELAY_SET, mask])

    if action == "dose_pair":
        pair = params.get("pair")
        vol_a = params.get("vol_a")
        vol_b = params.get("vol_b")

        if pair not in ("1_2", "3_4"):
            raise JsonCanError("dose_pair requires params.pair = '1_2' or '3_4'")
        if not isinstance(vol_a, (int, float)) or not isinstance(vol_b, (int, float)):
            raise JsonCanError("dose_pair requires numeric params.vol_a and params.vol_b (ml)")
        if vol_a < 0 or vol_b < 0 or vol_a > 655.35 or vol_b > 655.35:
            raise JsonCanError("dose_pair volumes must be between 0 and 655.35 ml (16-bit centi-ml limit)")

        opcode = Opcode.RELAY_PARASTATIC_12 if pair == "1_2" else Opcode.RELAY_PARASTATIC_34
        raw_a = int(round(vol_a * 100))
        raw_b = int(round(vol_b * 100))
        return bytes([
            opcode,
            raw_a & 0xFF, (raw_a >> 8) & 0xFF,
            raw_b & 0xFF, (raw_b >> 8) & 0xFF,
        ])

    return _encode_generic(action, params)


def _encode_led(action: str, params: dict) -> bytes:
    if action == "set_level":
        brightness = params.get("brightness")
        if brightness is None:
            raise JsonCanError("led 'set_level' requires params.brightness")
        if not isinstance(brightness, int) or not (0 <= brightness <= 255):
            raise JsonCanError(f"brightness must be an int 0-255, got {brightness!r}")
        return bytes([Opcode.LED_SET, brightness])

    return _encode_generic(action, params)


def _encode_generic(action: str, params: dict) -> bytes:
    """Shared opcodes valid across any board type."""
    if action == "check_node":
        return bytes([Opcode.CHECK_NODE])

    if action == "restart":
        return bytes([Opcode.REMOTE_RESTART, 0xA5])  # security key required by firmware

    raise JsonCanError(f"No encoder implemented for action '{action}'")


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        {"board": "led", "node_id": 1, "action": "set_level", "params": {"brightness": 180}},
        {"board": "relay", "node_id": 2, "action": "set_relay", "params": {"relay_mask": 0b00000101}},
        {"board": "relay", "node_id": 2, "action": "dose_pair", "params": {"pair": "1_2", "vol_a": 5.0, "vol_b": 3.0}},
        {"board": "sensor", "node_id": 3, "action": "check_node"},
        {"board": "relay", "node_id": "broadcast", "action": "restart"},
        {"board": "led", "node_id": 1, "action": "set_level", "priority": "emergency", "params": {"brightness": 0}},
    ]

    print("--- Valid commands ---")
    for cmd in tests:
        can_id, data = json_to_can(cmd)
        print(f"{cmd}")
        print(f"  -> ID={can_id:#05x}  DATA={data.hex()}\n")

    print("--- Invalid commands (should raise JsonCanError) ---")
    bad_tests = [
        {"board": "pump", "action": "set_level", "params": {}},
        {"board": "led", "action": "unknown_action", "params": {}},
        {"board": "led", "action": "set_level", "params": {"brightness": 999}},
        {"board": "relay", "node_id": 200, "action": "set_relay", "params": {"relay_mask": 1}},
        {"board": "relay", "action": "dose_pair", "params": {"pair": "9_9", "vol_a": 1, "vol_b": 1}},
    ]
    for cmd in bad_tests:
        try:
            json_to_can(cmd)
            print(f"UNEXPECTED SUCCESS for {cmd}")
        except JsonCanError as e:
            print(f"OK - correctly rejected: {cmd} -> {e}")
