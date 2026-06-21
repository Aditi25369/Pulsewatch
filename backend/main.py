"""
main.py
FastAPI application — entry point for SplunkOps Copilot.

Endpoints:
  POST /api/triage          — Start a new incident investigation
  GET  /api/triage/{id}     — Poll investigation status (future: async)
  POST /api/chat            — Follow-up questions on a past investigation
  GET  /api/health          — Health check
  GET  /api/splunk/status   — Check Splunk + MCP connectivity
"""

from __future__ import annotations

import uuid
import time
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from backend.config import settings
from backend.agents.orchestrator import get_orchestrator
from backend.streaming import create_stream, get_stream, remove_stream
from backend.models.schemas import (
    IncidentSeverity,
    InvestigationStatus,
    TriageReport,
    TriageRequest,
    TriageResponse,
    ChatMessage,
    SessionState,
)
from backend.tools.splunk_mcp import get_splunk_client

log = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory session store (swap for Redis in prod)
# ─────────────────────────────────────────────────────────────────────────────
_sessions: dict[str, SessionState] = {}
_reports: dict[str, TriageReport] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", env=settings.app_env, port=settings.app_port)
    # Warm up orchestrator (loads LLM client)
    get_orchestrator()
    yield
    log.info("shutdown")


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SplunkOps Copilot",
    description="AI-powered incident triage using Splunk MCP + LangGraph agents",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.is_dev else ["https://yourdomain.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Request logging middleware
# ─────────────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - t0) * 1000, 1)
    log.info(
        "http.request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Health & Status
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/health", tags=["system"])
async def health_check() -> dict[str, Any]:
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "env": settings.app_env,
    }


@app.get("/api/splunk/status", tags=["system"])
async def splunk_status() -> dict[str, Any]:
    """
    Check connectivity to Splunk MCP Server and Splunk instance.
    Useful for the UI to show connection state on load.
    """
    results: dict[str, Any] = {
        "splunk_host": settings.splunk_host,
        "splunk_port": settings.splunk_port,
        "mcp_url": settings.splunk_mcp_url,
    }

    # Test Splunk via HEC health check
    try:
        import httpx, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        async with httpx.AsyncClient(verify=False, timeout=5.0) as hc:
            r = await hc.get(
                f"https://{settings.splunk_host}/services/collector/health"
            )
            results["mcp_connected"] = True
            results["splunk_reachable"] = r.status_code in (200, 400)
    except Exception as e:
        results["mcp_connected"] = False
        results["splunk_reachable"] = False
        results["error"] = str(e)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Core: Triage
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/triage",
    response_model=TriageResponse,
    status_code=status.HTTP_200_OK,
    tags=["triage"],
    summary="Start an incident investigation",
)
async def start_triage(request: TriageRequest) -> TriageResponse:
    """
    Main endpoint. Developer describes the incident in natural language.
    The multi-agent pipeline investigates Splunk data and returns a report.

    - **query**: Natural language incident description
    - **service**: Optional service name to focus the investigation
    - **time_window_minutes**: How far back to search in Splunk (default 60)
    - **session_id**: Pass an existing session_id for follow-up investigations
    """
    investigation_id = str(uuid.uuid4())[:8]

    log.info(
        "triage.request",
        id=investigation_id,
        query=request.query[:80],
        service=request.service,
        window=request.time_window_minutes,
    )

    orchestrator = get_orchestrator()

    try:
        report = await orchestrator.investigate(
            query=request.query,
            service=request.service,
            time_window_minutes=request.time_window_minutes,
            investigation_id=investigation_id,
        )
    except Exception as e:
        log.exception("triage.failed", id=investigation_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Investigation failed: {str(e)}",
        )

    # Store report for follow-up questions
    _reports[investigation_id] = report

    # Create or update session
    session_id = request.session_id or str(uuid.uuid4())[:8]
    if session_id not in _sessions:
        _sessions[session_id] = SessionState(session_id=session_id)

    session = _sessions[session_id]
    session.messages.append(ChatMessage(role="user", content=request.query))
    session.messages.append(
        ChatMessage(role="assistant", content=report.summary)
    )
    session.last_report = report

    log.info(
        "triage.complete",
        id=investigation_id,
        severity=report.severity,
        status=report.status,
        agents_run=len(report.agent_findings),
    )

    return TriageResponse(
        success=report.status == InvestigationStatus.COMPLETED,
        investigation_id=investigation_id,
        report=report,
    )


