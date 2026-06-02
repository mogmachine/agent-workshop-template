#!/usr/bin/env python3
"""
learning.py — Poppy's self-learning system.

Port of Jack's learning.py (Hippius/Dubs team, 2026-04) adapted for our stack:
- Local Ollama (nomic-embed-text, 768d) — same as Jack, privacy-first
- Qdrant on localhost:6333
- 4 collections: experiences, corrections, preferences, principles

CORE PATTERN — RAG before action:
Before any sensitive/external action run:
    python3 scripts/learning.py check "proposed action description"
Returns top hits from all 4 collections. Adapt your plan accordingly.

After a mistake, a success worth remembering, or a mog correction, log it:
    python3 scripts/learning.py correction <agent> --proposed "X" --corrected "Y" --reason "Z"
    python3 scripts/learning.py experience <agent> --problem "..." --approach "..." --outcome success --lesson "..."
    python3 scripts/learning.py preference <agent> "mog prefers X over Y"

Weekly reflection (cron, Sunday 8pm UTC):
    python3 scripts/learning.py reflect

Stats:
    python3 scripts/learning.py stats

Design choices lifted from Jack's writeup:
- Manual logging, NOT automatic. Humans can't extract signal from
  "I ran ls and it worked". Manual forces intentionality.
- RAG before action, NOT system prompt injection.
  Pull > push. Saves tokens, avoids stale-context pollution.
- No dedup, no TTL, no ACLs. Trust circle on internal box. Add later if needed.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Optional

QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = 768

COLLECTIONS = ["experiences", "corrections", "preferences", "principles"]


# ---------- low-level HTTP ----------

def _request(url, data=None, api_key=None, method=None, timeout=30):
    headers = {"Content-Type": "application/json"} if data else {}
    if api_key:
        headers["api-key"] = api_key
    body = json.dumps(data).encode() if data is not None else None
    m = method or ("POST" if data is not None else "GET")
    req = urllib.request.Request(url, body, headers, method=m)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"{m} {url} → {e.code} {body[:300]}") from e


def _embed(text: str):
    if not text or not text.strip():
        raise ValueError("empty text for embedding")
    resp = _request(f"{OLLAMA_URL}/api/embed", {"model": EMBED_MODEL, "input": text})
    embeds = resp.get("embeddings") or []
    if not embeds:
        raise RuntimeError(f"no embedding returned from {EMBED_MODEL}")
    return embeds[0]


def _text_id(text: str) -> int:
    # Qdrant accepts uint64; take first 15 hex chars of sha256 for safety
    return int(hashlib.sha256(text.encode()).hexdigest()[:15], 16)


# ---------- collection lifecycle ----------

def _collection_exists(name: str) -> bool:
    try:
        _request(f"{QDRANT_URL}/collections/{name}", api_key=QDRANT_API_KEY)
        return True
    except RuntimeError:
        return False


def _ensure_collection(name: str):
    if _collection_exists(name):
        return
    body = {"vectors": {"size": EMBED_DIM, "distance": "Cosine"}}
    _request(f"{QDRANT_URL}/collections/{name}", body, QDRANT_API_KEY, method="PUT")
    print(f"✓ created collection: {name}")


def _ensure_all():
    for c in COLLECTIONS:
        _ensure_collection(c)


# ---------- store / search ----------

def _store(collection: str, text: str, payload: dict):
    _ensure_collection(collection)
    vector = _embed(text)
    payload.setdefault("timestamp", int(time.time()))
    pid = _text_id(text + str(payload["timestamp"]))
    resp = _request(
        f"{QDRANT_URL}/collections/{collection}/points",
        {"points": [{"id": pid, "vector": vector, "payload": payload}]},
        QDRANT_API_KEY, method="PUT"
    )
    return resp


def _search(collection: str, query: str, limit: int = 5, score_threshold: float = 0.3, filters=None):
    if not _collection_exists(collection):
        return []
    vector = _embed(query)
    body = {"vector": vector, "limit": limit, "with_payload": True,
            "score_threshold": score_threshold}
    if filters:
        body["filter"] = filters
    resp = _request(f"{QDRANT_URL}/collections/{collection}/points/search",
                    body, QDRANT_API_KEY)
    return resp.get("result", [])


def _count(collection: str) -> int:
    if not _collection_exists(collection):
        return 0
    resp = _request(f"{QDRANT_URL}/collections/{collection}", api_key=QDRANT_API_KEY)
    return resp["result"].get("points_count", 0)


# ---------- LearningAgent facade ----------

class LearningAgent:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id

    def log_experience(self, problem: str, approach: str, outcome: str,
                       lesson: str, tags=None, context: str = ""):
        text = (f"PROBLEM: {problem}\nAPPROACH: {approach}\n"
                f"OUTCOME: {outcome}\nLESSON: {lesson}")
        payload = {
            "text": text, "agent": self.agent_id, "type": "experience",
            "problem": problem, "approach": approach, "outcome": outcome,
            "lesson": lesson, "tags": tags or [], "context": context,
        }
        return _store("experiences", text, payload)

    def log_correction(self, proposed: str, corrected: str,
                       reason: str = "", domain: str = ""):
        text = f"PROPOSED: {proposed}\nCORRECTED: {corrected}\nREASON: {reason}"
        payload = {
            "text": text, "agent": self.agent_id, "type": "correction",
            "proposed": proposed, "corrected": corrected,
            "reason": reason, "domain": domain,
        }
        return _store("corrections", text, payload)

    def log_preference(self, preference: str, domain: str = ""):
        payload = {
            "text": preference, "agent": self.agent_id,
            "type": "preference", "domain": domain,
        }
        return _store("preferences", preference, payload)

    def pre_action_check(self, query: str, limit: int = 3,
                         agent: str = None, domain: str = None) -> dict:
        """
        Composite-ranked retrieval across all collections.

        Routes through scripts/recall.py (similarity + recency + frequency +
        specificity), falling back to pure similarity if recall.py is
        unavailable. The returned shape matches the legacy version so
        downstream printing keeps working.
        """
        # Use the agent passed in, or fall back to the agent_id this
        # LearningAgent was constructed with.
        eff_agent = agent or self.agent_id
        try:
            import sys as _sys, os as _os
            _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
            import recall as _recall  # type: ignore
            results = {}
            for col in COLLECTIONS:
                hits = _recall.recall(
                    collection=col, query=query, agent=eff_agent,
                    domain=domain, limit=limit, score_threshold=0.35,
                )
                results[col] = [{
                    "score": h.score,
                    "text": (h.payload.get("text") or "")[:400],
                    "lesson": h.payload.get("lesson", ""),
                    "outcome": h.payload.get("outcome", ""),
                    "domain": h.payload.get("domain", ""),
                    "agent": h.payload.get("agent", ""),
                    "similarity": h.similarity,
                    "age_days": h.age_days,
                    "duplicate_count": h.duplicate_count,
                } for h in hits]
            return results
        except Exception as e:
            # Fallback: legacy pure-similarity behaviour
            sys.stderr.write(f"[learning.check fallback to pure-sim: {e}]\n")
            results = {}
            for col in COLLECTIONS:
                hits = _search(col, query, limit=limit, score_threshold=0.35)
                results[col] = [{
                    "score": h["score"],
                    "text": h["payload"].get("text", "")[:400],
                    "lesson": h["payload"].get("lesson", ""),
                    "outcome": h["payload"].get("outcome", ""),
                    "domain": h["payload"].get("domain", ""),
                    "agent": h["payload"].get("agent", ""),
                } for h in hits]
            return results

    def reflect(self) -> dict:
        """Weekly reflection: scan experiences + corrections, extract top lessons,
        store a summary point in 'principles'."""
        all_exp = _search("experiences",
                          "lessons learned from problems and solutions",
                          limit=50, score_threshold=0.0)
        all_corr = _search("corrections",
                           "corrections and adjustments made",
                           limit=50, score_threshold=0.0)
        successes = [h for h in all_exp
                     if h["payload"].get("outcome") == "success"]
        failures = [h for h in all_exp
                    if h["payload"].get("outcome") == "failure"]
        lessons = [h["payload"].get("lesson", "") for h in successes
                   if h["payload"].get("lesson")]
        correction_reasons = [h["payload"].get("reason", "") for h in all_corr
                              if h["payload"].get("reason")]
        report = {
            "date": time.strftime("%Y-%m-%d"),
            "total_experiences": len(all_exp),
            "successes": len(successes),
            "failures": len(failures),
            "total_corrections": len(all_corr),
            "top_lessons": lessons[:10],
            "top_correction_reasons": correction_reasons[:10],
        }
        if lessons or correction_reasons:
            summary = (f"REFLECTION {report['date']}: {len(all_exp)} experiences "
                       f"({len(successes)} success, {len(failures)} fail), "
                       f"{len(all_corr)} corrections.")
            if lessons:
                summary += f" Top lessons: {'; '.join(lessons[:5])}"
            if correction_reasons:
                summary += f" Top correction patterns: {'; '.join(correction_reasons[:5])}"
            payload = {
                "text": summary, "agent": "system", "type": "principle",
                "principle": summary,
                "evidence_count": len(all_exp) + len(all_corr),
                "confidence": min(0.5 + len(all_exp) * 0.05, 0.95),
                "domain": "general",
            }
            _store("principles", summary, payload)
        return report


# ---------- pretty-printing ----------

def _print_check_results(results: dict, query: str):
    any_hit = any(results[c] for c in results)
    if not any_hit:
        print(f"🔍 No relevant context for: {query!r}")
        return
    print(f"🔍 Pre-action check: {query!r} (composite ranking)\n")
    for col in COLLECTIONS:
        hits = results.get(col, [])
        if not hits:
            continue
        print(f"[{col}] {len(hits)} hit(s)")
        for h in hits:
            snippet = h["text"][:200].replace("\n", " / ")
            extra = []
            if h.get("lesson"):
                extra.append(f"lesson={h['lesson'][:80]}")
            if h.get("outcome"):
                extra.append(f"outcome={h['outcome']}")
            if h.get("domain"):
                extra.append(f"domain={h['domain']}")
            if h.get("similarity") is not None:
                extra.append(f"sim={h['similarity']:.2f}")
            if h.get("age_days") is not None and h["age_days"] >= 0:
                extra.append(f"age={h['age_days']:.1f}d")
            if h.get("duplicate_count"):
                extra.append(f"dups={h['duplicate_count']}")
            tail = (" | " + " | ".join(extra)) if extra else ""
            print(f"  [{h['score']:.3f}] {snippet}{tail}")
        print()


# ---------- CLI ----------

def _parse_flags(argv):
    out = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--") and i + 1 < len(argv):
            out[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


def _usage():
    print("Usage:")
    print("  learning.py check '<proposed action / query>' [--limit N] [--agent X] [--domain Y]")
    print("  learning.py experience <agent> --problem '..' --approach '..' "
          "--outcome success|failure --lesson '..' [--tags a,b,c] [--context ..]")
    print("  learning.py correction <agent> --proposed '..' --corrected '..' "
          "[--reason '..'] [--domain ..]")
    print("  learning.py preference <agent> 'text' [--domain ..]")
    print("  learning.py reflect")
    print("  learning.py stats")
    print("  learning.py init   # create all 4 collections if missing")


def main():
    if len(sys.argv) < 2:
        _usage()
        sys.exit(1)
    cmd = sys.argv[1]

    if cmd == "init":
        _ensure_all()
        print("✓ all collections ready")
        return

    if cmd == "stats":
        print("📊 Learning stats:")
        for c in COLLECTIONS:
            print(f"  {c:15s} {_count(c):>5d} points")
        return

    if cmd == "reflect":
        a = LearningAgent("system")
        report = a.reflect()
        print(json.dumps(report, indent=2))
        return

    if cmd == "check":
        if len(sys.argv) < 3:
            _usage(); sys.exit(1)
        query = sys.argv[2]
        flags = _parse_flags(sys.argv[3:])
        limit = int(flags.get("limit", 3))
        agent = flags.get("agent", "orchestrator")
        domain = flags.get("domain")
        a = LearningAgent(agent)
        _print_check_results(
            a.pre_action_check(query, limit=limit, agent=agent, domain=domain),
            query,
        )
        return

    if cmd == "experience":
        if len(sys.argv) < 3:
            _usage(); sys.exit(1)
        agent = sys.argv[2]
        flags = _parse_flags(sys.argv[3:])
        required = ["problem", "approach", "outcome", "lesson"]
        missing = [k for k in required if not flags.get(k)]
        if missing:
            print(f"ERROR: missing required flags: {missing}")
            sys.exit(1)
        a = LearningAgent(agent)
        a.log_experience(
            problem=flags["problem"], approach=flags["approach"],
            outcome=flags["outcome"], lesson=flags["lesson"],
            tags=[t for t in flags.get("tags", "").split(",") if t],
            context=flags.get("context", ""),
        )
        print("✓ experience logged")
        return

    if cmd == "correction":
        if len(sys.argv) < 3:
            _usage(); sys.exit(1)
        agent = sys.argv[2]
        flags = _parse_flags(sys.argv[3:])
        required = ["proposed", "corrected"]
        missing = [k for k in required if not flags.get(k)]
        if missing:
            print(f"ERROR: missing required flags: {missing}")
            sys.exit(1)
        a = LearningAgent(agent)
        a.log_correction(
            proposed=flags["proposed"], corrected=flags["corrected"],
            reason=flags.get("reason", ""), domain=flags.get("domain", ""),
        )
        print("✓ correction logged")
        return

    if cmd == "preference":
        if len(sys.argv) < 4:
            _usage(); sys.exit(1)
        agent = sys.argv[2]
        text = sys.argv[3]
        flags = _parse_flags(sys.argv[4:])
        a = LearningAgent(agent)
        a.log_preference(text, domain=flags.get("domain", ""))
        print("✓ preference logged")
        return

    _usage()
    sys.exit(1)


if __name__ == "__main__":
    main()
