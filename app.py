
from __future__ import annotations

import ipaddress
import json
import re
import socket
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "agentblackbox.db"

app = FastAPI(title="AgentBlackBox", version="0.3.0")


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                agent TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                summary TEXT DEFAULT ''
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                details TEXT NOT NULL,
                evidence TEXT DEFAULT '',
                FOREIGN KEY(run_id) REFERENCES runs(id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS website_baselines (
                url TEXT PRIMARY KEY,
                http_status INTEGER NOT NULL,
                response_time_ms REAL,
                page_title TEXT,
                updated_at TEXT NOT NULL
            )
        """)


init_db()


# --------------------------------------------------
# REQUEST MODELS
# --------------------------------------------------

class RunIn(BaseModel):
    name: str = Field(default="Untitled run", max_length=200)
    agent: str = Field(default="Demo Agent", max_length=100)


class EventIn(BaseModel):
    event_type: str = Field(default="tool_call", max_length=60)
    title: str = Field(min_length=1, max_length=200)
    status: str = "success"
    details: dict = Field(default_factory=dict)
    evidence: str = Field(default="", max_length=5000)


class URLTestIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def redact(value):
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if re.search(
                    r"api.?key|token|password|secret|authorization",
                    str(key),
                    re.I,
                )
                else redact(val)
            )
            for key, val in value.items()
        }

    if isinstance(value, list):
        return [redact(item) for item in value]

    if isinstance(value, str):
        return re.sub(
            r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*"
            r"""['"]?[^'"\s,]+""",
            r"\1=[REDACTED]",
            value,
        )

    return value


def create_run(name, agent):
    run_id = "run_" + uuid.uuid4().hex[:12]

    with db() as c:
        c.execute(
            """INSERT INTO runs
               (id, name, agent, status, started_at, summary)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                name,
                agent,
                "running",
                now_iso(),
                "Execution started",
            ),
        )

    return run_id


def add_event(
    run_id,
    seq,
    event_type,
    title,
    status="success",
    details=None,
    evidence="",
):
    event_id = str(uuid.uuid4())

    with db() as c:
        c.execute(
            """INSERT INTO events
               (id, run_id, seq, timestamp, event_type, title,
                status, details, evidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                run_id,
                seq,
                now_iso(),
                event_type,
                title,
                status,
                json.dumps(redact(details or {})),
                evidence,
            ),
        )

    return event_id


def record_event(run_id, event_type, title, status, details, evidence):
    with db() as c:
        row = c.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=?",
            (run_id,),
        ).fetchone()
        seq = row[0]

    return add_event(
        run_id, seq, event_type, title, status, details, evidence
    )


def finish_run(run_id, status, summary):
    with db() as c:
        c.execute(
            "UPDATE runs SET status=?, summary=? WHERE id=?",
            (status, summary, run_id),
        )


def classify_http_failure(status_code=None, exception=None, regression=False):
    """Classify failures using explicit, evidence-based rules."""

    if isinstance(exception, requests.exceptions.Timeout):
        return {
            "category": "TIMEOUT",
            "severity": "high",
            "reason": "The HTTP request exceeded its timeout limit.",
        }

    if isinstance(exception, requests.exceptions.SSLError):
        return {
            "category": "TLS_ERROR",
            "severity": "high",
            "reason": "The TLS/SSL connection could not be verified.",
        }

    if isinstance(exception, requests.exceptions.ConnectionError):
        message = str(exception).lower()

        if any(term in message for term in (
            "name resolution",
            "getaddrinfo failed",
            "nodename nor servname",
            "name or service not known",
        )):
            category = "DNS_FAILURE"
            reason = "The hostname could not be resolved."
        else:
            category = "CONNECTION_FAILURE"
            reason = "The connection to the website failed."

        return {
            "category": category,
            "severity": "high",
            "reason": reason,
        }

    if exception is not None:
        return {
            "category": "REQUEST_FAILURE",
            "severity": "high",
            "reason": "The HTTP request failed.",
        }

    if status_code is not None and status_code >= 500:
        return {
            "category": "HTTP_SERVER_ERROR",
            "severity": "high",
            "reason": f"The server returned HTTP {status_code}.",
        }

    if status_code is not None and status_code >= 400:
        return {
            "category": "HTTP_CLIENT_ERROR",
            "severity": "medium",
            "reason": f"The server returned HTTP {status_code}.",
        }

    if status_code is not None and status_code >= 300:
        return {
            "category": "HTTP_REDIRECT",
            "severity": "low",
            "reason": f"The server returned HTTP {status_code}.",
        }

    if regression:
        return {
            "category": "PERFORMANCE_REGRESSION",
            "severity": "medium",
            "reason": "A configured baseline comparison threshold was exceeded.",
        }

    return {
        "category": "NONE",
        "severity": "low",
        "reason": "No configured failure condition was detected.",
    }


