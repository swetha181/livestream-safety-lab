"""Transactional moderation state with per-operation SQLite connections."""
import json
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from .rules import RuleEngine, fingerprint


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


class Store:
    def __init__(self, path, engine=None, clock=time.time):
        self.path = str(path)
        self.engine = engine or RuleEngine()
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    text TEXT NOT NULL, fingerprint TEXT NOT NULL, ingested_at REAL NOT NULL,
                    route TEXT NOT NULL CHECK(route IN ('allow','review')),
                    priority INTEGER NOT NULL, signals_json TEXT NOT NULL,
                    features_json TEXT NOT NULL, rule_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('not_queued','pending','resolved')),
                    decision TEXT, reviewer TEXT, reviewed_at REAL, note TEXT
                );
                CREATE INDEX IF NOT EXISTS recent_user_messages ON events(room_id,user_id,ingested_at);
                CREATE INDEX IF NOT EXISTS pending_priority ON events(status,priority DESC,ingested_at,event_id);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL REFERENCES events(event_id),
                    action TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL, detail_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_audit ON audit(event_id,id);
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def unpack(row):
        result = dict(row)
        result.pop("fingerprint", None)
        result["signals"] = json.loads(result.pop("signals_json"))
        result["features"] = json.loads(result.pop("features_json"))
        return result

    def ingest(self, event):
        """Check idempotency, extract history, classify and log in one write transaction."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute("SELECT * FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
                if existing:
                    if any(existing[k] != event[k] for k in ("room_id", "user_id", "text")):
                        raise Conflict("event_id already exists with a different payload")
                    conn.commit()
                    return self.unpack(existing), True
                now = self.clock()
                hashed = fingerprint(event["text"])
                history = conn.execute("""
                    SELECT COUNT(*) AS n, COALESCE(SUM(fingerprint=?),0) AS repeats
                    FROM events WHERE room_id=? AND user_id=? AND ingested_at>? AND ingested_at<=?
                """, (hashed, event["room_id"], event["user_id"], now-self.engine.config["window_seconds"], now)).fetchone()
                result = self.engine.evaluate(event["text"], history["n"], history["repeats"])
                status = "pending" if result["route"] == "review" else "not_queued"
                conn.execute("""INSERT INTO events
                    (event_id,room_id,user_id,text,fingerprint,ingested_at,route,priority,signals_json,features_json,rule_version,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event["event_id"], event["room_id"], event["user_id"], event["text"], hashed, now,
                     result["route"], result["priority"], json.dumps(result["signals"]), json.dumps(result["features"]), result["rule_version"], status))
                conn.execute("INSERT INTO audit(event_id,action,actor,at,detail_json) VALUES (?,?,?,?,?)",
                             (event["event_id"], "classified", "rule_engine", now, json.dumps(result)))
                saved = conn.execute("SELECT * FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
                conn.commit()
                return self.unpack(saved), False
            except Exception:
                conn.rollback()
                raise

    def get(self, event_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise Missing("event not found")
            return self.unpack(row)

    def queue(self, limit=20, offset=0):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM events WHERE status='pending' ORDER BY priority DESC,ingested_at,event_id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return [self.unpack(r) for r in rows]

    def review(self, event_id, decision, reviewer, note=""):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                event = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
                if event is None:
                    raise Missing("event not found")
                if event["status"] != "pending":
                    raise Conflict("only pending events can receive a review decision")
                now = self.clock()
                conn.execute("UPDATE events SET status='resolved',decision=?,reviewer=?,reviewed_at=?,note=? WHERE event_id=? AND status='pending'", (decision, reviewer, now, note, event_id))
                conn.execute("INSERT INTO audit(event_id,action,actor,at,detail_json) VALUES (?,?,?,?,?)",
                             (event_id, "human_review", reviewer, now, json.dumps({"decision": decision, "note": note})))
                row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
                conn.commit()
                return self.unpack(row)
            except Exception:
                conn.rollback()
                raise

    def audit(self, event_id):
        self.get(event_id)
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM audit WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["detail"] = json.loads(item.pop("detail_json"))
                result.append(item)
            return result

    def metrics(self):
        with self.connection() as conn:
            # Use one read transaction so totals and rule counts describe one snapshot.
            conn.execute("BEGIN")
            row = conn.execute("""SELECT COUNT(*) AS total_events,
                COALESCE(SUM(route='review'),0) AS flagged_events,
                COALESCE(SUM(status='pending'),0) AS pending_reviews,
                COALESCE(SUM(status='resolved'),0) AS completed_reviews,
                COALESCE(SUM(decision='allow'),0) AS human_allowed,
                COALESCE(SUM(decision='remove'),0) AS human_removed,
                AVG(reviewed_at-ingested_at) AS average_time_to_decision_seconds FROM events""").fetchone()
            counts = Counter(s["rule_id"] for r in conn.execute("SELECT signals_json FROM events WHERE route='review'") for s in json.loads(r[0]))
            conn.commit()
            return {**dict(row), "signals_by_rule": dict(sorted(counts.items()))}
