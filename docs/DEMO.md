# Two-minute LiveStream walkthrough

1. Run `python -m scripts.demo` and show the generated report.
2. Explain: 13 synthetic comments, 5 flags, 2 human decisions, 3 pending reviews.
3. Open the local API documentation following the README.
4. Submit the sample suspicious-link event, retry it unchanged, then change its text while keeping the ID. Explain 201, 200 and 409 responses.
5. Resolve the item and show its audit history.
6. Run `python -m scripts.smoke_http` for a real HTTP verification.

This is a deterministic rule engine, not a harm-probability model. The demo timer is simulated and is not a reviewer-productivity result.
