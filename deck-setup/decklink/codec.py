"""DeckLink frames, the same in both directions (addons/DeckLink/Codec.lua is the game's copy):

    len (2 bytes, payload length) | channel (1) | seq (1) | payload | CRC-16 (2)

big-endian, CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over everything before it. Channel 0 is
the link itself (OP_* below, first payload byte); addons use 1-255.
"""
import struct

HEADER = 4
OVERHEAD = HEADER + 2
MAX_IN = 4096  # longest payload the game accepts by keys

OP_HELLO, OP_ACK, OP_BYE, OP_PING = 1, 2, 3, 4


def crc16(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build(channel, seq, payload):
    if not 0 <= channel <= 255 or not 0 <= seq <= 255:
        raise ValueError("channel and seq are bytes")
    if len(payload) > 0xFFFF:
        raise ValueError("payload too long")
    body = struct.pack(">HBB", len(payload), channel, seq) + bytes(payload)
    return body + struct.pack(">H", crc16(body))


def parse(frame):
    """(channel, seq, payload) from a whole frame, or None if it's malformed or the CRC fails."""
    if len(frame) < OVERHEAD:
        return None
    length, channel, seq = struct.unpack(">HBB", frame[:HEADER])
    if len(frame) != OVERHEAD + length:
        return None
    if crc16(frame[:-2]) != struct.unpack(">H", frame[-2:])[0]:
        return None
    return channel, seq, bytes(frame[HEADER:-2])


def bits(data):
    """The bits of `data`, most significant first: what the keys send, one tap per bit."""
    for b in data:
        for i in range(7, -1, -1):
            yield (b >> i) & 1
