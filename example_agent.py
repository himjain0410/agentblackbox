"""Example showing how an agent or script can emit trace events."""
from recorder import AgentBlackBoxRecorder

recorder = AgentBlackBoxRecorder()
run_id = recorder.start_run("Example app update", "Example Python Agent")
recorder.record("Read source files", event_type="inspection",
                details={"files": ["app.py", "tests/test_app.py"]},
                evidence="Read-only inspection completed")
recorder.record("Executed unit tests: 12 passed", event_type="test",
                details={"passed": 12, "failed": 0}, evidence="pytest summary")
recorder.record("Deleted old migration file", event_type="file_change", status="warning",
                details={"file": "db/migrations/001_init.sql", "operation": "delete"},
                evidence="Example event only; no file was actually deleted")
recorder.record("Agent reported task completed", event_type="agent_report",
                details={"claim": "success"}, evidence="Agent output captured")
print("Run:", run_id)
print("Finish:", recorder.finish())
print("Open http://127.0.0.1:8000 to inspect the trace.")
