"""
splunk_mcp.py
Splunk Cloud client using REST API on port 443.
HEC (8088) = data ingest only.
REST API (443) = search queries.
"""

from __future__ import annotations

import json
import time
import asyncio
import base64
import urllib.request
import urllib.parse
import ssl
from typing import Any, Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from backend.config import settings
from backend.models.schemas import SplunkSearchResult


class SplunkMCPClient:
    """
    Queries Splunk Cloud via REST API on port 443.
    Splunk Cloud REST endpoint: https://<host>/services/...
    """

    def __init__(self) -> None:
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE
        # REST API always on 443 for Splunk Cloud
        self._base = f"https://{settings.splunk_host}"
        creds = f"{settings.splunk_username}:{settings.splunk_password}"
        self._auth_header = "Basic " + base64.b64encode(creds.encode()).decode()

    def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        url = f"{self._base}{path}"
        encoded = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(
            url,
            data=encoded,
            headers={
                "Authorization": self._auth_header,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=30) as r:
                body = r.read().decode()
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return {"raw": body}
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            return {"error": f"HTTP {e.code}: {body[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    def _run_search(self, spl_query: str, earliest: str, latest: str) -> list[dict]:
        q = spl_query.strip()
        if not q.startswith("search ") and not q.startswith("|"):
            q = "search " + q

        # Try oneshot first (faster, single request)
        data = {
            "search": q,
            "earliest_time": earliest,
            "latest_time": latest,
            "output_mode": "json",
            "exec_mode": "oneshot",
            "count": str(settings.splunk_max_results),
        }
        result = self._request("POST", "/services/search/jobs", data)

        if "error" in result:
            return [{"error": result["error"]}]

        # Oneshot returns results directly
        rows = result.get("results", [])
        if isinstance(rows, list):
            return rows

        # Fallback: blocking job
        data["exec_mode"] = "blocking"
        result = self._request("POST", "/services/search/jobs", data)
        if "error" in result:
            return [{"error": result["error"]}]

        sid = result.get("sid")
        if not sid:
            entry = result.get("entry", [{}])
            sid = entry[0].get("content", {}).get("sid") if entry else None

        if not sid:
            return [{"error": "No SID returned from Splunk"}]

        res = self._request(
            "GET",
            f"/services/search/jobs/{sid}/results?output_mode=json&count={settings.splunk_max_results}",
        )
        return res.get("results", [])

    async def search(
        self,
        spl_query: str,
        earliest: str = "-1h",
        latest: str = "now",
    ) -> SplunkSearchResult:
        t0 = time.perf_counter()
        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(
                None, self._run_search, spl_query, earliest, latest
            )
        except Exception as e:
            results = [{"error": str(e)}]

        return SplunkSearchResult(
            query=spl_query,
            result_count=len(results),
            results=results,
            earliest=earliest,
            latest=latest,
            duration_ms=round((time.perf_counter() - t0) * 1000, 2),
        )

    async def call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query", "search index=main | head 10")
        earliest = arguments.get("earliest_time", "-1h")
        latest = arguments.get("latest_time", "now")
        result = await self.search(query, earliest=earliest, latest=latest)
        return {"content": [{"type": "text", "text": json.dumps(result.results)}]}


_client: SplunkMCPClient | None = None

def get_splunk_client() -> SplunkMCPClient:
    global _client
    if _client is None:
        _client = SplunkMCPClient()
    return _client


# ── LangChain Tools ───────────────────────────────────────────────────────────

class SearchLogsInput(BaseModel):
    spl_query: str = Field(description="SPL query for Splunk logs index")
    earliest: str = Field(default="-1h")
    latest: str = Field(default="now")

class SearchLogsTool(BaseTool):
    name: str = "search_splunk_logs"
    description: str = "Search Splunk logs for errors, exceptions, and warnings."
    args_schema: Type[BaseModel] = SearchLogsInput

    def _run(self, spl_query: str, earliest: str = "-1h", latest: str = "now") -> str:
        raise NotImplementedError

    async def _arun(self, spl_query: str, earliest: str = "-1h", latest: str = "now") -> str:
        if "index=" not in spl_query:
            spl_query = f'index="{settings.splunk_index_logs}" {spl_query}'
        result = await get_splunk_client().search(spl_query, earliest=earliest, latest=latest)
        return json.dumps({"result_count": result.result_count, "results": result.results[:20], "query": result.query}, indent=2)


class SearchMetricsInput(BaseModel):
    spl_query: str = Field(description="SPL query for metrics index")
    earliest: str = Field(default="-1h")
    latest: str = Field(default="now")

class SearchMetricsTool(BaseTool):
    name: str = "search_splunk_metrics"
    description: str = "Search Splunk metrics for latency, error rates, throughput anomalies."
    args_schema: Type[BaseModel] = SearchMetricsInput

    def _run(self, spl_query: str, earliest: str = "-1h", latest: str = "now") -> str:
        raise NotImplementedError

    async def _arun(self, spl_query: str, earliest: str = "-1h", latest: str = "now") -> str:
        if "index=" not in spl_query:
            spl_query = f'index="{settings.splunk_index_metrics}" {spl_query}'
        result = await get_splunk_client().search(spl_query, earliest=earliest, latest=latest)
        return json.dumps({"result_count": result.result_count, "results": result.results[:20], "query": result.query}, indent=2)


class SearchDeploymentsInput(BaseModel):
    spl_query: str = Field(description="SPL query for deployments index")
    earliest: str = Field(default="-24h")
    latest: str = Field(default="now")

class SearchDeploymentsTool(BaseTool):
    name: str = "search_splunk_deployments"
    description: str = "Search Splunk deployments for recent releases and config changes."
    args_schema: Type[BaseModel] = SearchDeploymentsInput

    def _run(self, spl_query: str, earliest: str = "-24h", latest: str = "now") -> str:
        raise NotImplementedError

    async def _arun(self, spl_query: str, earliest: str = "-24h", latest: str = "now") -> str:
        if "index=" not in spl_query:
            spl_query = f'index="{settings.splunk_index_deploys}" {spl_query}'
        result = await get_splunk_client().search(spl_query, earliest=earliest, latest=latest)
        return json.dumps({"result_count": result.result_count, "results": result.results[:20], "query": result.query}, indent=2)


class AnomalyDetectionInput(BaseModel):
    metric_field: str = Field(description="Metric field to analyse")
    service: str = Field(description="Service name")
    earliest: str = Field(default="-2h")

class AnomalyDetectionTool(BaseTool):
    name: str = "detect_anomalies_in_splunk"
    description: str = "Detect anomalies in Splunk metrics using timechart."
    args_schema: Type[BaseModel] = AnomalyDetectionInput

    def _run(self, metric_field: str, service: str, earliest: str = "-2h") -> str:
        raise NotImplementedError

    async def _arun(self, metric_field: str, service: str, earliest: str = "-2h") -> str:
        spl = f'index="{settings.splunk_index_logs}" service="{service}" | timechart count span=5m'
        result = await get_splunk_client().search(spl, earliest=earliest, latest="now")
        return json.dumps({"result_count": result.result_count, "results": result.results[:20]}, indent=2)


def get_log_tools() -> list[BaseTool]:
    return [SearchLogsTool(), AnomalyDetectionTool()]

def get_metric_tools() -> list[BaseTool]:
    return [SearchMetricsTool(), AnomalyDetectionTool()]

def get_deploy_tools() -> list[BaseTool]:
    return [SearchDeploymentsTool()]

def get_all_tools() -> list[BaseTool]:
    return [SearchLogsTool(), SearchMetricsTool(), SearchDeploymentsTool(), AnomalyDetectionTool()]