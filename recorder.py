"""Tiny client helper for recording actions from a Python agent."""
from typing import Any
import requests

class AgentBlackBoxRecorder:
    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url.rstrip("/")
        self.run_id = None

    def start_run(self, name: str, agent: str = "Python Agent") -> str:
        response = requests.post(f"{self.base_url}/api/runs", json={"name": name, "agent": agent}, timeout=10)
        response.raise_for_status()
        self.run_id = response.json()["id"]
        return self.run_id

    def record(self, title: str, *, event_type: str = "tool_call", status: str = "success",
               details: dict[str, Any] | None = None, evidence: str = "") -> dict:
        if not self.run_id:
            raise RuntimeError("Call start_run() before record().")
        response = requests.post(f"{self.base_url}/api/runs/{self.run_id}/events",
            json={"title": title, "event_type": event_type, "status": status,
                  "details": details or {}, "evidence": evidence}, timeout=10)
        response.raise_for_status()
        return response.json()

    def finish(self) -> dict:
        if not self.run_id:
            raise RuntimeError("No active run.")
        response = requests.post(f"{self.base_url}/api/runs/{self.run_id}/finish", timeout=10)
        response.raise_for_status()
        return response.json()
