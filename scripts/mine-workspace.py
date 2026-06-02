#!/usr/bin/env python3
"""
mine-workspace.py — walk the workspace, chunk markdown files, embed with
Ollama (nomic-embed-text, 768d), write into Qdrant collection
`workspace-corpus`.

Replaces the third-party `mempalace` package as the workspace miner.
Designed to integrate with our composite retrieval (`scripts/recall.py`)
so the same scorer applies uniformly to corrections, experiences,
preferences, principles AND mined workspace files.

ARCHITECTURE
------------
Source folders → chunks (~800 chars overlap 100) → Ollama embed (768d) →
Qdrant point with payload {wing, room, source_file, chunk_index, mtime,
chunk_count, text}.

Wings (topic categories) are detected by keyword matching against chunk
text. Rooms (sub-categories) default to the file basename.

Idempotent: deterministic point IDs from sha256(source_file + chunk_index +
chunk_text). Re-mining the same file overwrites the same IDs. Files that
disappear from the workspace are pruned by source_file scan at end of run.

USAGE
-----
    python3 scripts/mine-workspace.py                 # full mine, default folders
    python3 scripts/mine-workspace.py --since 24h     # only files modified in last 24h
    python3 scripts/mine-workspace.py --dry-run       # show what would be mined
    python3 scripts/mine-workspace.py --prune         # delete points whose source_file no longer exists
    python3 scripts/mine-workspace.py --stats         # show corpus stats
    python3 scripts/mine-workspace.py --rebuild       # drop collection, full re-mine

CONFIG (top of file)
--------------------
SOURCE_DIRS       — folders to walk
WING_KEYWORDS     — topic detection
CHUNK_SIZE/OVERLAP — chunking knobs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional


# ============================================================
# Config
# ============================================================

WORKSPACE = os.environ.get("WORKSPACE",
                           os.environ.get("OPENCLAW_WORKSPACE",
                           os.path.expanduser("~/.openclaw/workspace")))

QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = 768

COLLECTION = "workspace-corpus"

# Folders to mine, relative to WORKSPACE. Each entry is (path, default_wing).
# default_wing is used if no keyword wing is detected.
SOURCE_DIRS: list[tuple[str, str]] = [
    # Top-level identity files
    ("MEMORY.md",            "core"),
    ("TODO.md",              "core"),
    ("CONTEXT.md",           "core"),
    ("SOUL.md",              "core"),
    ("USER.md",              "core"),
    ("IDENTITY.md",          "core"),
    ("TOOLS.md",             "core"),
    ("AGENTS.md",            "core"),
    ("AGENTS-SUBAGENT-RULES.md", "core"),
    ("AGENT-RELIABILITY.md", "core"),
    # Folders walked recursively
    ("memory/projects",      None),       # wing comes from file name + content
    ("memory",               "journal"),  # date-stamped journals
    ("docs",                 "reference"),
    ("drafts",               "reference"),
    ("reports",              "reference"),
    ("archive",              "reference"),
]

# Topic-detection keywords. First wing whose keyword set matches >= 1 keyword
# in the chunk wins; ties broken by order in this dict (insertion order).
#
# EDIT THIS for your own projects. The keys become the `wing` payload field on
# every mined chunk, and the values are case-insensitive substrings checked
# against the chunk text. Path-based wing detection (folder name in
# memory/projects/<wing>/) takes priority; keywords are the fallback when a
# chunk lives outside a project folder (e.g. a journal entry).
#
# The examples below are stubs — replace with your real project names.
WING_KEYWORDS: dict[str, list[str]] = {
    # "project_alpha":   ["alpha", "first project codename", "key term"],
    # "project_beta":    ["beta", "another codename"],
    "infrastructure":  ["nginx", "cloudflare", "cron", "backup", "deploy",
                        "ssh", "openclaw", "tailscale", "systemd", "qdrant"],
    "personal":        ["family", "home", "birthday"],
}

# Chunking
CHUNK_SIZE = 800     # chars per chunk
CHUNK_OVERLAP = 100  # overlap chars

# File filter
MD_EXT = {".md", ".markdown"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv",
             "obsidian", "vault-mirror", "archive_old", "_drafts",
             # Avoid mining secrets — defence in depth (.gitignore should cover too)
             "secrets"}

# Skip individual filenames
SKIP_FILES = {"BOOTSTRAP.md", "MIGRATION.md"}


# ============================================================
# HTTP plumbing
# ============================================================

def _request(url, data=None, api_key=None, method=None, timeout=60):
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
        raise RuntimeError(f"{m} {url} -> {e.code} {raw[:300]}") from e


def _embed(text: str) -> list[float]:
    if not text or not text.strip():
        raise ValueError("empty text for embedding")
    resp = _request(f"{OLLAMA_URL}/api/embed",
                    {"model": EMBED_MODEL, "input": text})
    embeds = resp.get("embeddings") or []
    if not embeds:
        raise RuntimeError(f"no embedding from {EMBED_MODEL}")
    return embeds[0]


def _ensure_collection():
    try:
        _request(f"{QDRANT_URL}/collections/{COLLECTION}", api_key=QDRANT_API_KEY)
        return
    except RuntimeError:
        pass
    body = {"vectors": {"size": EMBED_DIM, "distance": "Cosine"}}
    _request(f"{QDRANT_URL}/collections/{COLLECTION}", body, QDRANT_API_KEY,
             method="PUT")
    print(f"[+] created collection: {COLLECTION}")


def _drop_collection():
    try:
        _request(f"{QDRANT_URL}/collections/{COLLECTION}", api_key=QDRANT_API_KEY,
                 method="DELETE")
        print(f"[-] dropped collection: {COLLECTION}")
    except RuntimeError:
        pass


def _upsert_points(points: list[dict]):
    if not points:
        return
    body = {"points": points}
    _request(f"{QDRANT_URL}/collections/{COLLECTION}/points",
             body, QDRANT_API_KEY, method="PUT")


def _scroll_all(limit: int = 1000):
    """Yield every point in the collection (with payload, no vector)."""
    offset = None
    while True:
        body = {"limit": limit, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        resp = _request(f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
                        body, QDRANT_API_KEY)
        result = resp.get("result", {})
        points = result.get("points", [])
        for p in points:
            yield p
        offset = result.get("next_page_offset")
        if not offset:
            break


def _delete_by_ids(ids: list[int]):
    if not ids:
        return
    body = {"points": ids}
    _request(f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
             body, QDRANT_API_KEY, method="POST")


# ============================================================
# Chunking + wing detection
# ============================================================

_HDR_RE = re.compile(r"^(#+)\s+(.+)$", re.MULTILINE)


def chunk_markdown(text: str, size: int = CHUNK_SIZE,
                   overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Chunk markdown into ~size-char windows with overlap. Prefer to split at
    blank-line boundaries when possible to avoid mid-paragraph breaks.
    """
    text = text.replace("\r\n", "\n")
    if len(text) <= size:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    i = 0
    L = len(text)
    while i < L:
        end = min(i + size, L)
        # Prefer to break at a blank line within the last 200 chars of the window
        if end < L:
            window = text[i:end]
            cut = window.rfind("\n\n")
            if cut > size - 200:
                end = i + cut
        chunk = text[i:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= L:
            break
        i = max(end - overlap, i + 1)  # always make forward progress
    return chunks


def detect_wing(text: str, default: Optional[str], file_path: str) -> str:
    """Choose the wing for a chunk. Path hints first, then keyword match."""
    p = file_path.lower()
    # Strong path signals
    if "/projects/" in p:
        # Pull the project slug from the path: memory/projects/<slug>.md or .../<slug>/foo.md
        m = re.search(r"/projects/([a-z0-9-]+)", p)
        if m:
            slug = m.group(1)
            if slug in WING_KEYWORDS:
                return slug
            return slug
    if "/memory/" in p and re.search(r"/memory/\d{4}-\d{2}-\d{2}\.md$", p):
        return "journal"

    # Content-based detection
    blob = text.lower()
    for wing, kws in WING_KEYWORDS.items():
        for kw in kws:
            if kw in blob:
                return wing
    return default or "general"


def detect_room(file_path: str) -> str:
    """Room = the basename without extension; folders flatten to 'general'."""
    base = os.path.basename(file_path)
    if base.lower().endswith((".md", ".markdown")):
        base = os.path.splitext(base)[0]
    return base or "general"


# ============================================================
# File walking
# ============================================================

def walk_targets(since_seconds: Optional[float]) -> list[str]:
    """Yield absolute file paths from SOURCE_DIRS that pass filters."""
    cutoff = (time.time() - since_seconds) if since_seconds else None
    paths: list[str] = []
    seen: set[str] = set()
    for rel, _wing in SOURCE_DIRS:
        full = os.path.join(WORKSPACE, rel)
        if not os.path.exists(full):
            continue
        if os.path.isfile(full):
            if full in seen:
                continue
            if not _eligible(full, cutoff):
                continue
            seen.add(full)
            paths.append(full)
            continue
        for root, dirs, files in os.walk(full):
            # prune skip dirs in-place
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in files:
                if fn in SKIP_FILES:
                    continue
                if os.path.splitext(fn)[1].lower() not in MD_EXT:
                    continue
                fp = os.path.join(root, fn)
                if fp in seen:
                    continue
                if not _eligible(fp, cutoff):
                    continue
                seen.add(fp)
                paths.append(fp)
    return paths


def _eligible(path: str, cutoff: Optional[float]) -> bool:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    if st.st_size == 0:
        return False
    if st.st_size > 2_000_000:  # skip files > 2 MB (binary-ish)
        return False
    if cutoff is not None and st.st_mtime < cutoff:
        return False
    return True


# ============================================================
# Mining
# ============================================================

def make_point_id(source_file: str, chunk_index: int, chunk_text: str) -> int:
    """Deterministic 63-bit id (Qdrant accepts uint64; we use 60 bits to be safe)."""
    # Include chunk_text so a re-chunk creates new ids and the old ones can be
    # pruned by the post-mine sweep.
    h = hashlib.sha256(
        f"{source_file}::{chunk_index}::{chunk_text[:200]}".encode()
    ).hexdigest()
    return int(h[:15], 16)


def mine_one(path: str, default_wing: Optional[str]) -> int:
    """Mine a single file. Returns number of chunks written."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        print(f"  [skip:{path}] {e}", file=sys.stderr)
        return 0
    if not text.strip():
        return 0

    chunks = chunk_markdown(text)
    if not chunks:
        return 0

    mtime = os.path.getmtime(path)
    rel = os.path.relpath(path, WORKSPACE)

    points = []
    for idx, chunk in enumerate(chunks):
        try:
            vec = _embed(chunk)
        except Exception as e:
            print(f"  [embed-fail:{rel}#{idx}] {e}", file=sys.stderr)
            continue
        wing = detect_wing(chunk, default_wing, path)
        room = detect_room(path)
        payload = {
            "text": chunk,
            "wing": wing,
            "room": room,
            "source_file": rel,
            "chunk_index": idx,
            "chunk_count": len(chunks),
            "mtime": mtime,
            "timestamp": int(mtime),  # for recency scoring in recall.py
            "agent": "workspace",
            "type": "workspace_chunk",
            "domain": wing,           # so recall.py per-domain weights kick in
        }
        pid = make_point_id(rel, idx, chunk)
        points.append({"id": pid, "vector": vec, "payload": payload})

    if points:
        _upsert_points(points)
    return len(points)


def _wing_for_dir(rel_dir: str) -> Optional[str]:
    """Given a directory rel-path, return the configured default wing if any."""
    for d, w in SOURCE_DIRS:
        if d == rel_dir:
            return w
    return None


def mine_all(since_seconds: Optional[float], dry_run: bool) -> dict:
    paths = walk_targets(since_seconds)
    print(f"[*] {len(paths)} candidate file(s)")
    if dry_run:
        for p in paths[:50]:
            print("    DRY:", os.path.relpath(p, WORKSPACE))
        if len(paths) > 50:
            print(f"    ... and {len(paths) - 50} more")
        return {"files": len(paths), "chunks": 0, "dry_run": True}

    _ensure_collection()
    total_chunks = 0
    t0 = time.time()
    files_done = 0
    for p in paths:
        # Determine default wing from the source-dirs config
        default_wing = None
        for d, w in SOURCE_DIRS:
            full_d = os.path.join(WORKSPACE, d)
            if p == full_d or p.startswith(full_d + os.sep):
                default_wing = w
                break
        n = mine_one(p, default_wing)
        total_chunks += n
        files_done += 1
        if files_done % 20 == 0:
            elapsed = time.time() - t0
            print(f"    {files_done}/{len(paths)} files, "
                  f"{total_chunks} chunks ({elapsed:.1f}s elapsed)")
    elapsed = time.time() - t0
    print(f"[+] mined {files_done} file(s), {total_chunks} chunk(s) in {elapsed:.1f}s")
    return {"files": files_done, "chunks": total_chunks,
            "elapsed": elapsed, "dry_run": False}


def prune_orphans() -> int:
    """Delete points whose source_file no longer exists on disk."""
    to_delete: list[int] = []
    seen_files: set[str] = set()
    for pt in _scroll_all():
        payload = pt.get("payload") or {}
        src = payload.get("source_file") or ""
        if not src or src in seen_files:
            if src and src not in seen_files and not _file_exists(src):
                to_delete.append(pt["id"])
            continue
        seen_files.add(src)
        if not _file_exists(src):
            # Need to collect all chunks for this file, not just the first
            pass
    # Second pass: collect ALL chunk ids for missing files
    missing_files: set[str] = set()
    for src in seen_files:
        if not _file_exists(src):
            missing_files.add(src)
    if missing_files:
        for pt in _scroll_all():
            payload = pt.get("payload") or {}
            if payload.get("source_file") in missing_files:
                to_delete.append(pt["id"])
    if to_delete:
        # de-dup
        to_delete = list(set(to_delete))
        _delete_by_ids(to_delete)
        print(f"[-] pruned {len(to_delete)} orphan chunk(s) from "
              f"{len(missing_files)} missing file(s)")
    else:
        print("[*] no orphans to prune")
    return len(to_delete)


def _file_exists(rel: str) -> bool:
    return os.path.exists(os.path.join(WORKSPACE, rel))


def stats() -> dict:
    try:
        resp = _request(f"{QDRANT_URL}/collections/{COLLECTION}",
                        api_key=QDRANT_API_KEY)
    except RuntimeError:
        print("(collection does not exist yet)")
        return {}
    points = resp["result"].get("points_count", 0)
    print(f"collection: {COLLECTION}")
    print(f"points:     {points}")
    print(f"vector dim: {EMBED_DIM}")

    # Wing breakdown via scroll
    wings: dict[str, int] = {}
    files: set[str] = set()
    oldest = None
    newest = None
    for pt in _scroll_all():
        p = pt.get("payload") or {}
        wings[p.get("wing", "?")] = wings.get(p.get("wing", "?"), 0) + 1
        if p.get("source_file"):
            files.add(p["source_file"])
        mt = p.get("mtime")
        if isinstance(mt, (int, float)):
            if oldest is None or mt < oldest:
                oldest = mt
            if newest is None or mt > newest:
                newest = mt
    print(f"files:      {len(files)}")
    print("wings:")
    for w, n in sorted(wings.items(), key=lambda x: -x[1]):
        print(f"  {w:18s} {n:5d}")
    if oldest:
        print(f"oldest mtime: {datetime.fromtimestamp(oldest)}")
    if newest:
        print(f"newest mtime: {datetime.fromtimestamp(newest)}")
    return {"points": points, "files": len(files), "wings": wings}


# ============================================================
# CLI
# ============================================================

def parse_since(s: str) -> float:
    """Parse '24h', '7d', '30m' into seconds."""
    if not s:
        return 0
    m = re.match(r"^(\d+)\s*([smhd])$", s.strip())
    if not m:
        raise ValueError(f"bad --since value: {s!r} (use e.g. 24h, 7d)")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since", default=None,
                   help="Only mine files modified within this window (e.g. 24h, 7d)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--prune", action="store_true",
                   help="Delete points whose source_file no longer exists")
    p.add_argument("--rebuild", action="store_true",
                   help="Drop collection and full re-mine")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    if args.stats:
        stats()
        return

    if args.rebuild:
        _drop_collection()

    since_seconds = parse_since(args.since) if args.since else None
    if since_seconds == 0:
        since_seconds = None

    result = mine_all(since_seconds, args.dry_run)

    if not args.dry_run:
        if args.prune or args.rebuild:
            prune_orphans()
        # Quick stats summary
        print("---")
        stats()

    if not args.dry_run and result.get("chunks") == 0 and not args.prune:
        # No new chunks but also no error: explicit prune anyway to keep clean
        prune_orphans()


if __name__ == "__main__":
    main()
