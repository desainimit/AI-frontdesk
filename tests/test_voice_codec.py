from array import array

from app.voice.codec import double_rate, loudness, pcm_to_ulaw, ulaw_to_pcm


def pcm(*samples: int) -> bytes:
    return array("h", samples).tobytes()


def test_ulaw_matches_the_g711_reference_values() -> None:
    # Known pairs from the G.711 tables: silence, the loudest codes and a quiet step.
    assert ulaw_to_pcm(bytes([0xFF, 0x7F, 0x00, 0x80, 0xFE])) == pcm(0, 0, -32124, 32124, 8)
    assert pcm_to_ulaw(pcm(0, -1, 32767, -32768, 1000, -1000)) == bytes(
        [0xFF, 0x7E, 0x80, 0x00, 0xCE, 0x4E]
    )


def test_a_round_trip_stays_within_one_step_of_the_original() -> None:
    samples = list(range(-32000, 32001, 97))
    back = array("h")
    back.frombytes(ulaw_to_pcm(pcm_to_ulaw(pcm(*samples))))
    # The step doubles with each of the eight segments, up to 1024 near full scale.
    assert all(
        abs(after - before) <= max(8, abs(before) // 16)
        for before, after in zip(samples, back, strict=True)
    )


def test_loudness_tells_silence_from_speech() -> None:
    assert loudness(b"") == 0
    assert loudness(pcm(*[0] * 160)) == 0
    assert round(loudness(pcm(*[3000, -3000] * 80))) == 3000


def test_doubling_the_rate_fills_in_the_halfway_samples() -> None:
    assert double_rate(pcm(0, 100, 200)) == pcm(0, 50, 100, 150, 200, 200)
    assert double_rate(b"") == b""
