# Agent Workshop Template

> The reference setup behind the Proof of Talk workshop — **Paris · 2 June 2026**.
> One operator. One agent. Memory that compounds.

This repository is the public, code-bearing companion to the workshop handout
[*How to replicate my agent setup*](https://s3.hippius.com/poppy/share/proof-of-talk/replicate-my-agent-setup.html).
The handout explains the four-layer architecture in plain English; this repo
gives you the actual scripts.

---

## What's in here

```
scripts/
  obsidian-sync.sh       hourly push: workspace → S3 vault, with conflict detection
  obsidian-push.sh       immediate single-file push to the vault
  mine-workspace.py      walk workspace + embed + upsert into Qdrant
  recall.py              composite retrieval (sim + recency + frequency + specificity)
  learning.py            log corrections / experiences / preferences / principles + check before sensitive actions
  validate.py            gate task completion behind explicit evidence checks
templates/               starter MEMORY.md, SOUL.md, USER.md, IDENTITY.md, TODO.md
examples/
  systemd/qdrant.service systemd unit for a single-node Qdrant
  cron/obsidian-sync.cron hourly cron line
  qdrant-collections.md  one-shot bootstrap for the five Qdrant collections
.env.example             every env var these scripts read
```

No secrets, no production data, no inbound network surfaces. Each script is
dependency-light: Python standard library + `boto3`/`aws-cli` for S3 +
`requests`-less Qdrant calls over `urllib`.

---

## Quickstart (a working setup in one weekend)

### 1. Box + OpenClaw

Anywhere with SSH and Python 3.10+. DigitalOcean (~$12/mo) for anything
sensitive; Targon if you genuinely don't care about isolation.

```bash
curl -fsSL https://openclaw.ai/install.sh | bash
openclaw doctor   # all green before continuing
```

### 2. Clone this repo

```bash
cd /opt
sudo git clone https://github.com/taostat/agent-workshop-template.git
cd agent-workshop-template
cp .env.example .env
$EDITOR .env   # fill in S3_*, QDRANT_URL, WORKSPACE
```

### 3. Layer 1 — Identity

```bash
mkdir -p "$WORKSPACE"
for f in MEMORY.md SOUL.md USER.md IDENTITY.md TODO.md; do
  cp templates/$f "$WORKSPACE/$f"
done
$EDITOR "$WORKSPACE/MEMORY.md"   # …then SOUL, USER, IDENTITY, TODO
```

Two paragraphs each is enough on day one. You'll iterate.

### 4. Layer 2 — S3 vault + Obsidian

You need an S3-compatible bucket. Two reasonable choices:

- **Hippius S3** (`s3.hippius.com`) — decentralised, your memory isn't on AWS.
- **AWS S3 / Cloudflare R2 / Backblaze B2** — pick what you already use.

Create a bucket-scoped IAM key (read+write on this bucket only), put the
access/secret pair in `.env`, then:

```bash
./scripts/obsidian-sync.sh --dry-run   # see what would be pushed
./scripts/obsidian-sync.sh             # actually push
```

Set Obsidian's [Remotely Save](https://github.com/remotely-save/remotely-save)
plugin to the same bucket + prefix. The vault on your laptop now mirrors what
the agent sees on disk.

Schedule the hourly sync (example in `examples/cron/obsidian-sync.cron`).

### 5. Layer 3 — Qdrant + workspace corpus

```bash
# Install Qdrant (binary release):
curl -L https://github.com/qdrant/qdrant/releases/latest/download/qdrant-x86_64-unknown-linux-gnu.tar.gz \
  | sudo tar xz -C /usr/local/bin
sudo cp examples/systemd/qdrant.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now qdrant

# Install Ollama and pull the embedding model:
curl -fsSL https://ollama.com/install.sh | sh
ollama pull nomic-embed-text

# Create the 5 collections (workspace-corpus + 4 learning collections):
bash examples/qdrant-collections.md   # the one-shot loop is at the top
# (or just paste the curl loop into your shell)

# First mine:
python3 scripts/mine-workspace.py --rebuild
python3 scripts/mine-workspace.py --stats
```

### 6. Layer 4 — Wire the learning loop into the agent

Add to your agent's per-turn behaviour (system prompt / preflight script):

- Before any sensitive action, run `scripts/learning.py check "<one-line plan>"` and adapt to any returned corrections.
- When the operator corrects you, run `scripts/learning.py correction <agent> --proposed "..." --corrected "..." --reason "..." --domain "..."` immediately.
- Before claiming a task done, run `scripts/validate.py gate --task "..." --evidence "check1|check2|..."`.

Test:

```bash
./scripts/learning.py correction orchestrator \
  --proposed "I'll deploy directly to prod" \
  --corrected "Always dry-run first, then ask" \
  --reason "Operator said so on day one" \
  --domain "deploy"

./scripts/learning.py check "I'm about to deploy to prod"
# → should surface the correction above with a high score
```

---

## Composite retrieval — why pure similarity isn't enough

`recall.py` ranks results with

```
final_score = α·similarity + β·recency + γ·frequency + δ·specificity
```

Weight profiles live near the top of `recall.py`. The defaults are:

- `orchestrator` — always-on personal-assistant agent (similarity-biased, with recency overrides per chat domain)
- `peer` — team-facing peer agent in a chat channel (more recency)
- `research` — long-form research agent (more specificity, less recency)

Add your own under `PROFILES = { … }`. Per-domain overrides nest under
`"domains": { … }`. Half-lives are days.

Every retrieval writes a one-line JSON record to `$WORKSPACE/state/recall-audit.ndjson`.
When a rule is being missed, the audit tells you exactly which candidates
lost the score race.

---

## The host rule

Choose your server by **data sensitivity**, not convenience.

| Sensitivity | Host | Why |
|---|---|---|
| Sensitive / persistent | DigitalOcean (or any full VM) | Full VM isolation, egress firewall control |
| Throwaway / public-facing | Targon (Bittensor SN4) | 3-minute provisioning, shared kernel — fine when nothing private touches the box |

---

## What's NOT in this repo (yet)

- A turn-key OpenClaw config that wires the three reliability scripts into your agent's per-turn loop. You'll need to add `learning.py check` + `validate.py gate` invocations to your own agent prompt or per-turn hook. Example wiring is in the workshop handout.
- A web UI for browsing Qdrant collections. Use the Qdrant dashboard at `http://127.0.0.1:6333/dashboard`.

## Why ours, not third-party

Mem0, Letta, Zep, Mempalace — each pulled in nice ideas. None survived in
production. When a third-party CLI has a bug, it's between you and your
agent's memory. **Hybrid, ours**: pull the concepts, write the implementation
yourself. The whole stack here is ~2,100 lines.

## License

Apache 2.0. Take it. Fork it. Run it. Tell us what you built.

## Links

- Workshop handout (HTML): https://s3.hippius.com/poppy/share/proof-of-talk/replicate-my-agent-setup.html
- Speaker pathway (HTML): https://s3.hippius.com/poppy/share/proof-of-talk/05-speaker-pathway-v7.html
- Slides (HTML): https://s3.hippius.com/poppy/share/proof-of-talk/04-slides-v7.html
- OpenClaw: https://openclaw.ai · https://github.com/openclaw/openclaw
- Hippius S3: https://s3.hippius.com · https://docs.hippius.com
