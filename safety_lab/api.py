"""Run locally: uvicorn safety_lab.api:create_app --factory --host 127.0.0.1 --port 8000."""
import hmac
import os
import sqlite3
from pathlib import Path
from typing import Literal
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from .store import Conflict, Missing, Store

ROOT = Path(__file__).resolve().parents[1]


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    room_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    user_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    text: StrictStr = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("text cannot be blank")
        return value


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["allow", "remove"]
    reviewer: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    note: StrictStr = Field(default="", max_length=500)


def create_app(db_path=None, api_key=None, clock=None):
    key = api_key or os.getenv("SAFETY_API_KEY") or "local-demo-key"
    kwargs = {"clock": clock} if clock is not None else {}
    store = Store(db_path or os.getenv("SAFETY_DB") or ROOT / "data/safety.db", **kwargs)
    app = FastAPI(title="LiveStream Safety Lab", version="1.0.0",
                  description="Local portfolio prototype using synthetic live-chat comments. Rule flags require human review; this is not an official TikTok system.")
    app.state.store = store
    header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authenticate(value: str | None = Depends(header)):
        if value is None or not hmac.compare_digest(value.encode(), key.encode()):
            raise HTTPException(401, "Missing or invalid API key")

    secured = [Depends(authenticate)]

    @app.exception_handler(Conflict)
    async def conflict_handler(request: Request, exc: Conflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(Missing)
    async def missing_handler(request: Request, exc: Missing):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(sqlite3.OperationalError)
    async def database_handler(request: Request, exc: sqlite3.OperationalError):
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return JSONResponse(status_code=503, content={"detail": "Database busy; retry the same event_id"}, headers={"Retry-After": "1"})
        return JSONResponse(status_code=500, content={"detail": "Database operation failed"})

    @app.get("/health")
    def health():
        with store.connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "mode": "local-prototype"}

    @app.get("/rules", dependencies=secured)
    def rules():
        return {"version": store.engine.version, "config": store.engine.config}

    @app.post("/events", dependencies=secured)
    def ingest(event: Event):
        saved, replayed = store.ingest(event.model_dump())
        return JSONResponse(status_code=200 if replayed else 201, content={"event": saved, "replayed": replayed})

    @app.get("/events/{event_id}", dependencies=secured)
    def get_event(event_id: str):
        return store.get(event_id)

    @app.get("/review-queue", dependencies=secured)
    def queue(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        return {"items": store.queue(limit, offset), "limit": limit, "offset": offset}

    @app.post("/reviews/{event_id}", dependencies=secured)
    def review(event_id: str, body: Review):
        return store.review(event_id, body.decision, body.reviewer, body.note)

    @app.get("/audit/{event_id}", dependencies=secured)
    def audit(event_id: str):
        return {"items": store.audit(event_id)}

    @app.get("/metrics", dependencies=secured)
    def metrics():
        return store.metrics()

    return app
