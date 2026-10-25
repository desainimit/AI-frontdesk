"""Calls in progress when the API is asked to stop.

A restart for a release should not cut people off mid-sentence. While stopping, the API
reports itself not ready (so no new call is sent here), refuses new call streams, and
waits a while for the calls it has to end (see app/serve.py).
"""

from collections.abc import Generator
from contextlib import contextmanager


class Drain:
    def __init__(self) -> None:
        self.draining = False
        self.calls = 0

    @contextmanager
    def call(self) -> Generator[None]:
        self.calls += 1
        try:
            yield
        finally:
            self.calls -= 1


DRAIN = Drain()
