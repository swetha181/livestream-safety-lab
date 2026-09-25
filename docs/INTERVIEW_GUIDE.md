# Understand the project before submitting it

This project was prepared with AI assistance. Run it, inspect the implementation, make a change yourself and be ready to explain the trade-offs. Do not describe it as production work, imply it was used at Rently/TikTok, or claim you independently authored code you have not understood.

## A 60-second explanation to adapt after reviewing the code

“LiveStream Safety Lab is a local Python/FastAPI prototype for routing live-chat comments into human review. It extracts simple signals for suspicious links, configured phrases, repeated messages and bursts within a rolling window. Flagged events enter a persistent priority queue. A reviewer can allow or remove an item, and both the rule result and review decision are recorded in an audit history. I focused on retry safety and consistent state: duplicate event IDs do not increase counts, and simultaneous reviewers cannot overwrite each other. The tests include concurrency and boundary cases. It uses synthetic text and SQLite, so it is not a high-scale moderation service or a semantic safety classifier.”

## Walk through the code in this order

1. `config/rules.json`: thresholds, priorities and phrases. Explain why priority is not confidence.
2. `safety_lab/rules.py`: normalisation, n-grams, URL parsing, message fingerprints and multiple signals.
3. `safety_lab/store.py`: history query, `BEGIN IMMEDIATE`, event/audit writes, queue ordering and review conflict.
4. `safety_lab/api.py`: request models, API-key dependency, endpoints and error mapping.
5. `tests/test_safety_lab.py`: show one boundary test and one concurrency test.
6. `scripts/demo.py`: compare the flagged quotation with its later human-allow decision.

## Likely questions

**Why not automatically remove every phrase match?** A quotation or educational warning can contain the phrase without abusing anyone. Rules supply evidence for contextual review, not reliable intent detection.

**Why a database queue rather than a heap?** Persistence and review status already live in SQLite; an ordered index gives a simple durable queue. A heap would need separate persistence and coordination. Offset pagination is simple but not ideal at large offsets.

**How do you handle retries?** The primary-key event ID and transaction make insertion idempotent for the same payload. Reuse with a different payload returns 409. This is local transaction-level behaviour, not an end-to-end “exactly once” claim across external services.

**What happens if two comments arrive together?** The write transaction serialises the history calculation and insertion, so the second writer sees the first committed event. This improves consistency but limits throughput.

**What happens if two people review the same item?** Both may see it in the queue, but only one can resolve its pending state. The other receives 409. A reviewer assignment/lease feature would prevent duplicate effort; it is not implemented.

**How would you scale it?** First measure HTTP latency, contention and queue lag. Consider PostgreSQL, background consumers and partitioning by room/user while preserving ordering. Add retry/deduplication strategy, back-pressure, observability and load tests. Do not claim these are implemented.

**How would you evaluate quality?** Create a labelled dataset with positive, benign and ambiguous examples. Measure precision/recall by category, false positives and reviewer disagreement. A passing software test suite does not measure real-world moderation effectiveness.

**What does the latency metric mean?** Time from ingestion to human decision, including queue wait. It is not active handling time. The deterministic demo uses a simulated clock; the benchmark separately measures local store-call timing.

**What are the main security gaps?** The shared demo key does not provide role-based access or verified reviewer identities. There is no rate limiting, retention or tamper-proof audit. Keep the app on localhost and use only synthetic records.

## Three small exercises before an interview

1. Change `repetition_threshold` from 3 to 4 and add/update a boundary test. Run the suite, observe the demo expectation change, then restore the published default or update its report and documentation consistently.
2. Add a benign quotation test and explain why it still flags. Discuss what evidence a human needs to clear it.
3. Repeat the benchmark with 1, 4 and 8 workers. Compare results without claiming that more threads always improve throughput. Explain SQLite's single-writer constraint.

## Job-description mapping

| TikTok requirement | Concrete evidence here | Remaining gap |
| --- | --- | --- |
| Python; data structures and algorithms | Sets, tuples, hashing, rolling-window queries and indexed priority ordering | Interview-style algorithm practice still needed |
| Safety process/rule systems | Configurable rules, stored versions and reason codes | No real platform policy integration |
| Human review and risk insight | Queue, allow/remove decisions, audit and metrics | No production reviewers or measured efficiency gain |
| Stable concurrent systems | Atomic ingestion/review and concurrent tests | No distributed or high-concurrency production validation |
| Requirements and root-cause reasoning | Explicit scope, boundary cases, error semantics and trade-offs | Be ready to explain choices independently |

Use your real Rently examples for teamwork, communication and delivery ownership. The prototype adds domain relevance; it does not replace your professional experience.
