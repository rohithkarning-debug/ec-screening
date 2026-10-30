# Retrieval, chunking and caching (L1, D2, D7)

## The difficulty (brief section 3)

The Schedule is a table of thresholds, and the table is not the whole rule.
The General Condition, printed after the table, changes the category of
some projects by location, and no Schedule row repeats it. A search for
"cement plant 0.5 MTPA" returns the cement row and nothing about protected
areas.

## Strategy

| Source | Chunking | Metadata |
| --- | --- | --- |
| Schedule rows | One chunk per row, copied verbatim into `corpus/clauses.yaml` (PDF extraction scrambles tables) | `type=schedule_row`, `clause_id` e.g. `3(b)`, page, sector |
| General Condition | Its own chunk, from `clauses.yaml`; also auto-extracted from any PDF section headed "General Condition" | `type=modifier`, `clause_id=GC` |
| Amendments and office memoranda | Paragraph chunks packed to about 900 characters with 150 characters of overlap; long paragraphs split at line and sentence ends | `type=amendment` when the text amends something, with the item it names; else `paragraph`; S.O. number, date, page |

How this answers the difficulty:

1. The GC is a separate, typed unit, so the agent can ask for it directly
   (`get_clause("GC")`) or search only modifiers (`search_corpus(type="modifier")`).
   It never depends on similarity to the project description.
2. Schedule rows are whole, so thresholds for A and B are never split.
3. The verifier enforces the lookup: a Category B draft with no modifier
   retrieved fails verification (`modifier_not_checked`).

Rejected: fixed 512-token windows (splits rows, buries the GC with the last
rows of the table), whole-page chunks (too coarse to cite), and relying on
retrieval alone without a typed modifier (the failure section 5 describes).

## Embeddings and index

all-MiniLM-L6-v2 (384 dimensions), run locally through ONNX Runtime. Chroma
server with cosine distance, accessed through LangChain's `Chroma` vector store.

## Idempotent pipeline and re-embedding

`python -m ecs.ingestion.ingest` (the `ingest` service runs it on every `up`):

- chunk ID = `doc_id:clause_id:sha256(text)[:16]`;
- adds only IDs not in the index, deletes IDs no longer produced;
- an unchanged corpus embeds nothing; editing one clause re-embeds one chunk.

Freshness (build time, document hashes, chunk count, clause IDs) and
throughput (chunks/s) are stored in Redis `index:meta`, served at
`GET /v1/index`, and exported as `ecs_index_*` metrics. The index fingerprint
is part of every cache key, so re-ingestion invalidates caches.

## Caching (D7)

| Layer | Key | Why |
| --- | --- | --- |
| Whole run | request (without name) + prompt version + index fingerprint | The same project against the same corpus and prompt gets the same opinion; a hit costs no model calls. Only verified results are stored. |
| Retrieval | query + k + type + index fingerprint | Agents repeat queries such as "General Condition protected area" |
| Query embedding | in-process LRU (2048) | Saves the ONNX call for repeated queries |

Measure the effect on p95 and cost per run with Locust (`loadtest/RESULTS.md`).
