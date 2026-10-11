"""Telephone audio in plain Python: G.711 µ-law, loudness and a rate change.

Phone calls arrive as 8 kHz µ-law, one byte per sample. Everything else here works on
16-bit little-endian PCM. A call is 8,000 samples a second, so table lookups are plenty.
"""

import math
import sys
from array import array

_BIAS = 0x84
# The largest magnitude µ-law can carry, in 14-bit terms.
_CLIP = 8159
_SEGMENT_ENDS = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _decode(code: int) -> int:
    code = ~code & 0xFF
    magnitude = (((code & 0x0F) << 3) + _BIAS) << ((code >> 4) & 0x07)
    return _BIAS - magnitude if code & 0x80 else magnitude - _BIAS


def _encode(sample: int) -> int:
    """One 16-bit sample as µ-law, as the ITU reference does it on the top 14 bits."""
    value = sample >> 2
    mask = 0xFF
    if value < 0:
        value, mask = -value, 0x7F
    value = min(value, _CLIP) + (_BIAS >> 2)
    for segment, end in enumerate(_SEGMENT_ENDS):
        if value <= end:
            return ((segment << 4) | ((value >> (segment + 1)) & 0x0F)) ^ mask
    return 0x7F ^ mask


_TO_PCM = array("h", (_decode(code) for code in range(256)))
# Indexed by the sample read as an unsigned 16-bit number.
_TO_ULAW = bytes(_encode(value - 65536 if value >= 32768 else value) for value in range(65536))


def _samples(pcm: bytes, code: str) -> array[int]:
    samples: array[int] = array(code)
    samples.frombytes(pcm[: len(pcm) & ~1])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _bytes(samples: array[int]) -> bytes:
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def ulaw_to_pcm(ulaw: bytes) -> bytes:
    return _bytes(array("h", (_TO_PCM[code] for code in ulaw)))


def pcm_to_ulaw(pcm: bytes) -> bytes:
    return bytes(_TO_ULAW[value] for value in _samples(pcm, "H"))


def loudness(pcm: bytes) -> float:
    """Root mean square of the samples: 0 for silence, about 200 and up for speech."""
    samples = _samples(pcm, "h")
    if not samples:
        return 0.0
    return math.sqrt(sum(value * value for value in samples) / len(samples))


def double_rate(pcm: bytes) -> bytes:
    """8 kHz to 16 kHz: each new sample sits halfway between its neighbours."""
    samples = _samples(pcm, "h")
    if not samples:
        return b""
    doubled = array("h", bytes(4 * len(samples)))
    for index, value in enumerate(samples):
        following = samples[index + 1] if index + 1 < len(samples) else value
        doubled[2 * index] = value
        doubled[2 * index + 1] = (value + following) // 2
    return _bytes(doubled)