@app.get(
    "/api/triage/{investigation_id}",
    response_model=TriageResponse,
    tags=["triage"],
    summary="Get a past investigation by ID",
)
async def get_triage(investigation_id: str) -> TriageResponse:
    """Retrieve a previously completed investigation report."""
    report = _reports.get(investigation_id)
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Investigation {investigation_id} not found",
        )
    return TriageResponse(
        success=True,
        investigation_id=investigation_id,
        report=report,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Streaming: Live agent progress via SSE
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/triage/stream",
    tags=["triage"],
    summary="Start an investigation with live SSE progress updates",
)
async def start_triage_stream(request: TriageRequest):
    """
    Same as /api/triage but streams live progress events as agents run.

    Event types emitted:
      agent_started, tool_call, tool_result, agent_completed,
      agent_failed, report_generating, report_ready, error, done

    Consume with EventSource or fetch + ReadableStream on the frontend.
    """
    investigation_id = str(uuid.uuid4())[:8]
    stream = create_stream(investigation_id)
    session_id = request.session_id or investigation_id

    async def run_investigation():
        orchestrator = get_orchestrator()
        try:
            report = await orchestrator.investigate(
                query=request.query,
                service=request.service,
                time_window_minutes=request.time_window_minutes,
                investigation_id=investigation_id,
                stream=stream,
            )
            _reports[investigation_id] = report
            log.info("triage.stream.report_saved", id=investigation_id, status=report.status)

            if session_id not in _sessions:
                _sessions[session_id] = SessionState(session_id=session_id)
            session = _sessions[session_id]
            session.messages.append(ChatMessage(role="user", content=request.query))
            session.messages.append(ChatMessage(role="assistant", content=report.summary))
            session.last_report = report
            log.info("triage.stream.session_saved", session_id=session_id, total_sessions=len(_sessions))

        except Exception as e:
            log.exception("triage.stream.failed", id=investigation_id, error=str(e))
            await stream.error(str(e))
            await stream.close()
        finally:
            remove_stream(investigation_id)

    # Kick off the investigation in the background
    task = asyncio.create_task(run_investigation())

    return StreamingResponse(
        stream.generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Investigation-Id": investigation_id,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Chat: Follow-up questions
# ─────────────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str = Field(..., description="Session ID from a previous triage response")
    message: str = Field(..., min_length=1, max_length=1000)


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    investigation_id: str | None = None


@app.post(
    "/api/chat",
    response_model=ChatResponse,
    tags=["chat"],
    summary="Ask follow-up questions about a past investigation",
)
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Conversational follow-up on an existing investigation.
    E.g. "Show me the exact stack trace", "When did this last happen?"
    """
    session = _sessions.get(request.session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {request.session_id} not found. Start a triage first.",
        )

    last_report = session.last_report

    # Build context-aware prompt
    context = ""
    if last_report:
        context = f"""
Previous investigation summary:
Title: {last_report.title}
Severity: {last_report.severity}
Root cause: {last_report.root_cause.hypothesis if last_report.root_cause else 'Unknown'}

Agent findings:
"""
        for finding in last_report.agent_findings:
            context += f"- {finding.agent}: {finding.summary[:200]}\n"

    from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
    from backend.agents.orchestrator import build_llm
    llm = build_llm()

    # Build message history
    history_msgs = []
    for msg in session.messages[-6:]:
        if msg.role == "user":
            history_msgs.append(HumanMessage(content=msg.content))
        else:
            history_msgs.append(AIMessage(content=msg.content))

    system = f"""You are SplunkOps Copilot, an AI SRE assistant.
You have just completed an incident investigation. Answer follow-up questions
based on the investigation findings below.

{context}

Be concise and specific. If you reference Splunk data, cite which agent found it."""

    messages = [SystemMessage(content=system)] + history_msgs + [
        HumanMessage(content=request.message)
    ]

    response = await llm.ainvoke(messages)
    reply = response.content if isinstance(response.content, str) else ""

    # Store in session
    session.messages.append(ChatMessage(role="user", content=request.message))
    session.messages.append(ChatMessage(role="assistant", content=reply))

    return ChatResponse(
        session_id=request.session_id,
        reply=reply,
        investigation_id=last_report.investigation_id if last_report else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Serve frontend
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_frontend():
    return FileResponse(
        "frontend/index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Global error handler
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("unhandled.exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "Internal server error", "detail": str(exc)},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.is_dev,
        log_level=settings.log_level.lower(),
    )