def fetch_run(run_id):
    with db() as c:
        run = c.execute(
            "SELECT * FROM runs WHERE id=?", (run_id,)
        ).fetchone()

        if not run:
            raise HTTPException(404, "Run not found")

        events = c.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY seq ASC",
            (run_id,),
        ).fetchall()

    result_events = []

    for event in events:
        item = dict(event)
        item["details"] = json.loads(item["details"])
        result_events.append(item)

    failures = [
        event for event in result_events
        if event["status"] in ("failure", "warning")
    ]

    if failures:
        headline = failures[-1]["title"]
        explanation = (
            "A failure or warning was recorded. Inspect the evidence "
            "before concluding the cause."
        )
        level = (
            "high"
            if any(e["status"] == "failure" for e in failures)
            else "medium"
        )
        recommendation = (
            "Inspect the failed event, reproduce the check, and verify "
            "the result after making a correction."
        )
    else:
        headline = "No recorded failures detected"
        explanation = "No events marked as failures or warnings were found."
        level = "low"
        recommendation = "Continue monitoring and add relevant checks."

    return {
        "run": dict(run),
        "events": result_events,
        "analysis": {
            "level": level,
            "headline": headline,
            "explanation": explanation,
            "evidence": failures,
            "recommendation": recommendation,
        },
    }


# --------------------------------------------------
# SAFE PUBLIC URL VALIDATION
# --------------------------------------------------

def validate_public_url(url):
    url = url.strip()
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Only HTTP and HTTPS URLs are allowed.")

    if not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "Invalid URL or embedded credentials.")

    try:
        port = parsed.port
    except ValueError:
        raise HTTPException(400, "Invalid URL port.")

    expected_port = 443 if parsed.scheme == "https" else 80

    if port not in (None, expected_port):
        raise HTTPException(
            400, "Only standard HTTP/HTTPS ports are allowed."
        )

    host = parsed.hostname.rstrip(".").lower()

    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
    ):
        raise HTTPException(400, "Local destinations are blocked.")

    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(
                host, port or expected_port, type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror:
        raise HTTPException(400, "Could not resolve the website hostname.")

    if not addresses:
        raise HTTPException(400, "No IP addresses found.")

    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise HTTPException(
                400, "Private or non-public destinations are blocked."
            )

    return url


# --------------------------------------------------
# HOME AND GENERAL API
# --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home():
    index_file = BASE / "static" / "index.html"

    if not index_file.exists():
        raise HTTPException(500, "static/index.html was not found")

    return index_file.read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "product": "AgentBlackBox",
        "version": "0.3.0",
    }


@app.get("/api/runs")
def list_runs():
    with db() as c:
        rows = c.execute("""
            SELECT r.*,
              (SELECT COUNT(*) FROM events e WHERE e.run_id=r.id)
                AS event_count
            FROM runs r
            ORDER BY started_at DESC
        """).fetchall()

    return [dict(row) for row in rows]


@app.post("/api/runs")
def create_run_endpoint(payload: RunIn):
    run_id = create_run(payload.name, payload.agent)
    return {"id": run_id, "status": "running"}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return fetch_run(run_id)


@app.post("/api/runs/{run_id}/events")
def create_event(run_id: str, payload: EventIn):
    with db() as c:
        exists = c.execute(
            "SELECT id FROM runs WHERE id=?", (run_id,)
        ).fetchone()

    if not exists:
        raise HTTPException(404, "Run not found")

    if payload.status not in ("success", "warning", "failure"):
        raise HTTPException(
            400, "Status must be success, warning, or failure"
        )

    event_id = record_event(
        run_id,
        payload.event_type,
        payload.title,
        payload.status,
        payload.details,
        payload.evidence,
    )

    return {"id": event_id, "status": "recorded"}


