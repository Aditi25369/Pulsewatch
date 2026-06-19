# Pulsewatch

> **Agentic incident response for Splunk environments.** Describe what's wrong in plain English — get a root cause, timeline, and remediation plan in under 60 seconds.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![Splunk MCP](https://img.shields.io/badge/Splunk-MCP%20Server-green)](https://dev.splunk.com)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)](https://fastapi.tiangolo.com)

---

## The problem

When a production incident fires, engineers burn 20–40 minutes manually correlating logs, checking metrics, and digging through deploy history before they even know what broke. That time is pure overhead — it doesn't fix anything.

**Pulsewatch removes that overhead.** It's a multi-agent system that investigates Splunk data the way a senior SRE would: in parallel, methodically, and fast.

> *"Payment service throwing 500s and response time spiked after the 3am deploy"*

Pulsewatch returns a structured triage report — root cause hypothesis with confidence score, incident timeline, prioritized remediation steps, and a ready-to-paste SPL query for ongoing monitoring.

---

## How it works

Three specialist agents investigate concurrently, then a fourth synthesizes their findings:

```
                    ┌──────────────────┐
                    │  Developer query │
                    └────────┬─────────┘
                             ▼
                  ┌──────────────────────┐
                  │   FastAPI + SSE      │
                  └──────────┬───────────┘
                             ▼
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                     ▼
 ┌─────────────┐    ┌──────────────────┐   ┌─────────────────┐
 │ Log Analyst │    │ Metric Correlator│   │ Deploy Inspector│
 │             │    │                  │   │                 │
 │ Errors,     │    │ Latency spikes,  │   │ Recent deploys, │
 │ exceptions, │    │ error rate       │   │ config changes, │
 │ stack traces│    │ anomalies        │   │ guilty commits  │
 └──────┬──────┘    └────────┬─────────┘   └────────┬────────┘
        │                    │                       │
        └────────────────────┼───────────────────────┘
                             ▼
                    ┌──────────────────┐
                    │ Report Generator │
                    │ (synthesis only) │
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐
                    │  Triage Report   │
                    │  + Live SSE feed │
                    └──────────────────┘
```

Each investigative agent talks to Splunk through a dedicated tool layer (`splunk_mcp.py`), built around the Splunk MCP Server pattern — agents call structured tools, not raw HTTP. The UI streams every agent's progress live over Server-Sent Events, so you watch the investigation happen instead of staring at a spinner.

Full system diagram: [`architecture_diagram.md`](architecture_diagram.md)

---

## What makes this different

- **Built for Splunk Cloud, not just local Splunk.** Handles the real-world constraints of Splunk Cloud (blocked search ports, HEC-only ingest, REST auth quirks) rather than assuming a clean local dev box.
- **Live agent telemetry, not a black box.** SSE streaming shows exactly which agent is running, what it queried, and what it found — in real time.
- **Graceful degradation.** If one agent fails (rate limits, timeouts, bad data), the orchestrator still produces a report from whatever succeeded, with an honest confidence score.
- **Free-tier friendly.** Runs on Groq's free LLM tier by default — no paid API key required to try it.

---

## Demo

