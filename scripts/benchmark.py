"""Measure concurrent store ingestion locally, not network or production throughput."""
import argparse
import json
import math
import platform
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from safety_lab.store import Store

ROOT = Path(__file__).resolve().parents[1]


def run(count=500, workers=8):
    if count < 1 or workers < 1:
        raise ValueError("events and workers must be positive")
    with tempfile.TemporaryDirectory() as temp:
        store = Store(Path(temp)/"benchmark.db")
        def submit(i):
            start = time.perf_counter()
            # One user per event isolates write contention from burst policy.
            result, replayed = store.ingest({"event_id": f"bench{i}", "room_id": "bench", "user_id": f"viewer{i}",
                                           "text": "https://rewards.example" if i % 10 == 0 else "Enjoying the stream"})
            assert not replayed
            return (time.perf_counter()-start)*1000
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            durations = sorted(pool.map(submit, range(count)))
        elapsed = time.perf_counter()-start
        metrics = store.metrics()
        assert metrics["total_events"] == count
        assert metrics["flagged_events"] == (count+9)//10
        return {"scope": "local SQLite service-layer ingestion; excludes HTTP/network and server scheduling",
                "platform": platform.platform(), "python": platform.python_version(),
                "events": count, "workers": workers, "errors": 0,
                "elapsed_seconds": round(elapsed, 4), "events_per_second": round(count/elapsed, 2),
                "p50_ms": round(durations[math.ceil(.5*count)-1], 3),
                "p95_ms": round(durations[math.ceil(.95*count)-1], 3),
                "metrics": metrics,
                "caveat": "One local sample. SQLite serializes writes. This does not establish high-concurrency production capacity."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=500)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default=ROOT/"build/benchmark.json")
    args = parser.parse_args()
    result = run(args.events, args.workers)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
