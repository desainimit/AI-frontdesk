"""One in-flight audio frame per call; no recordings or external AI services."""

import asyncio
from contextlib import suppress

from loguru import logger

# Pipecat uses its own logger; do not allow frames or provider data in its debug logs.
logger.disable("pipecat")

from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.worker import PipelineParams, PipelineWorker  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.workers.runner import WorkerRunner  # noqa: E402


class EchoProcessor(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(
                OutputAudioRawFrame(
                    audio=frame.audio,
                    sample_rate=frame.sample_rate,
                    num_channels=frame.num_channels,
                )
            )
        else:
            await self.push_frame(frame, direction)


class AudioSink(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self.pending: asyncio.Future[bytes] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            if self.pending is not None and not self.pending.done():
                self.pending.set_result(frame.audio)
        else:
            await self.push_frame(frame, direction)


class EchoSession:
    def __init__(self) -> None:
        self.sink = AudioSink()
        self.worker = PipelineWorker(
            Pipeline([EchoProcessor(), self.sink]),
            params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000),
            enable_rtvi=False,
            enable_turn_tracking=False,
            idle_timeout_secs=None,
        )
        self.runner = WorkerRunner(handle_sigint=False)
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "EchoSession":
        await self.runner.add_workers(self.worker)
        self.task = asyncio.create_task(self.runner.run())
        return self

    async def process(self, pcm: bytes) -> bytes:
        if self.sink.pending is not None:
            raise RuntimeError("Concurrent audio processing is not supported")
        future = asyncio.get_running_loop().create_future()
        self.sink.pending = future
        try:
            async with asyncio.timeout(5):
                await self.worker.queue_frame(
                    InputAudioRawFrame(audio=pcm, sample_rate=8000, num_channels=1)
                )
                return await future
        finally:
            self.sink.pending = None

    async def __aexit__(self, *args: object) -> None:
        if self.task is None:
            return
        try:
            async with asyncio.timeout(5):
                await self.runner.cancel()
                await self.task
        finally:
            if not self.task.done():
                self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
