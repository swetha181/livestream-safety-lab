import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi.testclient import TestClient
from safety_lab.api import create_app
from safety_lab.rules import RuleEngine, fingerprint
from safety_lab.store import Store, Conflict, Missing


def event(identifier="e1", text="Great stream today", user="viewer1", room="room1"):
    return {"event_id": identifier, "text": text, "user_id": user, "room_id": room}


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.engine = RuleEngine()

    def test_benign_content_allowed(self):
        self.assertEqual(self.engine.evaluate("Great stream!")["route"], "allow")

    def test_watchlist_domain_and_subdomain(self):
        for url in ["https://rewards.example/claim", "https://promo.rewards.example/path"]:
            with self.subTest(url=url):
                self.assertEqual(self.engine.evaluate(url)["priority"], 90)

    def test_domain_suffix_does_not_match_unrelated_host(self):
        for url in ["https://rewards.example.safe.example", "https://notrewards.example", "https://safe.example/rewards.example"]:
            self.assertEqual(self.engine.evaluate(url)["route"], "allow")

    def test_malformed_url_does_not_crash(self):
        self.assertEqual(self.engine.evaluate("Look https://[broken")["route"], "allow")

    def test_case_and_invisible_character_normalisation(self):
        self.assertEqual(self.engine.evaluate("YOU are worth\u200bless")["priority"], 80)

    def test_phrase_does_not_match_inside_longer_word(self):
        self.assertEqual(self.engine.evaluate("you are worthlessish")["route"], "allow")

    def test_quoted_phrase_still_requires_contextual_review(self):
        self.assertEqual(self.engine.evaluate('Please report anyone saying "you are worthless"')["route"], "review")

    def test_repetition_threshold_includes_current_event(self):
        self.assertEqual(self.engine.evaluate("hello", prior_repeats=1)["route"], "allow")
        self.assertEqual(self.engine.evaluate("hello", prior_repeats=2)["priority"], 60)

    def test_burst_threshold_boundary(self):
        self.assertEqual(self.engine.evaluate("hello", prior_count=4)["route"], "allow")
        self.assertEqual(self.engine.evaluate("hello", prior_count=5)["priority"], 40)

    def test_multiple_signals_keep_highest_priority(self):
        result = self.engine.evaluate("you are worthless https://rewards.example", 5, 2)
        self.assertEqual(len(result["signals"]), 4)
        self.assertEqual(result["priority"], 90)

    def test_fingerprints_normalise_whitespace_and_case(self):
        self.assertEqual(fingerprint("Hello   WORLD"), fingerprint("hello world"))
        self.assertNotEqual(fingerprint("hello world"), fingerprint("hello elsewhere"))

    def test_rule_version_is_reproducible(self):
        self.assertEqual(self.engine.version, RuleEngine().version)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.db"
        self.now = 1000.0
        self.store = Store(self.path, clock=lambda: self.now)

    def test_identical_retry_has_no_duplicate_audit_or_window_count(self):
        self.store.ingest(event())
        saved, replayed = self.store.ingest(event())
        self.assertTrue(replayed)
        self.assertEqual(saved["features"]["messages_in_window"], 1)
        self.assertEqual(len(self.store.audit("e1")), 1)
        self.assertEqual(self.store.metrics()["total_events"], 1)

    def test_conflicting_event_id_rejected(self):
        self.store.ingest(event())
        with self.assertRaises(Conflict):
            self.store.ingest(event(text="Different content"))
        self.assertEqual(self.store.metrics()["total_events"], 1)

    def test_repetition_is_scoped_to_user_and_room(self):
        self.store.ingest(event("a"))
        self.store.ingest(event("b"))
        self.assertEqual(self.store.ingest(event("c", room="another"))[0]["route"], "allow")
        self.assertEqual(self.store.ingest(event("d", user="another"))[0]["route"], "allow")
        self.assertEqual(self.store.ingest(event("e"))[0]["priority"], 60)

    def test_rolling_window_excludes_exact_lower_boundary(self):
        self.store.ingest(event("a"))
        self.store.ingest(event("b"))
        self.now += 60
        result, _ = self.store.ingest(event("c"))
        self.assertEqual(result["features"]["messages_in_window"], 1)

    def test_history_survives_store_restart(self):
        self.store.ingest(event("a"))
        self.store.ingest(event("b"))
        restarted = Store(self.path, clock=lambda: self.now)
        self.assertEqual(restarted.ingest(event("c"))[0]["priority"], 60)

    def test_queue_priority_then_fifo(self):
        self.store.ingest(event("harass", "you are worthless"))
        self.now += 1
        self.store.ingest(event("link1", "https://rewards.example"))
        self.now += 1
        self.store.ingest(event("link2", "https://rewards.example", user="another"))
        self.assertEqual([x["event_id"] for x in self.store.queue()], ["link1", "link2", "harass"])
        self.assertEqual(self.store.queue(1, 1)[0]["event_id"], "link2")

    def test_human_review_is_logged_and_removed_from_queue(self):
        self.store.ingest(event(text="you are worthless"))
        self.now += 12
        result = self.store.review("e1", "allow", "reviewer1", "Quoted educational example")
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(self.store.queue(), [])
        self.assertEqual(self.store.audit("e1")[-1]["detail"]["decision"], "allow")
        self.assertEqual(self.store.metrics()["average_time_to_decision_seconds"], 12)

    def test_second_review_cannot_overwrite_first(self):
        self.store.ingest(event(text="you are worthless"))
        self.store.review("e1", "allow", "reviewer1")
        with self.assertRaises(Conflict):
            self.store.review("e1", "remove", "reviewer2")
        self.assertEqual(self.store.get("e1")["decision"], "allow")

    def test_unflagged_event_is_not_reviewable(self):
        self.store.ingest(event())
        with self.assertRaises(Conflict):
            self.store.review("e1", "remove", "reviewer1")

    def test_unknown_events_raise_missing(self):
        for operation in [lambda: self.store.get("missing"), lambda: self.store.audit("missing"), lambda: self.store.review("missing", "allow", "r1")]:
            with self.assertRaises(Missing):
                operation()

    def test_empty_metrics_are_explicit(self):
        result = self.store.metrics()
        self.assertEqual(result["total_events"], 0)
        self.assertIsNone(result["average_time_to_decision_seconds"])

    def test_concurrent_duplicate_events_insert_exactly_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.ingest(event()), range(24)))
        self.assertEqual(sum(not replayed for _, replayed in results), 1)
        self.assertEqual(self.store.metrics()["total_events"], 1)
        self.assertEqual(len(self.store.audit("e1")), 1)

    def test_concurrent_unique_events_have_atomic_window_counts(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.store.ingest(event(f"e{i}", f"message number {i}"))[0], range(24)))
        self.assertEqual(sorted(r["features"]["messages_in_window"] for r in results), list(range(1, 25)))
        self.assertEqual(self.store.metrics()["flagged_events"], 19)

    def test_concurrent_reviewers_cannot_double_resolve(self):
        self.store.ingest(event(text="https://rewards.example"))
        def decide(i):
            try:
                self.store.review("e1", "remove", f"reviewer{i}")
                return "saved"
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(decide, range(4)))
        self.assertEqual(results.count("saved"), 1)
        self.assertEqual(len(self.store.audit("e1")), 2)


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        app = create_app(Path(self.temp.name)/"api.db", api_key="test-key")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"X-API-Key": "test-key"}

    def test_health_is_accessible(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_protected_endpoints_reject_missing_and_wrong_keys(self):
        for key in [None, "wrong"]:
            response = self.client.get("/metrics", headers={} if key is None else {"X-API-Key": key})
            self.assertEqual(response.status_code, 401)

    def test_api_create_retry_and_conflict(self):
        self.assertEqual(self.client.post("/events", json=event(), headers=self.headers).status_code, 201)
        self.assertEqual(self.client.post("/events", json=event(), headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/events", json=event(text="changed"), headers=self.headers).status_code, 409)

    def test_bad_input_rejected(self):
        for changes in [{"text": " "}, {"text": "x"*2001}, {"text": 123}, {"room_id": ""}, {"user_id": "a/b"}, {"extra": "field"}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post("/events", json={**event(), **changes}, headers=self.headers).status_code, 422)

    def test_review_api_and_audit(self):
        self.client.post("/events", json=event(text="https://rewards.example"), headers=self.headers)
        self.assertEqual(len(self.client.get("/review-queue", headers=self.headers).json()["items"]), 1)
        result = self.client.post("/reviews/e1", json={"decision": "remove", "reviewer": "r1"}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.get("/metrics", headers=self.headers).json()["human_removed"], 1)
        self.assertEqual(len(self.client.get("/audit/e1", headers=self.headers).json()["items"]), 2)

    def test_invalid_decision_and_bad_pagination(self):
        self.assertEqual(self.client.post("/reviews/e1", json={"decision": "ban_forever", "reviewer": "r1"}, headers=self.headers).status_code, 422)
        for query in ["limit=101", "offset=-1"]:
            self.assertEqual(self.client.get("/review-queue?"+query, headers=self.headers).status_code, 422)

    def test_unknown_event_is_404(self):
        self.assertEqual(self.client.get("/events/missing", headers=self.headers).status_code, 404)

    def test_rules_are_versioned(self):
        self.assertIn("demo-1.0:", self.client.get("/rules", headers=self.headers).json()["version"])


if __name__ == "__main__":
    unittest.main()
