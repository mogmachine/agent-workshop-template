#!/usr/bin/env python3
"""
recall.py — composite-ranking memory retrieval for Poppy and the peer agents.

Inspired by Ditto (Bittensor SN118) but built around our stack:
- Local Ollama embeddings (nomic-embed-text, 768d) — privacy-first
- Qdrant on localhost:6333
- Per-agent weight profiles
- Per-domain weight overrides
- Audit log of every retrieval

CORE IDEA
---------
Pure cosine similarity ranks results by "how close is the query vector to the
stored vector." That misses three signals we care about:

  - RECENCY: a correction from yesterday should outrank a contradicting one
    from a week ago. Pure similarity is time-blind.
  - FREQUENCY: a rule reinforced 8 times should outrank a one-off remark.
    Frequency = how many points in this collection share the same lesson/
    correction shape (we approximate via near-duplicate count above a
    sub-threshold).
  - SPECIFICITY: a long, precise decision doc should outrank a short journal
    line that happened to be vector-close.

Final score:

    final = α · sim_norm
          + β · recency_norm
          + γ · frequency_norm
          + δ · specificity_norm

Weights live per-agent and per-domain (e.g. Will/trading weighs recency more
than Varro/docs). Defaults are documented in PROFILES below.

USAGE
-----
    from recall import recall, RecallResult

    hits = recall(
        collection="corrections",
        query="reply on mattermost",
        agent="orchestrator",
        domain="mattermost",
        limit=5,
    )
    for h in hits:
        print(h.score, h.breakdown, h.text)

CLI
---
    python3 recall.py search <collection> "<query>" [--agent X] [--domain Y]
                                                    [--limit N] [--explain]
    python3 recall.py profiles
    python3 recall.py audit-tail [N]

LOG
---
Every recall is appended (NDJSON) to:
    ~/.openclaw/workspace/state/recall-audit.ndjson

Each entry has: timestamp, agent, domain, collection, query, weights, top
results with full score breakdown. We use this to tune weights against
real query history.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import urllib.request
import urllib.error


# ============================================================
# Config
# ============================================================

QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = 768

WORKSPACE = os.environ.get("OPENCLAW_WORKSPACE",
                           os.path.expanduser("~/.openclaw/workspace"))
AUDIT_LOG = os.path.join(WORKSPACE, "state", "recall-audit.ndjson")

# Half-lives (in days) for recency decay. Per-agent overrides below.
DEFAULT_HALF_LIFE_DAYS = 30.0


# ============================================================
# Per-agent / per-domain weight profiles
# ============================================================
#
# Weights must sum to ~1.0 per profile. They can be tuned by editing this
# table — no code change needed elsewhere.
#
# alpha  = similarity weight  (vector closeness)
# beta   = recency weight     (newer wins; exponential decay)
# gamma  = frequency weight   (reinforced rules win)
# delta  = specificity weight (longer/structured payloads win)
#
# half_life_days controls recency decay: a point this many days old gets
# its recency component halved.
#
# A domain override extends/overrides the agent default for one query domain.

# ----------------------------------------------------------------------------
# Weight profiles
# ----------------------------------------------------------------------------
# Each agent gets a baseline profile of
#     final_score = alpha*similarity + beta*recency + gamma*frequency + delta*specificity
# plus a half-life in days for the recency decay. Domains nested under
# `domains` override the baseline for that one query domain.
#
# Tweak for your own agent. The supplied profiles match the production setup
# described in the workshop handout.
PROFILES: dict[str, dict] = {
    "orchestrator": {
        # Always-on personal-assistant agent. Slight bias to similarity with
        # recency overrides for fast-moving chat surfaces.
        "alpha": 0.55, "beta": 0.20, "gamma": 0.15, "delta": 0.10,
        "half_life_days": 30.0,
        "domains": {
            # Chat surfaces — recency-heavy because rules change weekly.
            "mattermost": {"alpha": 0.45, "beta": 0.30, "gamma": 0.20, "delta": 0.05, "half_life_days": 14.0},
            "telegram":   {"alpha": 0.45, "beta": 0.30, "gamma": 0.20, "delta": 0.05, "half_life_days": 14.0},
            "discord":    {"alpha": 0.45, "beta": 0.30, "gamma": 0.20, "delta": 0.05, "half_life_days": 14.0},
            # Long-running project context — decisions persist for months.
            "project":    {"alpha": 0.50, "beta": 0.10, "gamma": 0.20, "delta": 0.20, "half_life_days": 90.0},
            # Infra notes — weekly churn but specificity matters for configs.
            "infrastructure": {"alpha": 0.45, "beta": 0.25, "gamma": 0.20, "delta": 0.10, "half_life_days": 30.0},
            # People / preferences — frequency dominates, recency near-zero.
            "people":     {"alpha": 0.50, "beta": 0.10, "gamma": 0.30, "delta": 0.10, "half_life_days": 180.0},
            # Daily journals — mostly recent context.
            "journal":    {"alpha": 0.50, "beta": 0.30, "gamma": 0.10, "delta": 0.10, "half_life_days": 14.0},
            # Reference docs — specificity dominates, recency suppressed.
            "reference":  {"alpha": 0.55, "beta": 0.10, "gamma": 0.10, "delta": 0.25, "half_life_days": 120.0},
        },
    },
    "peer": {
        # Team-facing peer agent (lives in a chat channel, ops-focused).
        "alpha": 0.50, "beta": 0.25, "gamma": 0.15, "delta": 0.10,
        "half_life_days": 21.0,
    },
    "research": {
        # Long-form research agent (slow churn, factual specificity).
        "alpha": 0.55, "beta": 0.10, "gamma": 0.15, "delta": 0.20,
        "half_life_days": 60.0,
    },
    # Fallback if the requested agent is unknown.
    "_default": {
        "alpha": 0.55, "beta": 0.20, "gamma": 0.15, "delta": 0.10,
        "half_life_days": 30.0,
    },
}


def resolve_profile(agent: Optional[str], domain: Optional[str]) -> dict:
    """Return the weight profile for (agent, domain), falling back to defaults."""
    base = dict(PROFILES.get(agent or "_default", PROFILES["_default"]))
    domain_overrides = base.pop("domains", {})
    if domain and domain in domain_overrides:
        # Domain override completely replaces base weights for those keys
        base.update(domain_overrides[domain])
    base.setdefault("alpha", 0.55)
    base.setdefault("beta", 0.20)
    base.setdefault("gamma", 0.15)
    base.setdefault("delta", 0.10)
    base.setdefault("half_life_days", DEFAULT_HALF_LIFE_DAYS)
    return base


# ============================================================
# HTTP plumbing (kept dependency-free; stdlib only)
# ============================================================

def _request(url, data=None, api_key=None, method=None, timeout=30):
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if api_key:
        headers["api-key"] = api_key
    body = json.dumps(data).encode() if data is not None else None
    m = method or ("POST" if data is not None else "GET")
    req = urllib.request.Request(url, body, headers, method=m)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        raise RuntimeError(f"{m} {url} → {e.code} {raw[:300]}") from e


def _embed(text: str) -> list[float]:
    if not text or not text.strip():
        raise ValueError("empty text for embedding")
    resp = _request(f"{OLLAMA_URL}/api/embed",
                    {"model": EMBED_MODEL, "input": text})
    embeds = resp.get("embeddings") or []
    if not embeds:
        raise RuntimeError(f"no embedding returned from {EMBED_MODEL}")
    return embeds[0]


def _collection_exists(name: str) -> bool:
    try:
        _request(f"{QDRANT_URL}/collections/{name}", api_key=QDRANT_API_KEY)
        return True
    except RuntimeError:
        return False


def _qdrant_search(collection: str, vector: list[float], limit: int,
                   score_threshold: float = 0.0, filters=None) -> list[dict]:
    body = {"vector": vector, "limit": limit, "with_payload": True,
            "score_threshold": score_threshold}
    if filters:
        body["filter"] = filters
    resp = _request(f"{QDRANT_URL}/collections/{collection}/points/search",
                    body, QDRANT_API_KEY)
    return resp.get("result", [])


# ============================================================
# Result + scoring
# ============================================================

@dataclass
class RecallResult:
    score: float                 # final composite score, 0..1
    similarity: float            # raw cosine similarity (Qdrant returns 0..1 for cosine)
    breakdown: dict              # {alpha_contrib, beta_contrib, gamma_contrib, delta_contrib}
    text: str
    payload: dict
    age_days: float
    duplicate_count: int
    text_len: int

    def to_json(self) -> dict:
        return {
            "score": round(self.score, 4),
            "similarity": round(self.similarity, 4),
            "breakdown": {k: round(v, 4) for k, v in self.breakdown.items()},
            "text_preview": self.text[:200],
            "agent": self.payload.get("agent", ""),
            "domain": self.payload.get("domain", ""),
            "age_days": round(self.age_days, 2),
            "duplicate_count": self.duplicate_count,
            "text_len": self.text_len,
        }


def _payload_timestamp_seconds(payload: dict) -> Optional[float]:
    """Pull a unix-seconds timestamp out of a payload, no matter the format."""
    for key in ("timestamp", "stored_at", "ts", "created_at"):
        v = payload.get(key)
        if v is None:
            continue
        # int / float — assume seconds (or ms — heuristic)
        if isinstance(v, (int, float)):
            if v > 1e12:    # ms
                return float(v) / 1000.0
            return float(v)
        # ISO-8601 string
        if isinstance(v, str):
            try:
                # Strip trailing Z, parse with fromisoformat
                s = v.replace("Z", "+00:00")
                from datetime import datetime
                return datetime.fromisoformat(s).timestamp()
            except Exception:
                continue
    return None


def _recency_score(payload: dict, half_life_days: float, now: float) -> tuple[float, float]:
    """Returns (recency_score 0..1, age_in_days). No timestamp -> neutral 0.5."""
    ts = _payload_timestamp_seconds(payload)
    if ts is None:
        return 0.5, -1.0
    age_days = max(0.0, (now - ts) / 86400.0)
    # Exponential decay: score = 0.5^(age / half_life)
    score = 0.5 ** (age_days / max(half_life_days, 1.0))
    return score, age_days


def _specificity_score(payload: dict) -> tuple[float, int]:
    """Long, structured payloads score higher. Saturates near 800 chars."""
    text = payload.get("text") or ""
    text_len = len(text)
    # Sigmoid-ish: 200 chars -> 0.4, 500 -> 0.65, 1000 -> 0.85, 2000+ -> ~0.95
    score = 1.0 - math.exp(-text_len / 600.0)
    return min(score, 1.0), text_len


def _frequency_score(
    my_payload: dict,
    my_sim: float,
    candidates: list[dict],
    sim_threshold: float = 0.65,
) -> tuple[float, int]:
    """
    Approximate frequency: how many OTHER candidates plausibly reinforce this
    same lesson/correction? Two signals combine:

      1. Shared structural field (same `domain` or same `type`) AND high
         enough similarity to be on-topic.
      2. Vector similarity above `sim_threshold` (lowered from naive 0.85
         after observing real corpus — corrections that mean the same thing
         often sit at 0.65-0.80 cosine).

    A reinforced rule will have multiple sibling points; a one-off remark
    will have none.
    """
    my_domain = (my_payload.get("domain") or "").strip().lower()
    my_type = (my_payload.get("type") or "").strip().lower()
    my_text = my_payload.get("text") or ""
    dup_count = 0
    soft_count = 0
    for c in candidates:
        c_payload = c.get("payload") or {}
        c_text = c_payload.get("text") or ""
        if c_text == my_text:
            continue  # don't count self
        c_sim = float(c.get("score", 0.0))
        c_domain = (c_payload.get("domain") or "").strip().lower()
        c_type = (c_payload.get("type") or "").strip().lower()
        structural_match = (my_domain and c_domain == my_domain) or \
                           (my_type and c_type == my_type)

        if c_sim >= 0.85:
            # near-duplicate — strong reinforcement
            dup_count += 1
        elif structural_match and c_sim >= sim_threshold:
            # same domain/type AND on-topic — strong reinforcement
            dup_count += 1
        elif c_sim >= 0.55:
            # soft co-occurrence: candidates broadly on topic
            # contribute partial reinforcement
            soft_count += 1

    # Combine: each soft hit counts as 0.4 of a strong dup
    effective = dup_count + 0.4 * soft_count
    # Saturating: 0 -> 0.0, 1 -> 0.5, 3 -> 0.8, 6+ -> ~0.95
    score = 1.0 - math.exp(-effective / 1.5)
    return score, dup_count + soft_count


# ============================================================
# Public API
# ============================================================

def recall(
    collection: str,
    query: str,
    agent: Optional[str] = None,
    domain: Optional[str] = None,
    limit: int = 5,
    candidate_pool: int = 30,
    score_threshold: float = 0.30,
    filters: Optional[dict] = None,
    explain: bool = False,
    audit: bool = True,
) -> list[RecallResult]:
    """
    Run composite-rank retrieval over a Qdrant collection.

    1. Pull `candidate_pool` raw similarity hits from Qdrant.
    2. For each candidate compute recency, frequency (relative to other
       candidates), and specificity scores.
    3. Combine via the active profile's weights.
    4. Sort by composite score, return top `limit`.
    5. Append an audit-log entry (NDJSON).
    """
    if not _collection_exists(collection):
        return []

    profile = resolve_profile(agent, domain)
    alpha = profile["alpha"]
    beta = profile["beta"]
    gamma = profile["gamma"]
    delta = profile["delta"]
    half_life = profile["half_life_days"]

    vector = _embed(query)
    raw = _qdrant_search(collection, vector, candidate_pool,
                         score_threshold=score_threshold, filters=filters)
    if not raw:
        if audit:
            _audit({"timestamp": int(time.time()), "agent": agent,
                    "domain": domain, "collection": collection,
                    "query": query, "profile": profile, "hits": [],
                    "candidate_pool": 0})
        return []

    now = time.time()
    sims = [r.get("score", 0.0) for r in raw]

    out: list[RecallResult] = []
    for i, r in enumerate(raw):
        payload = r.get("payload") or {}
        sim = float(r.get("score", 0.0))

        # Other candidates feed frequency calculation
        rec_score, age_days = _recency_score(payload, half_life, now)
        spec_score, text_len = _specificity_score(payload)
        freq_score, dup_count = _frequency_score(payload, sim, raw)

        # Normalize sim into 0..1 (Qdrant cosine returns -1..1; clip negatives)
        sim_norm = max(0.0, min(1.0, sim))

        a_c = alpha * sim_norm
        b_c = beta * rec_score
        g_c = gamma * freq_score
        d_c = delta * spec_score
        final = a_c + b_c + g_c + d_c

        out.append(RecallResult(
            score=final,
            similarity=sim,
            breakdown={"alpha_sim": a_c, "beta_rec": b_c,
                       "gamma_freq": g_c, "delta_spec": d_c},
            text=payload.get("text", ""),
            payload=payload,
            age_days=age_days,
            duplicate_count=dup_count,
            text_len=text_len,
        ))

    out.sort(key=lambda r: r.score, reverse=True)
    top = out[:limit]

    if audit:
        _audit({
            "timestamp": int(now),
            "agent": agent,
            "domain": domain,
            "collection": collection,
            "query": query,
            "profile": profile,
            "candidate_pool": len(raw),
            "hits": [r.to_json() for r in top],
        })

    return top


def recall_multi(
    collections: list[str],
    query: str,
    agent: Optional[str] = None,
    domain: Optional[str] = None,
    limit_per: int = 3,
    score_threshold: float = 0.30,
) -> dict[str, list[RecallResult]]:
    """Run recall across multiple collections in one call."""
    results = {}
    for c in collections:
        results[c] = recall(c, query, agent=agent, domain=domain,
                            limit=limit_per, score_threshold=score_threshold)
    return results


# ============================================================
# Audit log
# ============================================================

def _audit(entry: dict):
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        # Best-effort; never break a real call because audit failed.
        sys.stderr.write(f"[recall.audit warn] {e}\n")


def audit_tail(n: int = 10) -> list[dict]:
    if not os.path.exists(AUDIT_LOG):
        return []
    with open(AUDIT_LOG) as f:
        lines = f.readlines()
    return [json.loads(l) for l in lines[-n:]]


# ============================================================
# CLI
# ============================================================

def _print_results(results: list[RecallResult], explain: bool):
    if not results:
        print("(no results)")
        return
    for i, r in enumerate(results, 1):
        snippet = r.text[:200].replace("\n", " / ")
        agent = r.payload.get("agent", "?")
        domain = r.payload.get("domain", "")
        print(f"#{i}  score={r.score:.3f}  sim={r.similarity:.3f}  "
              f"age={r.age_days:.1f}d  dups={r.duplicate_count}  "
              f"len={r.text_len}  agent={agent}{' domain=' + domain if domain else ''}")
        print(f"     {snippet}")
        if explain:
            b = r.breakdown
            print(f"     breakdown: α·sim={b['alpha_sim']:.3f} "
                  f"β·rec={b['beta_rec']:.3f} γ·freq={b['gamma_freq']:.3f} "
                  f"δ·spec={b['delta_spec']:.3f}")
        print()


def main():
    p = argparse.ArgumentParser(prog="recall.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("search")
    s.add_argument("collection")
    s.add_argument("query")
    s.add_argument("--agent", default=None)
    s.add_argument("--domain", default=None)
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--pool", type=int, default=30)
    s.add_argument("--threshold", type=float, default=0.30)
    s.add_argument("--explain", action="store_true")

    sub.add_parser("profiles")

    at = sub.add_parser("audit-tail")
    at.add_argument("n", type=int, nargs="?", default=10)

    args = p.parse_args()

    if args.cmd == "search":
        results = recall(args.collection, args.query,
                         agent=args.agent, domain=args.domain,
                         limit=args.limit, candidate_pool=args.pool,
                         score_threshold=args.threshold,
                         explain=args.explain)
        _print_results(results, args.explain)
        return

    if args.cmd == "profiles":
        print(json.dumps(PROFILES, indent=2))
        return

    if args.cmd == "audit-tail":
        for entry in audit_tail(args.n):
            ts = entry.get("timestamp", 0)
            agent = entry.get("agent", "-")
            dom = entry.get("domain", "-")
            col = entry.get("collection", "-")
            q = entry.get("query", "")[:80]
            print(f"[{time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts))}] "
                  f"agent={agent} domain={dom} col={col}")
            print(f"  q={q!r}")
            for h in entry.get("hits", [])[:3]:
                print(f"    {h.get('score',0):.3f}  {h.get('text_preview','')[:120]}")
        return

    p.print_help()


if __name__ == "__main__":
    main()
