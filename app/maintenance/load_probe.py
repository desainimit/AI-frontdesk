"""Bounded HTTP probe for a deployed health endpoint; call capacity is Phase 18."""

import argparse
import asyncio
import math
import statistics
import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ProbeResult:
    requests: int
    succeeded: int
    failed: int
    elapsed_seconds: float
    p50_ms: float
    p95_ms: float


async def probe(
    url: str,
    *,
    requests: int = 100,
    concurrency: int = 10,
    timeout_seconds: float = 5,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProbeResult:
    if not 1 <= requests <= 100_000 or not 1 <= concurrency <= 500:
        raise ValueError("Probe requests or concurrency out of range")
    if not 0.1 <= timeout_seconds <= 60:
        raise ValueError("Probe timeout out of range")
    if not url.startswith(("http://", "https://")):
        raise ValueError("Probe requires an HTTP(S) URL")
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    succeeded = 0
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout_seconds,
        limits=httpx.Limits(max_connections=concurrency),
    ) as client:

        async def one() -> None:
            nonlocal succeeded
            async with semaphore:
                start = time.perf_counter()
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        succeeded += 1
                except httpx.RequestError:
                    pass
                finally:
                    latencies.append((time.perf_counter() - start) * 1000)

        start = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(requests)))
        elapsed = time.perf_counter() - start
    ordered = sorted(latencies)
    return ProbeResult(
        requests=requests,
        succeeded=succeeded,
        failed=requests - succeeded,
        elapsed_seconds=elapsed,
        p50_ms=statistics.median(ordered),
        p95_ms=ordered[math.ceil(0.95 * len(ordered)) - 1],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/health/live")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=5)
    args = parser.parse_args()
    result = asyncio.run(
        probe(
            args.url,
            requests=args.requests,
            concurrency=args.concurrency,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(result)
    if result.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
