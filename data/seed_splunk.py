"""
seed_splunk.py
Seeds Splunk with synthetic incident data using HEC (HTTP Event Collector).
Works with both Splunk Cloud and Splunk Enterprise.

Setup (do this once in Splunk UI):
  Settings → Data Inputs → HTTP Event Collector → New Token
  Give it access to indexes: main, metrics, deployments
  Copy the token and pass it as --hec-token

Usage:
  python data/seed_splunk.py --hec-token YOUR_TOKEN
  python data/seed_splunk.py --hec-token YOUR_TOKEN --scenario auth_failure
  python data/seed_splunk.py --hec-token YOUR_TOKEN --host myinstance.splunkcloud.com
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timedelta, timezone

import urllib.request
import urllib.error
import ssl


# ─────────────────────────────────────────────────────────────────────────────
# Config (overridden by CLI args)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_HOST = "prd-p-n3mr7.splunkcloud.com"
DEFAULT_HEC_PORT = 443           # Splunk Cloud uses 443
DEFAULT_HEC_TOKEN = ""           # Set via --hec-token

SERVICES = ["payment-service", "auth-service", "order-service", "api-gateway", "notification-service"]
HOSTS = ["prod-k8s-node-01", "prod-k8s-node-02", "prod-k8s-node-03"]
ENDPOINTS = {
    "payment-service": ["/api/v1/payments", "/api/v1/refunds", "/api/v1/subscriptions"],
    "auth-service": ["/api/v1/login", "/api/v1/refresh", "/api/v1/logout"],
    "order-service": ["/api/v1/orders", "/api/v1/orders/{id}", "/api/v1/cart"],
    "api-gateway": ["/health", "/metrics", "/api"],
    "notification-service": ["/api/v1/email", "/api/v1/sms", "/api/v1/push"],
}


# ─────────────────────────────────────────────────────────────────────────────
# HEC Client
# ─────────────────────────────────────────────────────────────────────────────

class HECClient:
    def __init__(self, host: str, port: int, token: str, use_ssl: bool = True):
        self.host = host
        self.port = port
        self.token = token
        scheme = "https" if use_ssl else "http"
        self.url = f"{scheme}://{host}:{port}/services/collector/event"
        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE

    def send_batch(self, events: list[dict]) -> bool:
        """Send a batch of events to HEC. Each event is a HEC payload dict."""
        # HEC batch format: newline-separated JSON objects
        body = "\n".join(json.dumps(e) for e in events).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Authorization": f"Splunk {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=15) as resp:
                result = json.loads(resp.read())
                return result.get("text") == "Success"
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"  HEC error {e.code}: {body}")
            return False
        except Exception as e:
            print(f"  HEC send failed: {e}")
            return False

    def test_connection(self) -> bool:
        """Send a single test event."""
        return self.send_batch([{
            "time": _unix_ts(_now()),
            "host": "seed-script",
            "source": "seed:test",
            "sourcetype": "generic_single_line",
            "index": "main",
            "event": "SplunkOps Copilot seed script connected successfully",
        }])


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _unix_ts(dt: datetime) -> float:
    return dt.timestamp()

def _metric_event(t: datetime, service: str, latency: float, error_rate: float) -> dict:
    rps = random.randint(80, 320)
    fields = {
        "service": service,
        "environment": "production",
        "response_time_ms": round(latency + random.uniform(-10, 10), 1),
        "error_rate_pct": round(error_rate + random.uniform(-0.3, 0.3), 2),
        "requests_per_second": rps,
        "p50_ms": round(latency * 0.9, 1),
        "p95_ms": round(latency * 1.4, 1),
        "p99_ms": round(latency * 2.1, 1),
        "cpu_pct": round(random.uniform(20, 85), 1),
        "memory_mb": random.randint(256, 1024),
        "db_connections_active": random.randint(1, 20),
    }
    return {
        "time": _unix_ts(t),
        "host": random.choice(HOSTS),
        "source": "app:metrics",
        "sourcetype": "app:metrics",
        "index": "metrics",
        "event": f"metrics service={service} response_time={latency:.0f}ms error_rate={error_rate:.1f}%",
        "fields": fields,
    }

def _log_event(t: datetime, service: str, level: str, message: str) -> dict:
    endpoints_list = ENDPOINTS.get(service, ["/api"])
    return {
        "time": _unix_ts(t),
        "host": random.choice(HOSTS),
        "source": "app:log",
        "sourcetype": "app:log",
        "index": "main",
        "event": message,
        "fields": {
            "service": service,
            "level": level,
            "environment": "production",
            "endpoint": random.choice(endpoints_list),
            "trace_id": f"trace-{random.randint(100000, 999999)}",
        },
    }

def _deploy_event(t: datetime, data: dict) -> dict:
    return {
        "time": _unix_ts(t),
        "host": "deploy-pipeline",
        "source": "deploy:event",
        "sourcetype": "deploy:event",
        "index": "deployments",
        "event": data.pop("message", json.dumps(data)),
        "fields": data,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Scenarios
# ─────────────────────────────────────────────────────────────────────────────

def scenario_payment_latency_spike() -> list[dict]:
    """
    Bad deploy at 2h ago introduced N+1 DB query in payment-service.
    Latency: 80ms → 4500ms. Error rate spiked to 34%.
    """
    now = _now()
    deploy_time = now - timedelta(hours=2)
    incident_start = now - timedelta(hours=1, minutes=30)
    events = []

    # Deploy events
    events.append(_deploy_event(deploy_time, {
        "event_type": "deployment",
        "service": "payment-service",
        "version": "v2.14.3",
        "previous_version": "v2.14.2",
        "deployed_by": "github-actions",
        "environment": "production",
        "commit_sha": "a3f8b2c",
        "commit_message": "feat: cache payment method lookups for performance",
        "duration_seconds": 142,
        "status": "success",
        "message": "Deployment of payment-service v2.14.3 to production completed",
    }))

    events.append(_deploy_event(now - timedelta(hours=5), {
        "event_type": "deployment",
        "service": "notification-service",
        "version": "v1.8.1",
        "deployed_by": "github-actions",
        "environment": "production",
        "status": "success",
        "message": "Deployment of notification-service v1.8.1 to production completed",
    }))

    # Healthy baseline metrics (2h before deploy)
    for i in range(24):
        t = deploy_time - timedelta(minutes=5 * (24 - i))
        events.append(_metric_event(t, "payment-service", latency=random.randint(65, 95), error_rate=0.5))
        events.append(_metric_event(t, "auth-service", latency=random.randint(30, 55), error_rate=0.2))

    # Gradual creep post-deploy
    for i in range(6):
        t = deploy_time + timedelta(minutes=5 * i)
        events.append(_metric_event(t, "payment-service", latency=95 + (i * 80), error_rate=0.5 + (i * 1.2)))

    # Full spike metrics
    for i in range(12):
        t = incident_start + timedelta(minutes=5 * i)
        events.append(_metric_event(t, "payment-service", latency=random.randint(3800, 5200), error_rate=random.uniform(28, 38)))
        events.append(_metric_event(t, "api-gateway", latency=random.randint(200, 600), error_rate=random.uniform(10, 15)))

    # Healthy logs before incident
    for i in range(30):
        t = deploy_time - timedelta(minutes=random.randint(5, 120))
        events.append(_log_event(t, "payment-service", "INFO",
            f"Payment processed successfully id=PAY-{random.randint(10000,99999)} latency=82ms"))

    # First warning signs
    first_warn = deploy_time + timedelta(minutes=18)
    events.append(_log_event(first_warn, "payment-service", "WARNING",
        "DB query taking longer than expected: SELECT * FROM payment_methods latency=980ms"))
    events.append(_log_event(first_warn + timedelta(seconds=45), "payment-service", "WARNING",
        "DB query taking longer than expected: SELECT * FROM payment_methods latency=1240ms"))

    # Error storm
    error_messages = [
        "TimeoutException: Payment gateway did not respond within 5000ms at PaymentService.charge(PaymentService.java:142)",
        "DatabaseException: Too many connections. Pool exhausted (max=20, active=20, idle=0)",
        "HTTPException: Upstream payment-service returned 503 Service Unavailable",
        "N+1 Query detected: payment_methods table queried 47 times in single request",
        "CircuitBreaker OPEN for payment-service after 5 consecutive failures",
    ]
    for i in range(80):
        t = incident_start + timedelta(seconds=random.randint(0, 5400))
        service = random.choice(["payment-service", "payment-service", "payment-service", "api-gateway"])
        level = random.choice(["ERROR", "ERROR", "ERROR", "CRITICAL", "WARNING"])
        events.append(_log_event(t, service, level, random.choice(error_messages)))

    return events


def scenario_auth_failure() -> list[dict]:
    now = _now()
    incident_start = now - timedelta(minutes=45)
    events = []

    events.append(_deploy_event(incident_start - timedelta(minutes=5), {
        "event_type": "config_change",
        "service": "auth-service",
        "changed_by": "ops-team",
        "config_key": "JWT_SIGNING_KEY_ROTATION",
        "description": "Rotated JWT signing key - quarterly security review",
        "environment": "production",
        "message": "Config change: JWT_SIGNING_KEY_ROTATION applied to auth-service",
    }))

    for i in range(10):
        t = incident_start - timedelta(minutes=5 * i)
        events.append(_metric_event(t, "auth-service", latency=42, error_rate=0.1))

    for i in range(9):
        t = incident_start + timedelta(minutes=5 * i)
        events.append(_metric_event(t, "auth-service", latency=38, error_rate=random.uniform(45, 65)))

    for i in range(30):
        t = incident_start + timedelta(seconds=random.randint(0, 2700))
        events.append(_log_event(t, "auth-service", "ERROR",
            "JWT validation failed: signature verification failed for token issued before key rotation"))

    return events


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS = {
    "payment_latency": scenario_payment_latency_spike,
    "auth_failure": scenario_auth_failure,
}

BATCH_SIZE = 20  # HEC batch size


def main():
    parser = argparse.ArgumentParser(description="Seed Splunk Cloud with synthetic incident data via HEC")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Splunk host")
    parser.add_argument("--port", type=int, default=DEFAULT_HEC_PORT, help="HEC port (443 for Splunk Cloud)")
    parser.add_argument("--hec-token", required=True, help="Splunk HEC token (Settings → Data Inputs → HTTP Event Collector)")
    parser.add_argument("--scenario", choices=list(SCENARIOS.keys()), default="payment_latency")
    parser.add_argument("--no-ssl", action="store_true", help="Disable SSL (for local Splunk)")
    args = parser.parse_args()

    client = HECClient(
        host=args.host,
        port=args.port,
        token=args.hec_token,
        use_ssl=not args.no_ssl,
    )

    print(f"Testing HEC connection to {args.host}:{args.port}…")
    if not client.test_connection():
        print("\n❌ HEC connection failed. Check:")
        print("  1. HEC is enabled: Settings → Data Inputs → HTTP Event Collector → Global Settings → Enabled")
        print("  2. Token is correct")
        print("  3. Token has access to indexes: main, metrics, deployments")
        return

    print("✅ HEC connected!")
    print(f"\nGenerating scenario: {args.scenario}")
    events = SCENARIOS[args.scenario]()
    print(f"Generated {len(events)} events. Sending in batches of {BATCH_SIZE}…")

    success = 0
    for i in range(0, len(events), BATCH_SIZE):
        batch = events[i:i + BATCH_SIZE]
        if client.send_batch(batch):
            success += len(batch)
        else:
            print(f"  Batch {i//BATCH_SIZE + 1} failed")
        time.sleep(0.1)

    print(f"\n{'✅' if success == len(events) else '⚠️'} Sent {success}/{len(events)} events")
    print("\nSplunk takes ~30s to index. Then run:")
    print('  index=main level=ERROR | head 10')
    print("\nReady query for SplunkOps Copilot:")
    print('  "Payment service throwing 500s after the latest deploy. Response times spiked from 80ms to 4500ms."')


if __name__ == "__main__":
    main()