"""
log_analyst.py
Agent 1: Searches Splunk logs for errors, exceptions, and anomalies
around the incident window.
"""

from __future__ import annotations

import json
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel

from backend.models.schemas import AgentFinding, AgentName, AgentState
from backend.tools.splunk_mcp import get_log_tools
from backend.utils.formatting import parse_agent_json


SYSTEM_PROMPT = """You are an expert SRE (Site Reliability Engineer) specializing in log analysis.
Your job is to investigate a production incident by searching Splunk logs.

You have access to these tools:
- search_splunk_logs: Run SPL queries against the logs index
- detect_anomalies_in_splunk: Find statistical anomalies in metrics

Investigation strategy:
1. Search for ERROR and CRITICAL level logs in the incident window
2. Look for exception stack traces and error messages
3. Identify which services and endpoints are affected
4. Find the first occurrence of the error (the "patient zero" event)
5. Check error frequency — is it spike or steady state?

SPL tips:
- Always scope by time using earliest/latest params
- Use: index="main" level=ERROR OR level=CRITICAL
- Filter by service: ... service="payment-service"
- Count errors over time: ... | timechart count by level span=5m
- Find first error: ... | sort _time | head 1

Be methodical. Run 2-3 targeted queries. Summarize what you find concisely.
Return your findings as JSON with keys: summary, errors_found, affected_services, first_error_time, error_patterns, splunk_queries_run, confidence (0.0-1.0)
"""


async def run_log_analyst(
    state: AgentState,
    llm: BaseChatModel,
) -> AgentState:
    """
    Runs the log analyst agent. Populates state.log_findings.
    """
    tools = get_log_tools()

    # Build the human message with context
    user_message = f"""Incident report from developer:
"{state.original_query}"

Investigation scope:
- Service focus: {state.service or 'all services'}
- Time window: {state.earliest_time} to {state.latest_time}
- Splunk logs index: main

Please investigate the logs and find:
1. What errors are occurring?
2. Which services are affected?
3. When did errors first appear?
4. What's the error pattern/frequency?

Return your findings as a JSON object.
"""

    # Bind tools to LLM and run
    llm_with_tools = llm.bind_tools(tools)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]

    # Agentic loop — keep going until no more tool calls
    splunk_queries: list[str] = []
    max_iterations = 5
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        response = await llm_with_tools.ainvoke(messages)
        messages.append(response)

        # If no tool calls, agent is done
        if not response.tool_calls:
            break

        # Execute each tool call
        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]

            # Track queries for transparency
            if "spl_query" in tool_args:
                splunk_queries.append(tool_args["spl_query"])

            # Find and run the matching tool
            matching_tool = next((t for t in tools if t.name == tool_name), None)
            if matching_tool:
                try:
                    result = await matching_tool.ainvoke(tool_args)
                except Exception as e:
                    result = json.dumps({"error": str(e), "tool": tool_name})
            else:
                result = json.dumps({"error": f"Tool {tool_name} not found"})

            from langchain_core.messages import ToolMessage
            messages.append(
                ToolMessage(content=str(result), tool_call_id=tool_call["id"])
            )

    # Extract the final text response
    final_text = ""
    for msg in reversed(messages):
        if hasattr(msg, "content") and isinstance(msg.content, str) and msg.content:
            if not getattr(msg, "tool_calls", None):
                final_text = msg.content
                break

    # Parse JSON from the agent's response
    findings_data = parse_agent_json(final_text)

    finding = AgentFinding(
        agent=AgentName.LOG_ANALYST,
        summary=findings_data.get("summary", final_text[:500]),
        raw_data=findings_data.get("errors_found", []),
        confidence=float(findings_data.get("confidence", 0.5)),
        splunk_queries_run=splunk_queries,
        completed_at=datetime.utcnow(),
    )

    state.log_findings = finding
    state.completed_agents.append(AgentName.LOG_ANALYST)
    return state