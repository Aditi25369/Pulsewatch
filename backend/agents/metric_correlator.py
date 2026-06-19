"""
metric_correlator.py
Agent 2: Queries Splunk metrics/APM data to correlate performance
degradation with the incident window.
"""

from __future__ import annotations

import json
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.language_models import BaseChatModel

from backend.models.schemas import AgentFinding, AgentName, AgentState
from backend.tools.splunk_mcp import get_metric_tools
from backend.utils.formatting import parse_agent_json


SYSTEM_PROMPT = """You are an expert performance engineer specializing in APM and metrics analysis.
Your job is to investigate a production incident by analyzing Splunk metrics data.

You have access to:
- search_splunk_metrics: Query the metrics index for latency, error rates, throughput
- detect_anomalies_in_splunk: Find statistical outliers in any metric

Investigation strategy:
1. Check response time / latency trends in the incident window
2. Look at error rate (4xx, 5xx) over time — find the spike
3. Check throughput (requests per second) — did traffic change?
4. Look at resource metrics: CPU, memory, DB connection pool
5. Correlate metric degradation timing with the log errors

Useful SPL patterns:
- Latency over time:
  index="metrics" service="X" | timechart avg(response_time_ms) span=5m
- Error rate:
  index="metrics" service="X" | timechart count(eval(status_code>=500)) as errors span=5m
- P95 latency:
  index="metrics" service="X" | stats perc95(response_time_ms) by service
- Anomaly detection:
  index="metrics" | timechart avg(response_time_ms) | anomalydetection

Be precise about timestamps. Find the exact minute metrics degraded.
Return findings as JSON with keys:
  summary, metric_anomalies (list), degradation_start_time, peak_error_rate,
  avg_latency_normal, avg_latency_during_incident, affected_services,
  splunk_queries_run, confidence (0.0-1.0)
"""


async def run_metric_correlator(
    state: AgentState,
    llm: BaseChatModel,
) -> AgentState:
    """
    Runs the metric correlator agent. Populates state.metric_findings.
    """
    tools = get_metric_tools()

    # Include log findings as context if available
    log_context = ""
    if state.log_findings:
        log_context = f"""
Previous log analysis found:
{state.log_findings.summary}
Use this to correlate with metric data — do the metric anomalies align with log errors?
"""

    user_message = f"""Incident report from developer:
"{state.original_query}"

Investigation scope:
- Service focus: {state.service or 'all services'}
- Time window: {state.earliest_time} to {state.latest_time}
- Metrics index: metrics
{log_context}
Please investigate the metrics and find:
1. When did latency/error rate spike?
2. How severe was the degradation?
3. Which metrics degraded first?
4. Are there any anomalies in resource usage?

Return your findings as a JSON object.
"""

    llm_with_tools = llm.bind_tools(tools)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]

    splunk_queries: list[str] = []
    max_iterations = 5
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        response = await llm_with_tools.ainvoke(messages)
        messages.append(response)

        if not response.tool_calls:
            break

        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]

            if "spl_query" in tool_args:
                splunk_queries.append(tool_args["spl_query"])

            matching_tool = next((t for t in tools if t.name == tool_name), None)
            if matching_tool:
                try:
                    result = await matching_tool.ainvoke(tool_args)
                except Exception as e:
                    result = json.dumps({"error": str(e)})
            else:
                result = json.dumps({"error": f"Tool {tool_name} not found"})

            messages.append(
                ToolMessage(content=str(result), tool_call_id=tool_call["id"])
            )

    # Extract final response
    final_text = ""
    for msg in reversed(messages):
        if hasattr(msg, "content") and isinstance(msg.content, str) and msg.content:
            if not getattr(msg, "tool_calls", None):
                final_text = msg.content
                break

    findings_data = parse_agent_json(final_text)

    finding = AgentFinding(
        agent=AgentName.METRIC_CORRELATOR,
        summary=findings_data.get("summary", final_text[:500]),
        raw_data=findings_data.get("metric_anomalies", []),
        confidence=float(findings_data.get("confidence", 0.5)),
        splunk_queries_run=splunk_queries,
        completed_at=datetime.utcnow(),
    )

    state.metric_findings = finding
    state.completed_agents.append(AgentName.METRIC_CORRELATOR)
    return state