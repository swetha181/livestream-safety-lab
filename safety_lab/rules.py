"""Explainable text rules. Priority is a queue rank, not a probability of harm."""
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def normalise(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff")))
    return " ".join(text.split())


def fingerprint(text):
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


class RuleEngine:
    def __init__(self, config_path=None):
        self.config = json.loads(Path(config_path or ROOT / "config/rules.json").read_text(encoding="utf-8"))
        for key in ("window_seconds", "max_messages_per_window", "repetition_threshold"):
            if type(self.config[key]) is not int or self.config[key] < 1:
                raise ValueError(f"{key} must be a positive integer")
        for key in ("suspicious_link", "harassment_phrase", "repeated_message", "burst_activity"):
            value = self.config["priorities"][key]
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError("Each rule priority must be an integer between 1 and 100")
        self.domains = frozenset(d.lower().rstrip(".") for d in self.config["blocked_domains"])
        self.phrases = [tuple(re.findall(r"\w+", normalise(p))) for p in self.config["review_phrases"]]
        if any(not p for p in self.phrases):
            raise ValueError("Empty review phrase is not allowed")
        canonical = json.dumps(self.config, sort_keys=True).encode()
        self.version = self.config["version"] + ":" + hashlib.sha256(canonical).hexdigest()[:10]

    def evaluate(self, text, prior_count=0, prior_repeats=0):
        normal = normalise(text)
        tokens = tuple(re.findall(r"\w+", normal))
        signals = []

        def add(rule_id, reason):
            signals.append({"rule_id": rule_id, "reason": reason,
                            "priority": self.config["priorities"][rule_id]})

        hosts = set()
        for raw in URL_PATTERN.findall(normal):
            try:
                host = urlsplit(raw.rstrip(".,!?;")).hostname
                if host:
                    hosts.add(host.rstrip("."))
            except ValueError:
                # Malformed URLs are not sufficient evidence for a harmful-content decision.
                continue
        if any(h == d or h.endswith("." + d) for h in hosts for d in self.domains):
            add("suspicious_link", "Link points to a domain in the demo watchlist")

        # Token n-grams avoid matching a phrase inside a longer word.
        ngrams = {n: {tokens[i:i+n] for i in range(len(tokens)-n+1)} for n in {len(p) for p in self.phrases}}
        if any(phrase in ngrams[len(phrase)] for phrase in self.phrases):
            add("harassment_phrase", "A configured phrase requires a contextual human review")
        if prior_repeats + 1 >= self.config["repetition_threshold"]:
            add("repeated_message", "Repeated normalised message in this user's room-specific time window")
        if prior_count + 1 > self.config["max_messages_per_window"]:
            add("burst_activity", "User exceeded the demo message threshold in this room")

        return {"route": "review" if signals else "allow",
                "priority": max((s["priority"] for s in signals), default=0),
                "signals": signals, "rule_version": self.version,
                "features": {"messages_in_window": prior_count + 1, "repeats_in_window": prior_repeats + 1}}
