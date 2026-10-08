from dataclasses import dataclass
from typing import Protocol


class Dependency(Protocol):
    async def ping(self) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class Resources:
    database: Dependency
    redis: Dependency
