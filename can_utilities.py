from enum import IntEnum
import math


class Priority(IntEnum):
    EMERGENCY = 0
    NORMAL = 1


class BoardType(IntEnum):
    MAIN = 0
    RELAY = 1
    LED = 2
    SENSOR = 3


class Opcode(IntEnum):
    CHECK_NODE = 0x01
    CHECK_RESPONSE = 0x02
    REMOTE_RESTART = 0x03
    RELAY_SET = 0x11
    RELAY_STATE = 0x12
    RELAY_PARASTATIC_12 = 0x13
    RELAY_PARASTATIC_34 = 0x14
    LED_SET = 0x21
    LED_LEVEL = 0x22
    SENSOR_LIQUID = 0x31
    SENSOR_ENV = 0x32


SUBGROUP_BROADCAST = 0x7F


def build_can_id(priority: int, board_type: int, subgroup: int) -> int:
    priority &= 0x01       # 1 bit
    board_type &= 0x07     # 3 bits
    subgroup &= 0x7F       # 7 bits

    can_id = (
        (priority << 10) |
        (board_type << 7) |
        subgroup
    )

    return can_id


def decode_can_id(can_id: int):
    priority_bit = (can_id >> 10) & 0x01

    is_emergency = (priority_bit == Priority.EMERGENCY)
    board_type = (can_id >> 7) & 0x07
    subgroup = can_id & 0x7F

    return is_emergency, board_type, subgroup



def _decode_liquid_sensor(data: bytes):
    """
    Expects 8-byte payload
    Layout:
    [0]  opcode
    [1]  pH (uint8, 0.1 resolution, 0xFF = invalid)
    [2:5] ORP (int24 little-endian, 0.1 resolution, 0xFFFFFF = invalid)
    [5:8] EC (uint24 little-endian, 0.1 resolution, 0xFFFFFF = invalid)
    """

    # ---- pH ----
    if data[1] == 0xFF:
        ph = None
    else:
        ph = data[1] / 10.0

    # ---- ORP (signed 24-bit) ----
    orp_raw = data[2] | (data[3] << 8) | (data[4] << 16)

    if orp_raw == 0xFFFFFF:
        orp = None
    else:
        # Convert from signed 24-bit
        if orp_raw & 0x800000:  # negative
            orp_raw -= 1 << 24
        orp = orp_raw / 10.0

    # ---- EC (unsigned 24-bit) ----
    ec_raw = data[5] | (data[6] << 8) | (data[7] << 16)

    if ec_raw == 0xFFFFFF:
        ec = None
    else:
        ec = ec_raw / 10.0

    return {
        "ph": ph,
        "orp": orp,
        "ec": ec,
    }

def _decode_env_sensor(data: bytes):
    """
    Layout:
    [0] opcode
    [1:3] temperature (int16 little-endian, 0.1°C, 0x7FFF = invalid)
    [3:5] humidity (uint16 little-endian, 0.1%, 0xFFFF = invalid)
    [5:7] co2 (uint16 little-endian, 1ppm, 0xFFFF = invalid)
    """

    # ---- Temperature ----
    temp_raw = data[1] | (data[2] << 8)

    if temp_raw == 0x7FFF:
        temperature = None
    else:
        # Convert signed 16-bit
        if temp_raw & 0x8000:
            temp_raw -= 1 << 16
        temperature = temp_raw / 10.0

    # ---- Humidity ----
    hum_raw = data[3] | (data[4] << 8)

    if hum_raw == 0xFFFF:
        humidity = None
    else:
        humidity = hum_raw / 10.0

    # ---- CO2 ----
    if len(data) >= 7:
        co2_raw = data[5] | (data[6] << 8)
        if co2_raw == 0xFFFF:
            co2 = None
        else:
            co2 = float(co2_raw)
    else:
        co2 = None

    return {
        "temperature": temperature,
        "humidity": humidity,
        "co2": co2,
    }

def _decode_relay_data(data: bytes):
    """
    Each bit represents a relay state.
    LSB first in each byte.

    Example:
        data[0] bit0 -> Relay 0
        data[0] bit1 -> Relay 1
        ...
        data[0] bit7 -> Relay 7
        data[1] bit0 -> Relay 8
        ...

    Returns:
        {
            "relay_0": True/False,
            ...
        }
    """

    relays = {}

    for byte_index, byte in enumerate(data):
        for bit in range(8):
            relay_number = byte_index * 8 + bit
            state = bool((byte >> bit) & 0x01)
            relays[f"relay_{relay_number}"] = state

    return relays


