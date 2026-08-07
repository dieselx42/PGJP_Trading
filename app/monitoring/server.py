"""Minimal loopback HTTP server for /health and /status.

Written on ``asyncio`` streams with no web framework. The surface is tiny by
design: GET only, two fixed paths, a hard cap on how much of a request it will
read, and no request body handling at all. There is nothing here for an
attacker to reach even if the bind address were ever wrong.

Binding defaults to ``127.0.0.1``. The Docker healthcheck runs *inside* the
container, so loopback is sufficient and nothing needs publishing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from app.logging_config import get_logger

_LOG = get_logger("monitoring.server")

#: Requests larger than this are dropped without a response.
MAX_REQUEST_BYTES = 8 * 1024
MAX_HEADER_LINES = 64

HealthProvider = Callable[[], tuple[int, dict[str, object]]]
StatusProvider = Callable[[], dict[str, object]]


class HealthServer:
    """Serves ``/health`` and ``/status`` on loopback."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        health_provider: HealthProvider,
        status_provider: StatusProvider,
    ) -> None:
        self._host = host
        self._port = port
        self._health_provider = health_provider
        self._status_provider = status_provider
        self._server: asyncio.Server | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def bound_port(self) -> int:
        """Actual port in use. Differs from the requested port only when 0 was requested."""
        if self._server is None:
            return self._port
        for sock in self._server.sockets:
            return int(sock.getsockname()[1])
        return self._port

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        _LOG.info(
            "health server listening",
            extra={
                "event": "health.started",
                "host": self._host,
                "port": self.bound_port,
                "public": False,
            },
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        _LOG.info("health server stopped", extra={"event": "health.stopped"})

    # ------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line or len(request_line) > MAX_REQUEST_BYTES:
                return
            parts = request_line.decode("latin-1", errors="replace").split()
            if len(parts) < 2:
                return
            method, path = parts[0].upper(), parts[1]

            # Drain headers so the client sees a clean response, bounded so a
            # slow or hostile client cannot make us read forever.
            for _ in range(MAX_HEADER_LINES):
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            if method != "GET":
                await self._respond(writer, 405, {"error": "method not allowed"})
                return

            route = path.split("?", 1)[0].rstrip("/") or "/"
            if route in ("/health", "/healthz"):
                status_code, payload = self._health_provider()
                await self._respond(writer, status_code, payload)
            elif route == "/status":
                await self._respond(writer, 200, self._status_provider())
            elif route == "/":
                await self._respond(writer, 200, {"endpoints": ["/health", "/status"]})
            else:
                await self._respond(writer, 404, {"error": "not found"})
        except (TimeoutError, ConnectionResetError, BrokenPipeError):
            return
        except Exception as exc:  # noqa: BLE001 -- a monitoring bug must not kill the bot
            _LOG.warning(
                "health server request failed",
                extra={"event": "health.request_failed", "error": str(exc)},
            )
        finally:
            with _closing(writer):
                writer.close()

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter, status_code: int, payload: dict[str, object]
    ) -> None:
        body = json.dumps(payload, default=str, indent=2).encode("utf-8")
        reason = {
            200: "OK",
            404: "Not Found",
            405: "Method Not Allowed",
            503: "Service Unavailable",
        }
        head = (
            f"HTTP/1.1 {status_code} {reason.get(status_code, 'OK')}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()


class _closing:  # noqa: N801 -- context manager
    """Swallow errors raised while closing a client connection."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return exc_type is not None


__all__ = ["MAX_REQUEST_BYTES", "HealthServer"]
