"""
STEP 4 -- Error Handling Layer (consolidation pass)

Steps 2/3/5 already raise/catch JsonCanError, CanDecodeError, timeouts, and
bus send failures individually. This step's job is narrower: guarantee that
NOTHING coming out of this bridge ever raises an uncaught exception to
whatever calls it (Pi app, MQTT handler, REST endpoint, etc.) -- every
outcome is a structured dict with a consistent shape.

Depends on: can_utilities.py, step1_schema.py, step2_encoder.py,
step3_decoder.py, step5_bus_integration.py (all in the same folder).
"""

import traceback

from step1_schema import ResponseStatus
from step2_encoder import json_to_can, JsonCanError
from step3_decoder import can_to_json, CanDecodeError
from step5_bus_integration import CanBridge


# ---------------------------------------------------------------------------
# Standard result shape used everywhere in this layer.
# ---------------------------------------------------------------------------

def _ok(payload: dict) -> dict:
    return {"ok": True, "error": None, **payload}


def _fail(error_type: str, message: str, extra: dict = None) -> dict:
    result = {"ok": False, "error": {"type": error_type, "message": message}}
    if extra:
        result.update(extra)
    return result


# ---------------------------------------------------------------------------
# Safe wrappers around Step 2 / Step 3 functions
# ---------------------------------------------------------------------------

def safe_encode(command: dict) -> dict:
    """
    Never raises. Returns:
      ok:  {"ok": True, "error": None, "can_id": int, "data": bytes}
      fail: {"ok": False, "error": {"type": "...", "message": "..."}}
    """
    try:
        can_id, data = json_to_can(command)
        return _ok({"can_id": can_id, "data": data})
    except JsonCanError as e:
        return _fail("invalid_command", str(e), {"input": command})
    except Exception as e:
        # Catch-all for anything unexpected (bug in encoder, bad enum, etc.)
        # -- logged with full traceback so it's debuggable, but still
        # returned as a structured error, never crashes the caller.
        traceback.print_exc()
        return _fail("internal_error", f"Unexpected encoder failure: {e}", {"input": command})


def safe_decode(can_id: int, data: bytes) -> dict:
    """
    Never raises. Returns:
      ok:   {"ok": True, "error": None, **decoded_fields}
      fail: {"ok": False, "error": {"type": "...", "message": "..."}}
    """
    try:
        decoded = can_to_json(can_id, data)
        return _ok(decoded)
    except CanDecodeError as e:
        return _fail("decode_error", str(e),
                      {"raw_id": hex(can_id), "raw_data": data.hex() if data else ""})
    except Exception as e:
        traceback.print_exc()
        return _fail("internal_error", f"Unexpected decoder failure: {e}",
                      {"raw_id": hex(can_id), "raw_data": data.hex() if data else ""})


# ---------------------------------------------------------------------------
# Safe wrapper around the full send-command-and-wait-for-ack flow
# ---------------------------------------------------------------------------

class SafeCanBridge:
    """
    Wraps CanBridge so every public method returns a structured result and
    never raises -- this is the object the rest of the system (Pi app,
    MQTT handler, etc.) should actually import and use.
    """

    def __init__(self, channel="vcan0", bustype="virtual"):
        try:
            self._bridge = CanBridge(channel=channel, bustype=bustype)
            self._ready = True
            self._init_error = None
        except Exception as e:
            # e.g. adapter not plugged in, wrong COM port, permission error
            self._bridge = None
            self._ready = False
            self._init_error = str(e)

    def is_ready(self) -> bool:
        return self._ready

    def send_command(self, command: dict, wait_for_ack: bool = True) -> dict:
        if not self._ready:
            return _fail("bus_unavailable", f"CAN bus not initialized: {self._init_error}",
                          {"input": command})

        try:
            result = self._bridge.send_command(command, wait_for_ack=wait_for_ack)
        except Exception as e:
            traceback.print_exc()
            return _fail("internal_error", f"Unexpected send failure: {e}", {"input": command})

        if result.get("error"):
            return _fail("invalid_command", result["error"], {"input": command})

        ack = result.get("ack")
        ack_status = ack.get("status") if ack else None
        return _ok({
            "sent": result["sent"],
            "ack": ack,
            "ack_status": ack_status or ResponseStatus.ACK.name.lower(),
        })

    def listen_once(self, timeout=1.0) -> dict:
        """Non-generator, single-poll version -- safer for request/response
        style callers that don't want an infinite generator."""
        if not self._ready:
            return _fail("bus_unavailable", f"CAN bus not initialized: {self._init_error}")

        try:
            msg = self._bridge.bus.recv(timeout=timeout)
        except Exception as e:
            traceback.print_exc()
            return _fail("internal_error", f"Unexpected receive failure: {e}")

        if msg is None:
            return _fail("no_data", "No frame received within timeout")

        return safe_decode(msg.arbitration_id, msg.data)

    def close(self):
        if self._bridge:
            try:
                self._bridge.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("--- safe_encode: valid ---")
    print(safe_encode({"board": "led", "node_id": 1, "action": "set_level", "params": {"brightness": 100}}))

    print("\n--- safe_encode: invalid ---")
    print(safe_encode({"board": "toaster", "action": "set_level", "params": {}}))

    print("\n--- safe_encode: garbage input (not even a dict) ---")
    print(safe_encode("this is not a command"))

    print("\n--- safe_decode: valid ---")
    from can_utilities import build_can_id, Priority, BoardType, Opcode
    fid = build_can_id(Priority.NORMAL, BoardType.SENSOR, 1)
    fdata = bytes([Opcode.SENSOR_LIQUID, 68, 0xF4, 0x09, 0x00, 0x78, 0x00, 0x00])
    print(safe_decode(fid, fdata))

    print("\n--- safe_decode: malformed ---")
    print(safe_decode(fid, bytes([Opcode.SENSOR_LIQUID, 0x01])))

    print("\n--- SafeCanBridge: bus unavailable (bad channel/interface) ---")
    bad_bridge = SafeCanBridge(channel="does_not_exist", bustype="socketcan")
    print("is_ready:", bad_bridge.is_ready())
    print(bad_bridge.send_command({"board": "led", "node_id": 1, "action": "set_level", "params": {"brightness": 1}}))

    print("\n--- SafeCanBridge: working virtual bus, valid command, no responder -> timeout ack ---")
    bridge = SafeCanBridge(channel="test_channel_step4", bustype="virtual")
    print("is_ready:", bridge.is_ready())
    result = bridge.send_command({"board": "led", "node_id": 1, "action": "set_level", "params": {"brightness": 50}})
    print(result)
    bridge.close()

    print("\nAll Step 4 checks completed -- note nothing above ever raised an exception.")
