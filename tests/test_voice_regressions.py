import asyncio
import audioop
import io
import json
import wave
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from websockets.asyncio.server import ServerConnection, serve

from app.config.settings import Settings
from app.voice.audio import pcm_frames
from app.voice.http import retry_after_seconds
from app.voice.pipeline import VoicePipeline
from app.voice.stt.azure import AzureSTTProvider
from app.voice.stt.base import STTTransientError
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.base import TTSPermanentError
from tests.fake_stt import MockSTTProvider
from tests.fake_tts import MockTTSProvider


async def test_deepgram_live_websocket_finishes_utterance_before_disconnect() -> None:
    received = asyncio.Event()

    async def handler(socket: ServerConnection) -> None:
        assert socket.request is not None
        assert socket.request.headers["Authorization"] == "Token local-test-key"
        params = parse_qs(urlsplit(socket.request.path).query)
        assert params["sample_rate"] == ["8000"] and params["interim_results"] == ["true"]
        assert await socket.recv() == bytes(320)
        received.set()
        for text, finished in [("Book", False), ("tomorrow", True)]:
            await socket.send(
                json.dumps(
                    {
                        "type": "Results",
                        "is_final": True,
                        "speech_final": finished,
                        "channel": {"alternatives": [{"transcript": text, "confidence": 0.9}]},
                    }
                )
            )
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        provider = DeepgramSTTProvider(
            SecretStr("local-test-key"), base_url=f"http://127.0.0.1:{port}"
        )
        stream = await provider.create_stream()
        try:
            await stream.send_audio(bytes(320))
            iterator = stream.results()
            async with asyncio.timeout(3):
                interim = await anext(iterator)
                final = await anext(iterator)
            assert received.is_set()
            assert not interim.is_final
            assert final.is_final and final.text == "Book tomorrow"
        finally:
            await stream.close()


async def test_azure_endpointed_stream_sends_valid_wav_before_call_close() -> None:
    requests: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "audio/wav; codecs=audio/pcm; samplerate=16000"
        with wave.open(io.BytesIO(request.content), "rb") as wav:
            assert wav.getframerate() == 16000 and wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
        requests.append(request.content)
        return httpx.Response(200, json={"RecognitionStatus": "Success", "DisplayText": "Hello"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = await AzureSTTProvider(SecretStr("test"), client=client).create_stream()
        try:
            for _ in range(10):
                await stream.send_audio(b"\x10\x27" * 160)
            for _ in range(30):
                await stream.send_audio(bytes(320))
            async with asyncio.timeout(2):
                result = await anext(stream.results())
            assert result.is_final and result.text == "Hello" and len(requests) == 1
        finally:
            await stream.close()


async def test_azure_background_failure_reaches_consumer() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = await AzureSTTProvider(SecretStr("test"), client=client).create_stream()
        try:
            await stream.send_audio(b"\x10\x27" * 160)
            for _ in range(30):
                await stream.send_audio(bytes(320))
            with pytest.raises(STTTransientError):
                async with asyncio.timeout(2):
                    await anext(stream.results())
        finally:
            await stream.close()


async def test_pcm_chunk_boundaries_preserve_every_sample() -> None:
    audio = bytes(range(256)) * 3

    async def source() -> AsyncIterator[bytes]:
        for start, end in [(0, 1), (1, 303), (303, 511), (511, len(audio))]:
            yield audio[start:end]

    chunks = [frame async for frame in pcm_frames(source())]
    assert b"".join(chunks) == audio
    assert all(len(chunk) <= 320 and len(chunk) % 2 == 0 for chunk in chunks)

    async def broken() -> AsyncIterator[bytes]:
        yield b"x"

    with pytest.raises(TTSPermanentError):
        _ = [frame async for frame in pcm_frames(broken())]


async def test_barge_in_after_synthesis_until_playback_ack() -> None:
    sent: list[dict[str, Any]] = []

    async def send(frame: dict[str, Any]) -> None:
        sent.append(frame)

    pipeline = VoicePipeline(
        "MZtest", MockSTTProvider(results=[]), MockTTSProvider(total_chunks=1), send
    )
    await pipeline.start()
    try:
        await pipeline.speak("Hello")
        assert pipeline.interruption.is_synthesizing
        assert sent[-1]["event"] == "mark"
        pipeline.acknowledge_playback("unrelated")
        assert pipeline.interruption.is_synthesizing
        voiced = audioop.lin2ulaw(b"\x10\x27" * 160, 2)
        await pipeline.process_inbound_audio(voiced)
        await pipeline.process_inbound_audio(voiced)
        await asyncio.sleep(0)
        assert any(frame["event"] == "clear" for frame in sent)
        await pipeline.speak("Again")
        pipeline.acknowledge_playback(sent[-1]["mark"]["name"])
        assert not pipeline.interruption.is_synthesizing
    finally:
        await pipeline.close()


async def test_disabled_barge_in_does_not_clear_audio() -> None:
    sent: list[dict[str, Any]] = []

    async def send(frame: dict[str, Any]) -> None:
        sent.append(frame)

    pipeline = VoicePipeline(
        "MZtest",
        MockSTTProvider(results=[]),
        MockTTSProvider(total_chunks=1),
        send,
        barge_in_enabled=False,
    )
    await pipeline.start()
    try:
        await pipeline.speak("Hello")
        voiced = audioop.lin2ulaw(b"\x10\x27" * 160, 2)
        for _ in range(5):
            await pipeline.process_inbound_audio(voiced)
        assert not any(frame["event"] == "clear" for frame in sent)
    finally:
        await pipeline.close()


@pytest.mark.parametrize("value", ["invalid", "NaN", "Infinity", "Wed, 21 Oct 2015 07:28:00 GMT"])
def test_retry_after_invalid_values(value: str) -> None:
    assert retry_after_seconds(value) == 5


def test_voice_config_rejects_unknown_providers_and_wrong_codec(settings: Settings) -> None:
    for update in [
        {"stt_provider": "typo"},
        {"tts_provider": "typo"},
        {"stt_provider": "mock"},
        {"elevenlabs_output_format": "mp3_44100_128"},
    ]:
        with pytest.raises(ValidationError):
            Settings.model_validate({**settings.model_dump(), **update})


async def test_azure_voice_attribute_is_xml_escaped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        from xml.etree.ElementTree import fromstring

        root = fromstring(request.content)
        assert len(root) == 1 and root[0].attrib["name"] == "voice' injected='value"
        return httpx.Response(200, content=bytes(320))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AzureTTSProvider(SecretStr("test"), client=client)
        assert (
            len([chunk async for chunk in provider.synthesize("<hello>", "voice' injected='value")])
            == 1
        )
