#!/bin/bash
# Obsidian Vault Sync — push your workspace to an S3-compatible bucket.
#
# Mirrors your local workspace tree into the bucket so an Obsidian vault
# (configured with Remotely Save or s3fs) shows the same content as your
# agent sees on disk. Detects structural conflicts (files deleted in
# Obsidian since the last sync) and aborts rather than overwriting them.
#
# Usage: ./obsidian-sync.sh [--dry-run]
#
# REQUIRED ENV VARS (set in .env at the repo root, see .env.example):
#   S3_ENDPOINT       e.g. https://s3.hippius.com  or  https://s3.amazonaws.com
#   S3_BUCKET         e.g. my-agent-memory
#   S3_ACCESS_KEY     scoped IAM key with read+write on this bucket only
#   S3_SECRET_KEY     matching secret
#   VAULT_PREFIX      key prefix inside the bucket (default: agent/). Trailing slash required.
#   WORKSPACE         absolute path to your workspace (default: $HOME/.openclaw/workspace)
#
# OPTIONAL:
#   QDRANT_REINDEX=1  re-mine the workspace into Qdrant after a successful sync

set -euo pipefail

# Load .env if present
ENV_FILE="$(dirname "$(readlink -f "$0")")/../.env"
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
fi

: "${S3_ENDPOINT:?S3_ENDPOINT not set}"
: "${S3_BUCKET:?S3_BUCKET not set}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY not set}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY not set}"
VAULT_PREFIX="${VAULT_PREFIX:-agent/}"
WORKSPACE="${WORKSPACE:-$HOME/.openclaw/workspace}"

export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
EP="--endpoint-url $S3_ENDPOINT"
BUCKET="s3://$S3_BUCKET"
WS="$WORKSPACE"
STAGING="/tmp/obsidian-sync-staging-$$"
MANIFEST="/tmp/obsidian-sync-manifest-$$"
PREV_MANIFEST="$HOME/.cache/obsidian-sync-prev-manifest.txt"
DRY_RUN="${1:-}"

trap 'rm -rf "$STAGING" "$MANIFEST".local "$MANIFEST".remote' EXIT
rm -rf "$STAGING"; mkdir -p "$STAGING/$VAULT_PREFIX"
mkdir -p "$(dirname "$PREV_MANIFEST")"

# ---------------------------------------------------------------------------
# Build local staging area mirroring the canonical workspace tree.
# Edit the lists below to match the tree you keep on disk.
# ---------------------------------------------------------------------------

# Core identity files (Layer 1) live at the workspace root.
mkdir -p "$STAGING/$VAULT_PREFIX/Memory"
for f in MEMORY.md SOUL.md USER.md IDENTITY.md TODO.md CONTEXT.md \
         AGENTS.md AGENT-RELIABILITY.md TOOLS.md HEARTBEAT.md; do
  [ -f "$WS/$f" ] && cp "$WS/$f" "$STAGING/$VAULT_PREFIX/Memory/$f"
done

# Project overviews — one .md per active project plus optional sub-folders.
mkdir -p "$STAGING/$VAULT_PREFIX/Projects"
for f in "$WS"/memory/projects/*.md; do
  [ -f "$f" ] && cp "$f" "$STAGING/$VAULT_PREFIX/Projects/$(basename "$f")"
done
for d in "$WS"/memory/projects/*/; do
  [ -d "$d" ] || continue
  sub=$(basename "$d")
  mkdir -p "$STAGING/$VAULT_PREFIX/Projects/$sub"
  for f in "$d"*.md; do
    [ -f "$f" ] && cp "$f" "$STAGING/$VAULT_PREFIX/Projects/$sub/$(basename "$f")"
  done
done

# Drafts (work-in-progress) — flat + one level of sub-folders for versioning.
mkdir -p "$STAGING/$VAULT_PREFIX/Drafts"
for f in "$WS"/drafts/*.md; do
  [ -f "$f" ] && cp "$f" "$STAGING/$VAULT_PREFIX/Drafts/$(basename "$f")"
done
for d in "$WS"/drafts/*/; do
  [ -d "$d" ] || continue
  sub=$(basename "$d")
  mkdir -p "$STAGING/$VAULT_PREFIX/Drafts/$sub"
  for f in "$d"*.md; do
    [ -f "$f" ] && cp "$f" "$STAGING/$VAULT_PREFIX/Drafts/$sub/$(basename "$f")"
  done
