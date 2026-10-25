"""Run the API so that calls in progress can finish when it is stopped.

Uvicorn closes every open WebSocket as soon as it starts shutting down, which would end
live calls mid-sentence. This server first stops taking connections and waits for the
calls it has, up to a limit, then shuts down as usual.
"""

import asyncio
import logging
import socket

import uvicorn

from app.core.drain import DRAIN

logger = logging.getLogger(__name__)

# The container's stop grace period must be longer than this (deploy/docker-compose.yml).
DRAIN_SECONDS = 300


class DrainingServer(uvicorn.Server):
    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        DRAIN.draining = True
        for server in self.servers:
            server.close()
        waited = 0.0
        if DRAIN.calls:
            logger.info("waiting_for_calls", extra={"calls": DRAIN.calls})
        while DRAIN.calls and waited < DRAIN_SECONDS and not self.force_exit:
            await asyncio.sleep(0.5)
            waited += 0.5
        await super().shutdown(sockets)


def main() -> None:
    config = uvicorn.Config(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        access_log=False,
        proxy_headers=False,
        timeout_graceful_shutdown=20,
        ws_max_size=16384,
        ws_max_queue=16,
    )
    DrainingServer(config).run()


if __name__ == "__main__":
    main()
