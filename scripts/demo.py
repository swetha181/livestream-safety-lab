"""Exercise the real API in-process; no internet, streaming account or live data."""
import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient
from safety_lab.api import create_app

ROOT = Path(__file__).resolve().parents[1]


def run():
    clock = [datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc).timestamp()]
    with tempfile.TemporaryDirectory() as temp:
        app = create_app(Path(temp)/"demo.db", api_key="demo-test-key", clock=lambda: clock[0])
        with TestClient(app) as client:
            headers = {"X-API-Key": "demo-test-key"}
            events = [
                {"event_id": "normal", "user_id": "friendly", "text": "Great stream today!"},
                {"event_id": "link", "user_id": "linker", "text": "Claim bonus coins at https://rewards.example/claim"},
                {"event_id": "phrase", "user_id": "unkind", "text": "you are worthless"},
                *[{"event_id": f"repeat{i}", "user_id": "repeater", "text": "Visit my channel"} for i in range(1, 4)],
                *[{"event_id": f"burst{i}", "user_id": "busy", "text": f"Quick message number {i}"} for i in range(1, 7)],
                {"event_id": "context", "user_id": "educator", "text": 'Please report anyone saying "you are worthless".'}
            ]
            outcomes = []
            for event in events:
                clock[0] += 1
                response = client.post("/events", json={"room_id": "demo-room", **event}, headers=headers)
                response.raise_for_status()
                saved = response.json()["event"]
                outcomes.append({"event_id": saved["event_id"], "route": saved["route"], "priority": saved["priority"], "signals": saved["signals"]})
            before = client.get("/metrics", headers=headers).json()
            queue_before = [x["event_id"] for x in client.get("/review-queue", headers=headers).json()["items"]]
            clock[0] += 20
            for event_id, decision, note in [("context", "allow", "Educational quotation; contextual review clears the flag"), ("link", "remove", "Synthetic demo watchlist link")]:
                response = client.post(f"/reviews/{event_id}", json={"decision": decision, "reviewer": "demo-reviewer", "note": note}, headers=headers)
                response.raise_for_status()
            after = client.get("/metrics", headers=headers).json()
            assert (before["total_events"], before["flagged_events"], after["pending_reviews"], after["completed_reviews"]) == (13, 5, 3, 2)
            return {"dataset": "synthetic", "transport": "FastAPI TestClient (in-process)",
                    "outcomes": outcomes, "queue_before_review": queue_before,
                    "metrics_before": before, "metrics_after": after,
                    "context_audit": client.get("/audit/context", headers=headers).json()["items"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT/"build/demo_report.json")
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["metrics_after"], indent=2))
    print(f"Verified demo report: {args.output}")


if __name__ == "__main__":
    main()
