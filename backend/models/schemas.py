"""
schemas.py
All Pydantic models: API contracts + internal agent state.
"""

from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class IncidentSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class InvestigationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentName(str, Enum):
    LOG_ANALYST = "log_analyst"
    METRIC_CORRELATOR = "metric_correlator"
    DEPLOY_INSPECTOR = "deploy_inspector"
    REPORT_GENERATOR = "report_generator"


# ─────────────────────────────────────────────────────────────────────────────
# API Request / Response
# ─────────────────────────────────────────────────────────────────────────────

class TriageRequest(BaseModel):
    """Incoming incident description from the developer."""
    query: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="Natural language description of the incident",
        examples=["API latency spiked after the 3am deploy, payment service is throwing 500s"],
    )
    service: str | None = Field(
        default=None,
        description="Specific service name to focus on (optional)",
        examples=["payment-service", "auth-api"],
    )
    time_window_minutes: int = Field(
        default=60,
        ge=5,
        le=1440,
        description="How far back to look in Splunk (minutes)",
    )
    session_id: str | None = Field(
        default=None,
        description="For follow-up questions in an ongoing investigation",
    )


class AgentFinding(BaseModel):
    """One agent's findings from its investigation step."""
    agent: AgentName
    summary: str
    raw_data: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    splunk_queries_run: list[str] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=datetime.utcnow)


class RootCause(BaseModel):
    """Structured root cause hypothesis."""
    hypothesis: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    first_seen: str | None = None
    affected_services: list[str] = Field(default_factory=list)


class RemediationStep(BaseModel):
    priority: int
    action: str
    rationale: str
    estimated_impact: str


class IncidentTimeline(BaseModel):
    timestamp: str
    event: str
    source: str  # which agent/tool found this


class TriageReport(BaseModel):
    """Final structured report delivered to the developer."""
    investigation_id: str
    status: InvestigationStatus
    severity: IncidentSeverity
    title: str
    summary: str
    timeline: list[IncidentTimeline] = Field(default_factory=list)
    root_cause: RootCause | None = None
    agent_findings: list[AgentFinding] = Field(default_factory=list)
    remediation_steps: list[RemediationStep] = Field(default_factory=list)
    splunk_dashboard_spl: str | None = Field(
        default=None,
        description="A ready-to-paste SPL query for a Splunk dashboard",
    )
    investigated_at: datetime = Field(default_factory=datetime.utcnow)
    duration_seconds: float | None = None


class TriageResponse(BaseModel):
    """HTTP response wrapper."""
    success: bool
    investigation_id: str
    report: TriageReport | None = None
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph Agent State
# ─────────────────────────────────────────────────────────────────────────────

class AgentState(BaseModel):
    """
    Shared state passed between all agents in the LangGraph graph.
    Each agent reads what it needs and writes its findings back.
    """
    # Input
    investigation_id: str
    original_query: str
    service: str | None = None
    time_window_minutes: int = 60

    # Computed time range (set by orchestrator before agents run)
    earliest_time: str = "-60m"
    latest_time: str = "now"

    # Agent outputs (populated as graph runs)
    log_findings: AgentFinding | None = None
    metric_findings: AgentFinding | None = None
    deploy_findings: AgentFinding | None = None

    # Final output
    final_report: TriageReport | None = None

    # Routing / control
    errors: list[str] = Field(default_factory=list)
    completed_agents: list[AgentName] = Field(default_factory=list)

    class Config:
        arbitrary_types_allowed = True


# ─────────────────────────────────────────────────────────────────────────────
# Splunk Tool I/O
# ─────────────────────────────────────────────────────────────────────────────

class SplunkSearchResult(BaseModel):
    """Normalised result from any Splunk MCP search."""
    query: str
    result_count: int
    results: list[dict[str, Any]]
    earliest: str
    latest: str
    duration_ms: float | None = None


class SplunkError(BaseModel):
    query: str
    error_message: str
    error_type: str


# ─────────────────────────────────────────────────────────────────────────────
# Chat / Session (for follow-up questions)
# ─────────────────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]  # type: ignore[valid-type]
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SessionState(BaseModel):
    session_id: str
    messages: list[ChatMessage] = Field(default_factory=list)
    last_report: TriageReport | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# Required when using `from __future__ import annotations` with forward refs
SessionState.model_rebuild()
TriageReport.model_rebuild()
AgentState.model_rebuild()