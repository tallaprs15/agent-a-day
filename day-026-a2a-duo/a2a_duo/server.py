"""Serve the librarian over A2A (agent card at /.well-known/agent-card.json)."""

from __future__ import annotations

import asyncio
import socket

import uvicorn
from google.adk.a2a.utils.agent_to_a2a import to_a2a

from .agent import build_librarian, default_ledger
from .ledger import RunLedger


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_app(agent, port: int):
    return to_a2a(agent, host="127.0.0.1", port=port)


class BackgroundServer:
    """Runs the librarian's A2A server inside the current event loop (used by the CLI and tests)."""

    def __init__(self, ledger: RunLedger, port: int | None = None, agent=None):
        self.port = port or free_port()
        config = uvicorn.Config(make_app(agent or build_librarian(ledger), self.port), host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "BackgroundServer":
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started:
                return self
            await asyncio.sleep(0.1)
        raise RuntimeError("A2A server did not start")

    async def __aexit__(self, *exc) -> None:
        self._server.should_exit = True
        if self._task:
            await self._task


def serve_forever(port: int) -> None:
    uvicorn.run(make_app(build_librarian(default_ledger()), port), host="127.0.0.1", port=port)
