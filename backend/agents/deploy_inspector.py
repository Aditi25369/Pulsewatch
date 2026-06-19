"""
deploy_inspector.py
Agent 3: Searches Splunk deployment events for recent releases,
config changes, and infra events that may have caused the incident.
"""

from __future__ import annotations

import json
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.language_models import BaseChatModel

from backend.models.schemas import AgentFinding, AgentName, AgentState
from backend.tools.splunk_mcp import get_deploy_tools
from backend.utils.formatting import parse_agent_json


SYSTEM_PROMPT = """You are an expert DevOps engineer specializing in change management and release analysis.
Your job is to find what changed in the system before or during a production incident.

You have access to:
- search_splunk_deployments: Query the deployments index for releases, config changes, infra events

Investigation strategy:
1. Find all deployments in the 24 hours before the incident
2. Identify the most recent deployment before incident start
3. Check if the affected service was deployed recently
4. Look for config changes, feature flag changes, infra scaling events
5. Assess temporal correlation — did metrics degrade within minutes of a deploy?

Useful SPL patterns:
- All recent deploys:
  index="deployments" | sort -_time | table _time, service, version, deployed_by, environment
- Deploys for specific service:
  index="deployments" service="payment-service" | sort -_time
- Config changes:
  index="deployments" event_type="config_change" | sort -_time
- Deploy immediately before incident:
  index="deployments" | where _time < relative_time(now(), "-1h") | sort -_time | head 5

Key questions to answer:
- What was deployed in the last 24h?
- Was the affected service deployed recently?
- Is there a "guilty deploy" — one that correlates temporally with the incident?

Return findings as JSON with keys:
  summary, recent_deploys (list with time/service/version),
  guilty_deploy (object or null), time_between_deploy_and_incident,
  config_changes, splunk_queries_run, confidence (0.0-1.0)
"""


async def run_deploy_inspector(
    state: AgentState,
    llm: BaseChatModel,
) -> AgentState:
    """
    Runs the deploy inspector agent. Populates state.deploy_findings.
    """
    tools = get_deploy_tools()

    # Build rich context from previous agents
    prior_context = ""
    if state.log_findings:
        prior_context += f"\nLog analysis found: {state.log_findings.summary}"
    if state.metric_findings:
        prior_context += f"\nMetric analysis found: {state.metric_findings.summary}"

    user_message = f"""Incident report from developer:
"{state.original_query}"

Investigation scope:
- Service focus: {state.service or 'all services'}
- Incident window: {state.earliest_time} to {state.latest_time}
- Look back window for deploys: last 24 hours
{prior_context}

Please investigate recent deployments and find:
1. What was deployed in the last 24 hours?
2. Was the affected service recently deployed?
3. Is there a deploy that correlates with the incident start time?
4. Were there any config or infra changes?

Return your findings as a JSON object.
"""

    llm_with_tools = llm.bind_tools(tools)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]

    splunk_queries: list[str] = []
    max_iterations = 4
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

    final_text = ""
    for msg in reversed(messages):
        if hasattr(msg, "content") and isinstance(msg.content, str) and msg.content:
            if not getattr(msg, "tool_calls", None):
                final_text = msg.content
                break

    findings_data = parse_agent_json(final_text)

    # Extract recent deploys for raw_data
    recent_deploys = findings_data.get("recent_deploys", [])
    if findings_data.get("guilty_deploy"):
        recent_deploys = [findings_data["guilty_deploy"]] + recent_deploys

    finding = AgentFinding(
        agent=AgentName.DEPLOY_INSPECTOR,
        summary=findings_data.get("summary", final_text[:500]),
        raw_data=recent_deploys,
        confidence=float(findings_data.get("confidence", 0.5)),
        splunk_queries_run=splunk_queries,
        completed_at=datetime.utcnow(),
    )

    state.deploy_findings = finding
    state.completed_agents.append(AgentName.DEPLOY_INSPECTOR)
    return state