@app.post("/api/runs/{run_id}/finish")
def finish_run_endpoint(run_id: str):
    with db() as c:
        run = c.execute(
            "SELECT id FROM runs WHERE id=?", (run_id,)
        ).fetchone()

        events = c.execute(
            "SELECT status FROM events WHERE run_id=?", (run_id,)
        ).fetchall()

    if not run:
        raise HTTPException(404, "Run not found")

    statuses = [event["status"] for event in events]

    if "failure" in statuses or "warning" in statuses:
        status, summary = "incident", "Execution contains a failure or warning"
    else:
        status, summary = "success", "Execution finished"

    finish_run(run_id, status, summary)
    return {"id": run_id, "status": status}


@app.delete("/api/runs")
def clear_runs():
    with db() as c:
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM runs")

    return {"status": "cleared"}


@app.post("/api/demo")
def create_demo():
    run_id = create_run(
        "Migration file regression", "AgentBlackBox Demo Agent"
    )

    record_event(
        run_id, "inspection", "Read source files", "success",
        {"files_read": 3}, "Demo evidence: source files inspected."
    )
    record_event(
        run_id, "file_change", "Modified database initialization",
        "success", {"file": "database.py"},
        "Demo evidence: initialization configuration updated."
    )
    record_event(
        run_id, "test", "Executed unit tests: 12 passed",
        "success", {"passed": 12, "failed": 0},
        "Demo evidence: unit test summary."
    )
    record_event(
        run_id, "file_change", "Deleted old migration file",
        "warning", {"file": "migrations/001_init.sql"},
        "Illustrative destructive action in demo data."
    )
    record_event(
        run_id, "agent_report", "Agent reported task completed",
        "success", {"reported_success": True},
        "Illustrative agent report."
    )
    record_event(
        run_id, "verification", "Clean application startup not verified",
        "failure", {"startup_verified": False},
        "Illustrative verification gap; not an actual application test."
    )

    finish_run(
        run_id, "incident",
        "Demo incident: startup verification was missing"
    )

    return {"id": run_id, "status": "incident"}


# --------------------------------------------------
# URL INCIDENT TESTER + BASELINE COMPARISON
# --------------------------------------------------