done

# Finalised docs and dated reports.
mkdir -p "$STAGING/$VAULT_PREFIX/Docs" "$STAGING/$VAULT_PREFIX/Reports"
for f in "$WS"/docs/*.md;    do [ -f "$f" ] && cp "$f" "$STAGING/$VAULT_PREFIX/Docs/$(basename "$f")"; done
for f in "$WS"/reports/*.md; do [ -f "$f" ] && cp "$f" "$STAGING/$VAULT_PREFIX/Reports/$(basename "$f")"; done

# Archived/completed work (kept quarterly).
mkdir -p "$STAGING/$VAULT_PREFIX/Archive"
for d in "$WS"/archive/*/; do
  [ -d "$d" ] || continue
  sub=$(basename "$d")
  mkdir -p "$STAGING/$VAULT_PREFIX/Archive/$sub"
  for f in "$d"*.md; do
    [ -f "$f" ] && cp "$f" "$STAGING/$VAULT_PREFIX/Archive/$sub/$(basename "$f")"
  done
done

# Daily journals.
mkdir -p "$STAGING/$VAULT_PREFIX/Journal"
for f in "$WS"/memory/[0-9][0-9][0-9][0-9]-*.md; do
  [ -f "$f" ] && cp "$f" "$STAGING/$VAULT_PREFIX/Journal/$(basename "$f")"
done

# NEVER ship the secrets dir. Defence in depth.
rm -rf "$STAGING/$VAULT_PREFIX/secrets" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Conflict detection — abort if files in the bucket vanished since last sync
# (almost always means the user moved/deleted them in Obsidian and we'd lose
# their edits by overwriting).
# ---------------------------------------------------------------------------

# Hard timeout on the listing — S3 endpoints can hang and burn the cron budget.
timeout 90 aws s3 ls "$BUCKET/$VAULT_PREFIX" $EP --recursive \
  --cli-read-timeout 60 --cli-connect-timeout 20 2>/dev/null \
  | awk '{print $4}' | sort > "$MANIFEST.remote" || true

if [ ! -s "$MANIFEST.remote" ] && [ -z "$DRY_RUN" ]; then
  echo "WARN: remote manifest empty or listing failed — skipping sync to avoid partial upload"
  echo "SYNC_SKIPPED_UPSTREAM — $(date -u '+%Y-%m-%d %H:%M UTC')"
  exit 0
fi

(cd "$STAGING" && find "$VAULT_PREFIX" -name "*.md" | sort) > "$MANIFEST.local"

if [ -f "$PREV_MANIFEST" ]; then
  DISAPPEARED=$(comm -23 "$PREV_MANIFEST" "$MANIFEST.remote" 2>/dev/null || true)
  if [ -n "$DISAPPEARED" ]; then
    echo "CONFLICT: Files disappeared from remote since last sync (user reorganised?):"
    echo "$DISAPPEARED"
    echo "Aborting. Inspect, then either restore from S3 or refresh \$PREV_MANIFEST and rerun."
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Sync.
# ---------------------------------------------------------------------------

if [ "$DRY_RUN" = "--dry-run" ]; then
  echo "DRY RUN — would sync these files:"
  (cd "$STAGING" && find . -name "*.md" | sort)
  echo "Total: $(cd "$STAGING" && find . -name "*.md" | wc -l) files"
else
  aws configure set default.s3.max_concurrent_requests 32 2>/dev/null || true
  aws s3 sync "$STAGING/$VAULT_PREFIX" "$BUCKET/$VAULT_PREFIX" $EP \
    --size-only --cli-read-timeout 120 --cli-connect-timeout 30
  echo "SYNC_OK — $(date -u '+%Y-%m-%d %H:%M UTC')"
fi

cp "$MANIFEST.remote" "$PREV_MANIFEST"

# Optional: re-mine into Qdrant workspace-corpus so the search index stays fresh.
if [ "${QDRANT_REINDEX:-0}" = "1" ]; then
  timeout 600 python3 "$(dirname "$(readlink -f "$0")")/mine-workspace.py" --since 24h 2>&1 | tail -3 || true
fi