def decode_payload(data: bytes):
    """
    Decodes based on opcode.
    You can extend this for more opcodes later.
    """

    if len(data) < 1:
        raise ValueError("Payload too short")

    opcode = data[0]

    if opcode == Opcode.SENSOR_LIQUID:
        if len(data) != 8:
            raise ValueError("Invalid liquid sensor payload length")
        return {
            "type": "liquid",
            "opcode": opcode,
            **_decode_liquid_sensor(data)
        }

    elif opcode == Opcode.SENSOR_ENV:
        if len(data) < 5:
            raise ValueError("Invalid env sensor payload length")
        return {
            "type": "environment",
            "opcode": opcode,
            **_decode_env_sensor(data)
        }
    
    elif opcode == Opcode.RELAY_STATE:
        return {
            "type": "relay_state",
            "opcode": opcode,
            **_decode_relay_data(data[1:])  # Pass only the relay bytes
        }

    elif opcode in (Opcode.RELAY_PARASTATIC_12, Opcode.RELAY_PARASTATIC_34):
        if len(data) < 5:
            raise ValueError("Invalid parastatic pump payload length")
        
        v1_raw = data[1] | (data[2] << 8)
        v2_raw = data[3] | (data[4] << 8)
        
        return {
            "type": "parastatic_pump",
            "opcode": opcode,
            "v1_ml": v1_raw / 100.0,
            "v2_ml": v2_raw / 100.0
        }

    elif opcode == Opcode.CHECK_RESPONSE:
        # Check response often contains a status bitmask (e.g., 6-bit feedback or device presence)
        status_mask = data[1] if len(data) > 1 else 0
        extra_mask = data[2] if len(data) > 2 else None
        
        result = {
            "type": "check_response",
            "opcode": opcode,
            "status_mask": hex(status_mask),
            "bits": [bool((status_mask >> i) & 0x01) for i in range(8)]
        }
        
        if extra_mask is not None:
            result["extra_mask"] = hex(extra_mask)
            result["extra_bits"] = [bool((extra_mask >> i) & 0x01) for i in range(8)]
            
        return result

    else:
        return {
            "type": "unknown",
            "opcode": opcode,
            "raw_payload": data.hex()
        }


def serialize_relay_payload(opcode: int, relay_states: dict) -> bytes:
    """
    Serialize 8 relay states into 1 byte.

    relay_states format:
        {
            0: True,
            1: False,
            ...
            7: True
        }

    Returns:
        bytes([opcode, relay_byte])
    """

    relay_byte = 0x00

    for relay_number, state in relay_states.items():
        if not (0 <= relay_number <= 7):
            raise ValueError("Relay number must be between 0 and 7")

        if state:
            relay_byte |= (1 << relay_number)

    return bytes([opcode, relay_byte])


if __name__ == "__main__":
    # Example usage
    can_id = build_can_id(Priority.NORMAL, BoardType.SENSOR, 0x01)
    print(f"CAN ID: {can_id:#04x}")

    is_emergency, board_type, subgroup = decode_can_id(can_id)
    print(f"Decoded CAN ID - Emergency: {is_emergency}, Board Type: {board_type}, Subgroup: {subgroup}")

    # Example payload for liquid sensor
    liquid_payload = bytes([Opcode.SENSOR_LIQUID, 25, 0x10, 0x27, 0x00, 0x20, 0x4E, 0x00])
    decoded_liquid = decode_payload(liquid_payload)
    print(f"Decoded Liquid Sensor Payload: {decoded_liquid}")

    # Example payload for environment sensor
    env_payload = bytes([Opcode.SENSOR_ENV, 0x1E, 0x00, 0x58, 0x02])  # Temp=3.0°C, Humidity=60.0%
    decoded_env = decode_payload(env_payload)
    print(f"Decoded Environment Sensor Payload: {decoded_env}")

    # Example relay state payload
    relay_payload = serialize_relay_payload(Opcode.RELAY_SET, {0: True, 1: False, 3: True})
    print(f"Serialized Relay Payload: {relay_payload.hex()}")