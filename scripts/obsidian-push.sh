#!/bin/bash
# Obsidian Fast Push — immediate upload of a single file or drafts/docs tree
#
# Use this when you need a file in Obsidian NOW, not on the next hourly sync.
# Does NOT run summaries, conflict detection, or re-mine. Just uploads.
#
# Usage:
#   obsidian-push.sh drafts/foo.md           # push single file
#   obsidian-push.sh docs/bar.md             # push single file
#   obsidian-push.sh drafts                  # push whole drafts/ tree
#   obsidian-push.sh docs                    # push whole docs/ tree
#   obsidian-push.sh reports                 # push whole reports/ tree
#
# Workspace detection (2026-05-13):
# - Walks up from cwd to find the first directory containing AGENTS.md.
# - That dir is the workspace root. Targets are resolved relative to it.
# - Works for Poppy's main workspace AND peer agent workspaces (e.g.
#   /home/openclaw/.openclaw/workspace/agents/lois/).
# - Legacy `WS=$HOME/.openclaw/workspace` is kept as a final fallback.
#
# Vault namespacing convention:
# - Pushes go to the SAME top-level Drafts/, Docs/, etc. regardless of which
#   peer pushed them. No <peer>/ subdir.
# - Peers SHOULD use a scope-prefixed filename (e.g. `news-monday.md`,
#   `infra-deploy.md`) so files don't collide with other peers' files.
#
# Exits 0 on success (or no-op), 1 on error.
set -euo pipefail

# Ensure we can find `aws` regardless of how the script is invoked (systemd,
# user shell, agent tool, ssh non-login). On peer agent boxes the openclaw
# user often has aws installed via pip --user under ~/.local/bin which isn't
# in systemd's default PATH.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# Credentials loaded from .env at the repo root (gitignored).
ENV_FILE="$(dirname "$(readlink -f "$0")")/../.env"
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
fi
: "${S3_ENDPOINT:?S3_ENDPOINT not set (see .env.example)}"
: "${S3_BUCKET:?S3_BUCKET not set}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY not set}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY not set}"
VAULT_PREFIX="${VAULT_PREFIX:-agent/}"
export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
EP="--endpoint-url $S3_ENDPOINT"
BUCKET="s3://$S3_BUCKET"

# --- Workspace detection ---
# Walk up from cwd to find a dir containing AGENTS.md
detect_ws() {
    local d="${PWD:-$(pwd)}"
    while [[ "$d" != "/" && "$d" != "" ]]; do
        if [[ -f "$d/AGENTS.md" ]]; then
            echo "$d"
            return 0
        fi
        d="$(dirname "$d")"
    done
    return 1
}

WS="$(detect_ws || true)"
if [[ -z "$WS" ]]; then
    # Final fallback: legacy default
    WS="$HOME/.openclaw/workspace"
fi

# --- Helpers ---
# Map a local path under $WS to its vault destination.
# Peers and Poppy share the same top-level Poppy/Drafts/, Poppy/Docs/, etc.
# Use a scope-prefixed filename (e.g. `taostats-news-...md`) to avoid collisions.
map_to_vault() {
    local rel="$1"
    case "$rel" in
        drafts/*)          echo "${VAULT_PREFIX}Drafts/${rel#drafts/}" ;;
        docs/*)            echo "${VAULT_PREFIX}Docs/${rel#docs/}" ;;
        reports/*)         echo "${VAULT_PREFIX}Reports/${rel#reports/}" ;;
        archive/*)         echo "${VAULT_PREFIX}Archive/${rel#archive/}" ;;
        memory/projects/*) echo "${VAULT_PREFIX}Projects/${rel#memory/projects/}" ;;
        *) echo "" ;;
    esac
}

if [ $# -eq 0 ]; then
    echo "usage: obsidian-push.sh <file-or-dir-relative-to-workspace>" >&2
    echo "  e.g. obsidian-push.sh drafts/foo.md" >&2
    echo "  e.g. obsidian-push.sh drafts" >&2
    echo "  detected workspace: $WS" >&2
    exit 1
fi

target="$1"
# Strip leading ./ or workspace prefix if given
target="${target#./}"
target="${target#$WS/}"

full="$WS/$target"

if [ -f "$full" ]; then
    # Single file push
    vault_path=$(map_to_vault "$target")
    if [ -z "$vault_path" ]; then
        echo "ERROR: $target is not in a syncable folder (drafts/, docs/, reports/, archive/, memory/projects/)" >&2
        exit 1
    fi
    dest="$BUCKET/$vault_path"
    echo "PUSH: $target → $dest"
    aws s3 cp "$full" "$dest" $EP --only-show-errors
    echo "PUSH_OK: $target"
elif [ -d "$full" ]; then
    # Directory push — sync the whole tree, only .md
    vault_prefix=$(map_to_vault "$target/")
    if [ -z "$vault_prefix" ]; then
        echo "ERROR: $target is not a syncable folder" >&2
        exit 1
    fi
    # Strip trailing slash for aws s3 sync (it treats presence/absence differently)
    vault_prefix="${vault_prefix%/}"
    dest="$BUCKET/$vault_prefix"
    echo "PUSH: $target/ → $dest/"
    aws s3 sync "$full/" "$dest/" $EP \
        --exclude "*" --include "*.md" \
        --only-show-errors
    echo "PUSH_OK: $target/"
else
    echo "ERROR: $target does not exist under $WS" >&2
    exit 1
fi