[![Demo Video](https://img.shields.io/badge/Watch-Demo%20Video-red)](https://youtube.com/YOUR_LINK)

**Scenario:** Payment service latency spike (80ms → 4500ms) caused by a deploy that introduced an N+1 database query. Pulsewatch correlates the error logs, the metric degradation, and the deploy timestamp to identify the root cause and recommend a rollback.

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (async), Server-Sent Events |
| Agent orchestration | LangChain + custom async orchestrator |
| LLM | Groq (Llama 3.3 70B) — swappable for Anthropic Claude or OpenAI |
| Splunk integration | REST Search API + HEC, MCP-style tool abstraction |
| Validation | Pydantic v2 |
| Frontend | Vanilla HTML/CSS/JS, SSE consumer, single file |
| Containerization | Docker (multi-stage build), Docker Compose |
| Logging | structlog |

---

## Setup

### Prerequisites

- Python 3.11+
- A Splunk instance (Cloud or Enterprise) with HEC enabled
- A free [Groq API key](https://console.groq.com) (or Anthropic/OpenAI if you prefer)

### Install

```bash
git clone https://github.com/Aditi25369/pulsewatch
cd pulsewatch
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
pip install langchain-groq==0.2.4
```

### Configure

```bash
cp .env.example .env
```

Fill in `.env`:
```env
GROQ_API_KEY=gsk_your_key_here
SPLUNK_HOST=your-instance.splunkcloud.com
SPLUNK_PORT=8088
SPLUNK_USERNAME=your_username
SPLUNK_PASSWORD=your_password
SPLUNK_SCHEME=https
LLM_PROVIDER=groq
LLM_MODEL=llama-3.3-70b-versatile
AGENT_TIMEOUT_SECONDS=120
SPLUNK_INDEX_LOGS=main
SPLUNK_INDEX_METRICS=metrics
SPLUNK_INDEX_DEPLOYS=deployments
```

### Seed demo data (optional)

Pulsewatch ships with a synthetic incident generator so you can demo it without real production data:

```bash
python data/seed_splunk.py --hec-token YOUR_HEC_TOKEN --host your-instance.splunkcloud.com
```

This seeds a realistic payment-service latency incident: healthy baseline → bad deploy → gradual degradation → full error storm.

### Run

```bash
python -m uvicorn backend.main:app --reload --port 8000
```

Open `http://localhost:8000`.

### Run with Docker

```bash
docker-compose up --build
```

---

## Usage

### Web UI

Type an incident description, optionally scope it to a service and time window, and watch the agents investigate live before the report renders.

### REST API

```bash
# Streaming investigation (recommended — shows live agent progress)
curl -N -X POST http://localhost:8000/api/triage/stream \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Payment service throwing 500s after latest deploy",
    "service": "payment-service",
    "time_window_minutes": 60
  }'

# Non-streaming investigation
curl -X POST http://localhost:8000/api/triage \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Payment service throwing 500s after latest deploy",
    "service": "payment-service",
    "time_window_minutes": 60
  }'

# Follow-up question on a past investigation
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "abc12345",
    "message": "Show me the exact error stack trace"
  }'

# Splunk connectivity check
curl http://localhost:8000/api/splunk/status
```

Interactive API docs: `http://localhost:8000/api/docs`

---

## Project structure

```
pulsewatch/
├── backend/
│   ├── main.py                  # FastAPI app, REST + SSE endpoints
│   ├── config.py                # Settings (pydantic-settings)
│   ├── streaming.py             # SSE event stream manager
│   ├── agents/
│   │   ├── orchestrator.py      # Parallel agent execution + synthesis
│   │   ├── log_analyst.py       # Agent: error log investigation
│   │   ├── metric_correlator.py # Agent: latency/error rate anomalies
│   │   ├── deploy_inspector.py  # Agent: deploy/config correlation
│   │   └── report_generator.py  # Agent: final report synthesis
│   ├── tools/
│   │   └── splunk_mcp.py        # Splunk REST/HEC client + tool wrappers
│   └── models/
│       └── schemas.py           # Pydantic request/response/state models
├── frontend/
│   └── index.html               # Chat UI with live SSE progress panel
├── data/
│   └── seed_splunk.py           # Synthetic incident data generator
├── architecture_diagram.md
├── Dockerfile                   # Multi-stage build, healthcheck
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Example output

```json
{
  "severity": "high",
  "title": "Payment Service 500 Errors After Latest Deploy",
  "root_cause": {
    "hypothesis": "Recent deployment introduced an N+1 query on payment_methods table",
    "confidence": 0.80,
    "evidence": [
      "Deploy v2.14.3 committed 18 minutes before first error",
      "DB connection pool exhausted (20/20 active) at incident start",
      "Response time degraded from 80ms to 4500ms within 30 minutes of deploy"
    ]
  },
  "remediation_steps": [
    { "priority": 1, "action": "Roll back to previous deploy", "rationale": "Immediately restores service" },
    { "priority": 2, "action": "Add eager loading for payment_methods", "rationale": "Fixes root cause" }
  ]
}
```

---

## Known limitations

- In-memory session/report storage — restarting the server clears history (Redis/Postgres planned)
- No authentication layer yet — intended for local/demo use, not multi-tenant production
- Splunk Cloud's free trial tier blocks direct port 8089 access; Pulsewatch routes around this via REST API (443) and HEC (8088), but this means agents query Splunk Cloud's public search endpoint rather than a dedicated MCP server process

## Roadmap

- [ ] Redis + PostgreSQL for persistent investigation history
- [ ] JWT authentication and role-based access
- [ ] React frontend with investigation history dashboard
- [ ] Slack/Teams integration for incident notifications
- [ ] Evaluation framework for root-cause accuracy
- [ ] OpenTelemetry tracing across agent calls

---

## License

MIT — see [LICENSE](LICENSE)

---

*Built for the Splunk Agentic Ops Hackathon 2026 — Platform & Developer Experience track.*