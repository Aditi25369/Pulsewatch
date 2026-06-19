"""
orchestrator.py
LangGraph supervisor with SSE streaming support.
Emits real-time progress events as each agent runs.
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from datetime import datetime, timezone

import structlog
from langchain_core.language_models import BaseChatModel

from backend.config import settings
from backend.models.schemas import (
    AgentName,
    AgentState,
    IncidentSeverity,
    InvestigationStatus,
    TriageReport,
)
from backend.agents.log_analyst import run_log_analyst
from backend.agents.metric_correlator import run_metric_correlator
from backend.agents.deploy_inspector import run_deploy_inspector
from backend.agents.report_generator import run_report_generator

log = structlog.get_logger(__name__)


def build_llm() -> BaseChatModel:
    if settings.llm_provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=settings.llm_model,
            api_key=settings.groq_api_key,
            temperature=0,
            max_tokens=4096,
        )
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=settings.llm_model,
            api_key=settings.anthropic_api_key,
            temperature=0,
            max_tokens=4096,
        )
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        temperature=0,
        max_tokens=4096,
    )


def _build_time_range(minutes: int) -> tuple[str, str]:
    return f"-{minutes}m", "now"


async def _run_agent_with_stream(agent_fn, state, llm, agent_name: AgentName, stream=None):
    """Wrap an agent run with SSE stream events."""
    if stream:
        await stream.agent_started(agent_name)
    try:
        result = await agent_fn(state, llm)
        finding = getattr(result, {
            AgentName.LOG_ANALYST: "log_findings",
            AgentName.METRIC_CORRELATOR: "metric_findings",
            AgentName.DEPLOY_INSPECTOR: "deploy_findings",
        }.get(agent_name, "log_findings"), None)

        if stream and finding:
            await stream.agent_completed(agent_name, finding.summary, finding.confidence)
            for q in finding.splunk_queries_run:
                await stream.agent_tool_call(agent_name, "search_splunk", q)
        return result
    except Exception as e:
        if stream:
            await stream.agent_failed(agent_name, str(e))
        raise


class InvestigationOrchestrator:

    def __init__(self) -> None:
        self.llm = build_llm()

    async def investigate(
        self,
        query: str,
        service: str | None = None,
        time_window_minutes: int = 60,
        investigation_id: str | None = None,
        stream=None,
    ) -> TriageReport:
        inv_id = investigation_id or str(uuid.uuid4())[:8]
        t_start = time.perf_counter()
        log.info("investigation.start", id=inv_id, query=query[:80])

        earliest, latest = _build_time_range(time_window_minutes)
        state = AgentState(
            investigation_id=inv_id,
            original_query=query,
            service=service,
            time_window_minutes=time_window_minutes,
            earliest_time=earliest,
            latest_time=latest,
        )

        try:
            state = await self._run_parallel_agents(state, stream)

            if stream:
                await stream.report_generating()

            state = await run_report_generator(state, self.llm)

        except asyncio.TimeoutError:
            log.error("investigation.timeout", id=inv_id)
            report = self._timeout_report(inv_id)
            if stream:
                await stream.error("Investigation timed out")
                await stream.report_ready(report.model_dump())
                await stream.close()
            return report
        except Exception as e:
            log.exception("investigation.error", id=inv_id, error=str(e))
            report = self._error_report(inv_id, str(e))
            if stream:
                await stream.error(str(e))
                await stream.report_ready(report.model_dump())
                await stream.close()
            return report

        duration = round(time.perf_counter() - t_start, 2)
        log.info("investigation.complete", id=inv_id, duration_s=duration)

        if state.final_report:
            state.final_report.duration_seconds = duration
            if stream:
                await stream.report_ready(state.final_report.model_dump())
                await stream.close()
            return state.final_report

        report = self._error_report(inv_id, "Report generator returned no output")
        if stream:
            await stream.report_ready(report.model_dump())
            await stream.close()
        return report

    async def _run_parallel_agents(self, state: AgentState, stream=None) -> AgentState:
        state_log = copy.deepcopy(state)
        state_metric = copy.deepcopy(state)
        state_deploy = copy.deepcopy(state)

        async def staggered(fn, st, name, delay):
            if delay:
                await asyncio.sleep(delay)
            return await _run_agent_with_stream(fn, st, self.llm, name, stream)

        results = await asyncio.wait_for(
            asyncio.gather(
                staggered(run_log_analyst, state_log, AgentName.LOG_ANALYST, 0),
                staggered(run_metric_correlator, state_metric, AgentName.METRIC_CORRELATOR, 2),
                staggered(run_deploy_inspector, state_deploy, AgentName.DEPLOY_INSPECTOR, 4),
                return_exceptions=True,
            ),
            timeout=settings.agent_timeout_seconds,
        )

        agent_names = [AgentName.LOG_ANALYST, AgentName.METRIC_CORRELATOR, AgentName.DEPLOY_INSPECTOR]
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                log.warning("agent.failed", agent=agent_names[i].value, error=str(result))
                state.errors.append(f"{agent_names[i].value}: {str(result)}")
            else:
                if i == 0 and result.log_findings:
                    state.log_findings = result.log_findings
                elif i == 1 and result.metric_findings:
                    state.metric_findings = result.metric_findings
                elif i == 2 and result.deploy_findings:
                    state.deploy_findings = result.deploy_findings

        return state

    def _timeout_report(self, inv_id: str) -> TriageReport:
        return TriageReport(
            investigation_id=inv_id,
            status=InvestigationStatus.FAILED,
            severity=IncidentSeverity.UNKNOWN,
            title="Investigation Timed Out",
            summary=f"Exceeded {settings.agent_timeout_seconds}s timeout. Try narrowing the time window.",
            investigated_at=datetime.now(timezone.utc),
        )

    def _error_report(self, inv_id: str, error: str) -> TriageReport:
        return TriageReport(
            investigation_id=inv_id,
            status=InvestigationStatus.FAILED,
            severity=IncidentSeverity.UNKNOWN,
            title="Investigation Failed",
            summary=f"Error: {error}",
            investigated_at=datetime.now(timezone.utc),
        )


_orchestrator: InvestigationOrchestrator | None = None


def get_orchestrator() -> InvestigationOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = InvestigationOrchestrator()
    return _orchestrator