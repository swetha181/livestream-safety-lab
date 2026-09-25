"""End-to-end smoke test against an isolated local Uvicorn process."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import httpx

PROJECT = Path(__file__).resolve().parents[1]
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
with tempfile.TemporaryDirectory() as temp:
    env = {**os.environ, "SAFETY_DB": str(Path(temp)/"http.db"), "SAFETY_API_KEY": "smoke-test-key"}
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "safety_lab.api:create_app", "--factory", "--host", "127.0.0.1", "--port", str(port)], cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3, trust_env=False) as client:
            for _ in range(40):
                if process.poll() is not None:
                    raise RuntimeError(process.stdout.read())
                try:
                    health = client.get("/health")
                    if health.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError("Local API startup timed out")
            assert "Swagger UI" in client.get("/docs").text
            assert client.get("/metrics").status_code == 401
            client.headers["X-API-Key"] = "smoke-test-key"
            body = {"event_id": "http1", "room_id": "room1", "user_id": "user1", "text": "https://rewards.example/claim"}
            assert client.post("/events", json=body).status_code == 201
            assert client.post("/events", json=body).status_code == 200
            assert len(client.get("/review-queue").json()["items"]) == 1
            review = {"decision": "remove", "reviewer": "r1", "note": "Smoke test"}
            assert client.post("/reviews/http1", json=review).status_code == 200
            assert client.post("/reviews/http1", json=review).status_code == 409
            assert len(client.get("/audit/http1").json()["items"]) == 2
            assert client.get("/metrics").json()["human_removed"] == 1
        print("HTTP smoke passed: server startup, docs, authentication, ingestion, retry, queue, review conflict, audit and metrics.")
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
