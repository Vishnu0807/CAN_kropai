# JSON <-> CAN Bridge

This is a small Python layer that converts between JSON and CAN bus messages
for our boards (relay, LED, sensor). Instead of dealing with raw CAN IDs and
byte payloads everywhere, anything upstream (Pi app, database, whatever) can
just send/receive plain JSON, and this handles the conversion to/from what
the boards actually understand.

It's built directly on top of `can_utilities.py` (the existing ID/opcode
logic in this repo) - I didn't reinvent the byte packing, just wrapped it.

## Files

- `can_utilities.py` - existing repo file, ID building + opcode definitions + payload decode
- `step1_schema.py` - JSON schema and board/action definitions
- `step2_encoder.py` - JSON command -> CAN frame
- `step3_decoder.py` - CAN frame -> JSON
- `step4_error_handling.py` - wraps everything so nothing crashes, always returns a clean result
- `step5_bus_integration.py` - connects the encoder/decoder to an actual CAN bus using python-can
- `step6a_integration_test.py` - simulated test with 3 fake boards on one bus, no hardware needed

## How it works

**Sending a command (JSON -> CAN):**

```python
from step4_error_handling import SafeCanBridge

bridge = SafeCanBridge(channel="can0", bustype="socketcan")

result = bridge.send_command({
    "board": "led",
    "node_id": 1,
    "action": "set_level",
    "params": {"brightness": 180}
})

print(result)
```

This converts the JSON into the correct CAN ID + byte payload (using the
same ID scheme and opcodes already in `can_utilities.py`), sends it on the
bus, and waits briefly for an acknowledgement.

**Receiving data (CAN -> JSON):**

```python
result = bridge.listen_once(timeout=1.0)
print(result)
```

Any incoming frame (sensor readings, relay status, etc.) gets decoded into
plain JSON automatically.

## Supported commands right now

| Board  | Action        | Params                                |
|--------|---------------|----------------------------------------|
| relay  | set_relay     | relay_mask (0-255)                     |
| relay  | dose_pair     | pair ("1_2" or "3_4"), vol_a, vol_b (ml)|
| led    | set_level     | brightness (0-255)                     |
| any    | check_node    | -                                       |
| any    | restart       | -                                       |

`node_id` can be a number (0-126) or `"broadcast"` to hit all boards.

## ACK handling

Boards don't have a dedicated "ack" opcode, so this reuses the existing
`CHECK_RESPONSE (0x02)` opcode - if a board sends that back within ~2
seconds of a command, it's treated as an acknowledgement. If nothing comes
back in time, the result says `"ack_status": "timeout"`.

Note: this assumes boards actually send `CHECK_RESPONSE` right after
handling a command. That's not confirmed on real firmware yet - needs
checking once hardware is available.

## Error handling

Nothing in here throws an exception outward. Bad input, decode failures,
bus errors, missing hardware - all of it comes back as a normal dict like:

```python
{"ok": False, "error": {"type": "invalid_command", "message": "..."}}
```

so whatever calls this doesn't need to wrap everything in try/except.

## Testing

Everything above has been tested using python-can's built-in "virtual" bus,
which simulates real CAN traffic entirely in software - no adapter or board
needed. `step6a_integration_test.py` runs a realistic scenario: three fake
boards on one bus, mixed commands going out while sensor telemetry streams
in on its own, and checks that acks match the right board and nothing gets
mixed up.

## What's left

- Test against real boards once hardware is back (confirm the ACK
  assumption above actually holds on real firmware)
- Hook this into wherever the JSON is actually coming from/going to on the
  Pi side 
- LED board CAN firmware itself is separate and still needs to be written

## Requirements

```
pip install python-can
```

## Running the test

```
python step6a_integration_test.py
```
