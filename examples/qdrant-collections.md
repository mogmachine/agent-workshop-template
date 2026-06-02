# Qdrant collection setup

Five collections total. One for the workspace corpus, four for the learning loop.

All use **768-dim cosine** because that's what `nomic-embed-text` produces.

## One-shot bootstrap

After Qdrant is up on `127.0.0.1:6333`:

```bash
for COLL in workspace-corpus corrections experiences preferences principles; do
  curl -sS -X PUT "http://127.0.0.1:6333/collections/$COLL" \
    -H 'Content-Type: application/json' \
    -d '{
      "vectors": { "size": 768, "distance": "Cosine" },
      "hnsw_config": { "m": 16, "ef_construct": 200 },
      "optimizers_config": { "default_segment_number": 2 }
    }'
  echo
done
```

Verify:

```bash
curl -sS http://127.0.0.1:6333/collections | jq .
```

## Per-collection notes

| Collection | Written by | Read by | Payload fields (in addition to `stored_at`, `agent`) |
|---|---|---|---|
| `workspace-corpus` | `mine-workspace.py` | `recall.py` | `source_file`, `chunk_index`, `wing`, `text` |
| `corrections` | `learning.py correction …` | `learning.py check …` | `proposed`, `corrected`, `reason`, `domain` |
| `experiences` | `learning.py experience …` | `learning.py check …` | `problem`, `approach`, `outcome`, `lesson`, `domain` |
| `preferences` | `learning.py preference …` | `learning.py check …` | `text`, `domain` |
| `principles` | `learning.py principle …` | `learning.py check …` | `text`, `domain` |

## Rebuild

Full re-mine of the workspace into `workspace-corpus`:

```bash
python3 scripts/mine-workspace.py --rebuild
```

Stats:

```bash
python3 scripts/mine-workspace.py --stats
```

Drop and recreate a collection (you will lose stored learning):

```bash
curl -sS -X DELETE http://127.0.0.1:6333/collections/corrections
# then re-run the bootstrap loop for that collection
```
