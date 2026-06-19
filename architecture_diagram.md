# SplunkOps Copilot — Architecture Diagram

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         DEVELOPER                                        │
│  "Payment service throwing 500s after 3am deploy — what happened?"      │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │  HTTP POST /api/triage
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      FastAPI Backend  (main.py)                          │
│                                                                          │
│   POST /api/triage   →  InvestigationOrchestrator                       │
│   POST /api/chat     →  Follow-up conversation handler                  │
│   GET  /api/splunk/status  →  Connectivity check                        │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │  asyncio.gather() — parallel execution
          ┌────────────────────┼─────────────────────┐
          ▼                    ▼                      ▼
┌─────────────────┐  ┌─────────────────┐  ┌──────────────────┐
│  Log Analyst    │  │Metric Correlator│  │ Deploy Inspector │
│  Agent          │  │ Agent           │  │ Agent            │
│                 │  │                 │  │                  │
│ Finds errors,   │  │ Finds latency   │  │ Finds recent     │
│ exceptions,     │  │ spikes, error   │  │ deploys, config  │
│ stack traces    │  │ rate anomalies  │  │ changes, guilty  │
│ in Splunk logs  │  │ in APM metrics  │  │ commit           │
└────────┬────────┘  └────────┬────────┘  └────────┬─────────┘
         │                    │                      │
         │  LangChain Tool calls (search_splunk_*)   │
         └────────────────────┼──────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Splunk MCP Server                                     │
│                    (tools/splunk_mcp.py)                                 │
│                                                                          │
│   Tools exposed:                                                         │
│   • search_splunk_logs        → index=main                              │
│   • search_splunk_metrics     → index=metrics                           │
│   • search_splunk_deployments → index=deployments                       │
│   • detect_anomalies_in_splunk → anomalydetection SPL                  │
│                                                                          │
│   MCP Protocol: JSON-RPC 2.0 over HTTP                                  │
│   Fallback: Splunk Python SDK (direct REST)                             │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │  SPL Queries
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Splunk Enterprise                                   │
│                                                                          │
│   index=main          Application logs (errors, exceptions, traces)     │
│   index=metrics       APM data (latency, error rate, throughput)        │
│   index=deployments   Deploy events, config changes, releases           │
│                                                                          │
│   Features used:                                                         │
│   • SPL (Search Processing Language)                                    │
│   • anomalydetection command                                            │
│   • timechart, stats, sort commands                                     │
│   • Splunk REST API (port 8089)                                         │
└─────────────────────────────────────────────────────────────────────────┘

         ← Results flow back up through MCP → agents →

┌─────────────────────────────────────────────────────────────────────────┐
│                    Report Generator Agent                                │
│                                                                          │
│   Input:  Findings from all 3 parallel agents                           │
│   LLM:    Claude (claude-sonnet-4-6) via Anthropic API                  │
│                                                                          │
│   Output:                                                               │
│   • Incident title + severity classification                            │
│   • Chronological event timeline                                        │
│   • Root cause hypothesis + confidence score + evidence                 │
│   • Prioritised remediation steps                                       │
│   • Ready-to-use SPL query for Splunk dashboard                        │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Chat UI (frontend/index.html)                         │
│                                                                          │
│   • Natural language incident input                                     │
│   • Structured triage report with severity badge                        │
│   • Root cause with confidence bar                                      │
│   • Timeline, remediation steps, copyable SPL query                    │
│   • Follow-up conversation mode (/api/chat)                             │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Component Breakdown

### How the application interacts with Splunk

| Layer | Mechanism | Detail |
|---|---|---|
| Primary | **Splunk MCP Server** | JSON-RPC 2.0 calls to MCP server which translates to Splunk REST API queries |
| Fallback | **Splunk Python SDK** | Direct `splunklib.client` connection to port 8089 when MCP server unreachable |
| Protocol | SPL queries | All data retrieval uses Splunk's native Search Processing Language |

### How AI models and agents are integrated

| Component | Role | Model |
|---|---|---|
| `LogAnalystAgent` | Searches error logs, finds exceptions, identifies first occurrence | Claude via LangChain `bind_tools()` |
| `MetricCorrelatorAgent` | Queries APM metrics, finds latency/error rate spikes, detects anomalies | Claude via LangChain `bind_tools()` |
| `DeployInspectorAgent` | Finds recent deployments, identifies guilty deploy via temporal correlation | Claude via LangChain `bind_tools()` |
| `ReportGeneratorAgent` | Synthesizes all findings into structured incident report | Claude (no tools, pure reasoning) |
| **Orchestrator** | Runs agents 1–3 in parallel via `asyncio.gather()`, then report generator | LangGraph-style state machine |

### Data flow between services

```
Developer Query
    │
    ├─→ FastAPI validates request (Pydantic)
    │
    ├─→ AgentState created (shared investigation context)
    │
    ├─→ [PARALLEL] Log Analyst
    │       └─→ MCP Tool: search_splunk_logs
    │               └─→ Splunk index=main → errors, stack traces
    │
    ├─→ [PARALLEL] Metric Correlator
    │       └─→ MCP Tool: search_splunk_metrics
    │               └─→ Splunk index=metrics → latency, error rate
    │       └─→ MCP Tool: detect_anomalies_in_splunk
    │               └─→ Splunk anomalydetection command
    │
    ├─→ [PARALLEL] Deploy Inspector
    │       └─→ MCP Tool: search_splunk_deployments
    │               └─→ Splunk index=deployments → deploy events
    │
    └─→ [SEQUENTIAL] Report Generator
            └─→ All 3 findings → Claude → TriageReport
                    └─→ JSON response → Chat UI renders report
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend framework | FastAPI + uvicorn |
| Agent orchestration | LangGraph / LangChain |
| LLM | Anthropic Claude (claude-sonnet-4-6) |
| Splunk integration | **Splunk MCP Server** + Splunk Python SDK |
| Data validation | Pydantic v2 |
| Frontend | Vanilla HTML/CSS/JS (single file) |
| Logging | structlog |
| Containerization | Docker + Docker Compose |