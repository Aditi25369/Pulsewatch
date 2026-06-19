"""
streaming.py
Server-Sent Events (SSE) support for live agent progress updates.
Streams agent status, findings, and final report token-by-token to the UI.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncGenerator

from backend.models.schemas import AgentName


class StreamEvent:
    """A single SSE event."""

    def __init__(self, event: str, data: dict):
        self.event = event
        self.data = data

    def encode(self) -> str:
        payload = json.dumps(self.data, default=str)
        return f"event: {self.event}\ndata: {payload}\n\n"


class InvestigationStream:
    """
    Manages the SSE stream for a single investigation.
    Agents push events here; the HTTP endpoint reads from the queue.
    """

    def __init__(self, investigation_id: str):
        self.investigation_id = investigation_id
        self._queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        self.created_at = datetime.now(timezone.utc)

    async def emit(self, event: str, data: dict) -> None:
        """Push an event onto the stream."""
        await self._queue.put(StreamEvent(event=event, data={"investigation_id": self.investigation_id, **data}))

    async def close(self) -> None:
        """Signal the stream is done."""
        await self._queue.put(None)

    async def generator(self) -> AsyncGenerator[str, None]:
        """Async generator consumed by FastAPI's StreamingResponse."""
        while True:
            event = await self._queue.get()
            if event is None:
                yield StreamEvent(event="done", data={"investigation_id": self.investigation_id}).encode()
                break
            yield event.encode()

    # ── Convenience emitters ──────────────────────────────────────────────────

    async def agent_started(self, agent: AgentName) -> None:
        await self.emit("agent_started", {
            "agent": agent.value,
            "message": f"{agent.value.replace('_', ' ').title()} started investigating…",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def agent_tool_call(self, agent: AgentName, tool: str, query: str) -> None:
        await self.emit("tool_call", {
            "agent": agent.value,
            "tool": tool,
            "query": query[:200],
            "message": f"Running Splunk query via {tool}…",
        })

    async def agent_tool_result(self, agent: AgentName, tool: str, result_count: int) -> None:
        await self.emit("tool_result", {
            "agent": agent.value,
            "tool": tool,
            "result_count": result_count,
            "message": f"Found {result_count} results",
        })

    async def agent_completed(self, agent: AgentName, summary: str, confidence: float) -> None:
        await self.emit("agent_completed", {
            "agent": agent.value,
            "summary": summary[:300],
            "confidence": confidence,
            "message": f"{agent.value.replace('_', ' ').title()} completed ({confidence:.0%} confidence)",
        })

    async def agent_failed(self, agent: AgentName, error: str) -> None:
        await self.emit("agent_failed", {
            "agent": agent.value,
            "error": error[:200],
            "message": f"{agent.value.replace('_', ' ').title()} encountered an error",
        })

    async def report_generating(self) -> None:
        await self.emit("report_generating", {
            "message": "All agents complete. Synthesising triage report…",
        })

    async def report_ready(self, report_dict: dict) -> None:
        await self.emit("report_ready", {
            "report": report_dict,
            "message": "Investigation complete.",
        })

    async def error(self, message: str) -> None:
        await self.emit("error", {"message": message})


# ── Global stream registry ─────────────────────────────────────────────────────

_streams: dict[str, InvestigationStream] = {}


def create_stream(investigation_id: str) -> InvestigationStream:
    stream = InvestigationStream(investigation_id)
    _streams[investigation_id] = stream
    return stream


def get_stream(investigation_id: str) -> InvestigationStream | None:
    return _streams.get(investigation_id)


def remove_stream(investigation_id: str) -> None:
    _streams.pop(investigation_id, None)