@app.post("/api/test-url")
def test_url(payload: URLTestIn):
    url = validate_public_url(payload.url)
    parsed = urlparse(url)
    hostname = parsed.hostname

    baseline_url = url.split("#", 1)[0].rstrip("/")

    if not parsed.path and not parsed.query:
        baseline_url = f"{parsed.scheme}://{parsed.netloc}"

    run_id = create_run(
        f"Website test: {hostname}",
        "AgentBlackBox URL Tester",
    )

    record_event(
        run_id,
        "url_validation",
        "Public URL validation passed",
        "success",
        {"hostname": hostname},
        "URL scheme, port and resolved IP addresses were checked.",
    )

    http_status = None
    response_time_ms = None
    page_title = None
    content_type = None
    regression = False
    regression_reasons = []
    baseline = None
    failure_info = classify_http_failure()

    try:
        started = time.perf_counter()

        with requests.get(
            url,
            timeout=(3, 7),
            allow_redirects=False,
            stream=True,
            headers={"User-Agent": "AgentBlackBox-URLTester/0.3"},
        ) as response:
            response_time_ms = round(
                (time.perf_counter() - started) * 1000, 2
            )
            http_status = response.status_code
            content_type = response.headers.get("content-type", "")
            html_bytes = b""

            if "text/html" in content_type.lower():
                for chunk in response.iter_content(chunk_size=4096):
                    if not chunk:
                        continue

                    html_bytes += chunk[:max(0, 65536 - len(html_bytes))]

                    if len(html_bytes) >= 65536:
                        break

            if html_bytes:
                html = html_bytes.decode("utf-8", errors="replace")
                match = re.search(
                    r"<title[^>]*>(.*?)</title>",
                    html,
                    flags=re.IGNORECASE | re.DOTALL,
                )

                if match:
                    page_title = re.sub(
                        r"\s+", " ", match.group(1)
                    ).strip()[:200]

        with db() as c:
            row = c.execute(
                "SELECT * FROM website_baselines WHERE url=?",
                (baseline_url,),
            ).fetchone()
            baseline = dict(row) if row else None

        if 200 <= http_status < 300:
            if baseline is None:
                with db() as c:
                    c.execute(
                        """INSERT OR IGNORE INTO website_baselines
                           (url, http_status, response_time_ms,
                            page_title, updated_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            baseline_url,
                            http_status,
                            response_time_ms,
                            page_title,
                            now_iso(),
                        ),
                    )

                    row = c.execute(
                        "SELECT * FROM website_baselines WHERE url=?",
                        (baseline_url,),
                    ).fetchone()
                    baseline = dict(row) if row else None

                record_event(
                    run_id,
                    "baseline",
                    "Initial healthy baseline established",
                    "success",
                    {
                        "url": baseline_url,
                        "http_status": http_status,
                        "response_time_ms": response_time_ms,
                        "page_title": page_title,
                    },
                    "First successful 2xx response saved as baseline.",
                )

                summary = (
                    f"HTTP {http_status}: initial baseline established"
                )
                run_status = "success"

            else:
                old_status = baseline["http_status"]
                old_time = baseline["response_time_ms"]
                old_title = baseline["page_title"]

                if old_status != http_status:
                    regression = True
                    regression_reasons.append(
                        f"HTTP status changed from {old_status} to {http_status}"
                    )

                if (
                    old_time is not None
                    and response_time_ms > old_time * 1.5
                    and response_time_ms - old_time >= 300
                ):
                    regression = True
                    regression_reasons.append(
                        f"Response time increased from {old_time:.2f} ms "
                        f"to {response_time_ms:.2f} ms"
                    )

                if old_title and page_title and old_title != page_title:
                    regression = True
                    regression_reasons.append(
                        "Page title changed from the saved baseline"
                    )

                failure_info = classify_http_failure(
                    status_code=http_status,
                    regression=regression,
                )

                record_event(
                    run_id,
                    "baseline_comparison",
                    "Compared current result with saved baseline",
                    "failure" if regression else "success",
                    {
                        "url": baseline_url,
                        "baseline_http_status": old_status,
                        "current_http_status": http_status,
                        "baseline_response_time_ms": old_time,
                        "current_response_time_ms": response_time_ms,
                        "baseline_page_title": old_title,
                        "current_page_title": page_title,
                        "regression_detected": regression,
                        "failure_category": failure_info["category"],
                        "severity": failure_info["severity"],
                        "reasons": regression_reasons,
                    },
                    "A detected change is a signal to investigate, not proof of causality.",
                )

                if regression:
                    summary = "Potential website regression detected"
                    run_status = "incident"
                else:
                    summary = (
                        f"HTTP {http_status}: no regression threshold exceeded"
                    )
                    run_status = "success"

        elif 300 <= http_status < 400:
            failure_info = classify_http_failure(status_code=http_status)
            summary = f"Redirect detected: HTTP {http_status}"
            run_status = "incident"

        else:
            failure_info = classify_http_failure(status_code=http_status)
            summary = (
                f"{failure_info['category']}: {failure_info['reason']}"
            )
            run_status = "incident"

        http_event_status = (
            "failure" if http_status >= 400
            else "warning" if http_status >= 300 or regression
            else "success"
        )

        record_event(
            run_id,
            "http_check",
            f"HTTP response: {http_status}",
            http_event_status,
            {
                "url": url,
                "http_status": http_status,
                "response_time_ms": response_time_ms,
                "page_title": page_title,
                "content_type": content_type,
                "failure_category": failure_info["category"],
                "severity": failure_info["severity"],
                "failure_reason": failure_info["reason"],
            },
            "Observed HTTP response; redirects are not followed.",
        )

        if regression:
            record_event(
                run_id,
                "regression",
                "Potential regression detected",
                "failure",
                {
                    "reasons": regression_reasons,
                    "baseline_url": baseline_url,
                    "failure_category": "PERFORMANCE_REGRESSION",
                },
                "At least one configured comparison threshold was exceeded.",
            )

    except requests.exceptions.RequestException as exc:
        failure_info = classify_http_failure(exception=exc)
        summary = f"{failure_info['category']}: {failure_info['reason']}"
        run_status = "incident"

        record_event(
            run_id,
            "http_check",
            "Website request failed",
            "failure",
            {
                "url": url,
                "failure_category": failure_info["category"],
                "severity": failure_info["severity"],
                "error_type": type(exc).__name__,
            },
            failure_info["reason"],
        )

    finish_run(run_id, run_status, summary)
    saved = fetch_run(run_id)

    return {
        "run_id": run_id,
        "summary": summary,
        "http_status": http_status,
        "response_time_ms": response_time_ms,
        "page_title": page_title,
        "status": run_status,
        "event_count": len(saved["events"]),
        "baseline": baseline,
        "regression_detected": regression,
        "regression_reasons": regression_reasons,
        "failure_category": failure_info["category"],
        "failure_severity": failure_info["severity"],
        "failure_reason": failure_info["reason"],
    }
