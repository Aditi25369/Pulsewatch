"""
report_generator.py
Agent 4: Synthesizes all agent findings into a final structured
incident triage report with root cause + remediation steps.
No tools needed — this agent reasons over collected evidence.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel

from backend.models.schemas import (
    AgentName,
    AgentState,
    IncidentSeverity,
    IncidentTimeline,
    InvestigationStatus,
    RemediationStep,
    RootCause,
    TriageReport,
)


SYSTEM_PROMPT = """You are a senior SRE writing a post-incident triage report.
You have received findings from three specialist agents:
- Log Analyst: found errors and exceptions in logs
- Metric Correlator: found performance degradation patterns
- Deploy Inspector: found recent deployments and changes

Your job is to synthesize these findings into a clear, actionable incident report.

The report must include:
1. A concise title (one line, what happened)
2. Executive summary (2-3 sentences — what happened, what was affected, severity)
3. Incident timeline (chronological events with timestamps)
4. Root cause hypothesis (most likely cause + confidence + evidence)
5. Remediation steps (ordered by priority, specific and actionable)
6. A ready-to-use SPL query for a Splunk dashboard to monitor this going forward

Severity classification:
- CRITICAL: complete outage or data loss
- HIGH: major feature down, many users affected
- MEDIUM: degraded performance, partial impact
- LOW: minor issue, minimal user impact
- UNKNOWN: insufficient data

Return a single JSON object with this exact structure:
{
  "title": "string",
  "summary": "string",
  "severity": "critical|high|medium|low|unknown",
  "timeline": [
    {"timestamp": "ISO string or relative", "event": "string", "source": "string"}
  ],
  "root_cause": {
    "hypothesis": "string",
    "confidence": 0.0-1.0,
    "evidence": ["string", ...],
    "first_seen": "timestamp string",
    "affected_services": ["string", ...]
  },
  "remediation_steps": [
    {"priority": 1, "action": "string", "rationale": "string", "estimated_impact": "string"}
  ],
  "splunk_dashboard_spl": "SPL query string"
}
"""


async def run_report_generator(
    state: AgentState,
    llm: BaseChatModel,
) -> AgentState:
    """
    Runs the report generator. Populates state.final_report.
    """

    # Compile all evidence
    evidence_block = f"""
=== ORIGINAL INCIDENT REPORT ===
{state.original_query}
Service focus: {state.service or 'all services'}
Time window: {state.earliest_time} to {state.latest_time}

=== LOG ANALYST FINDINGS ===
{_format_finding(state.log_findings)}

=== METRIC CORRELATOR FINDINGS ===
{_format_finding(state.metric_findings)}

=== DEPLOY INSPECTOR FINDINGS ===
{_format_finding(state.deploy_findings)}
"""

    user_message = f"""Here are all findings from the investigation:

{evidence_block}

Please synthesize these into a complete incident triage report.
Return ONLY the JSON object — no preamble, no markdown fences.
"""

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]

    response = await llm.ainvoke(messages)
    response_text = response.content if isinstance(response.content, str) else ""

    # Parse the report JSON
    report_data = _parse_report_json(response_text)

    # Build the TriageReport model
    timeline = [
        IncidentTimeline(
            timestamp=item.get("timestamp", "unknown"),
            event=item.get("event", ""),
            source=item.get("source", "agent"),
        )
        for item in report_data.get("timeline", [])
    ]

    root_cause_data = report_data.get("root_cause", {})
    root_cause = RootCause(
        hypothesis=root_cause_data.get("hypothesis", "Unable to determine root cause"),
        confidence=float(root_cause_data.get("confidence", 0.3)),
        evidence=root_cause_data.get("evidence", []),
        first_seen=root_cause_data.get("first_seen"),
        affected_services=root_cause_data.get("affected_services", []),
    ) if root_cause_data else None

    remediation_steps = [
        RemediationStep(
            priority=step.get("priority", i + 1),
            action=step.get("action", ""),
            rationale=step.get("rationale", ""),
            estimated_impact=step.get("estimated_impact", ""),
        )
        for i, step in enumerate(report_data.get("remediation_steps", []))
    ]

    # Collect all agent findings
    agent_findings = [
        f for f in [state.log_findings, state.metric_findings, state.deploy_findings]
        if f is not None
    ]

    severity_str = report_data.get("severity", "unknown").lower()
    try:
        severity = IncidentSeverity(severity_str)
    except ValueError:
        severity = IncidentSeverity.UNKNOWN

    final_report = TriageReport(
        investigation_id=state.investigation_id,
        status=InvestigationStatus.COMPLETED,
        severity=severity,
        title=report_data.get("title", "Incident Investigation Complete"),
        summary=report_data.get("summary", "Investigation completed. See findings above."),
        timeline=timeline,
        root_cause=root_cause,
        agent_findings=agent_findings,
        remediation_steps=remediation_steps,
        splunk_dashboard_spl=report_data.get("splunk_dashboard_spl"),
        investigated_at=datetime.utcnow(),
    )

    state.final_report = final_report
    state.completed_agents.append(AgentName.REPORT_GENERATOR)
    return state


def _format_finding(finding) -> str:
    if finding is None:
        return "No findings available."
    return f"""Summary: {finding.summary}
Confidence: {finding.confidence:.0%}
Queries run: {len(finding.splunk_queries_run)}
Raw data points: {len(finding.raw_data)}"""


def _parse_report_json(text: str) -> dict:
    """Parse JSON from report generator — strips fences if present."""
    # Strip markdown fences
    clean = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object
    try:
        start = clean.find("{")
        end = clean.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(clean[start:end])
    except json.JSONDecodeError:
        pass

    # Fallback minimal report
    return {
        "title": "Incident Investigation Complete",
        "summary": text[:400],
        "severity": "unknown",
        "timeline": [],
        "root_cause": {
            "hypothesis": "Could not determine root cause from available data.",
            "confidence": 0.1,
            "evidence": [],
            "affected_services": [],
        },
        "remediation_steps": [
            {
                "priority": 1,
                "action": "Review Splunk logs manually for the incident window",
                "rationale": "Automated analysis was inconclusive",
                "estimated_impact": "May identify root cause",
            }
        ],
        "splunk_dashboard_spl": 'index=main level=ERROR | timechart count by service span=5m',
    }