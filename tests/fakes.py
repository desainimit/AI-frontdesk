import asyncio


class FakeDependency:
    def __init__(self, *, failing: bool = False, hanging: bool = False) -> None:
        self.failing = failing
        self.hanging = hanging
        self.pings = 0
        self.closed = False
        self.cancelled = False

    async def ping(self) -> None:
        self.pings += 1
        if self.failing:
            raise ConnectionError("sensitive-password-must-not-escape")
        if self.hanging:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def close(self) -> None:
        self.closed = True
