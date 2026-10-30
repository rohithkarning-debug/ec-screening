"""Idempotent corpus ingestion: read -> parse -> chunk -> embed -> index (D2).

    python -m ecs.ingestion.ingest                # incremental (default)
    python -m ecs.ingestion.ingest --rebuild      # drop and rebuild the collection
    python -m ecs.ingestion.ingest --dry-run      # parse and chunk only

Idempotency: chunk IDs are content hashes. On each run we compute the full
set of desired chunk IDs, embed and add only the IDs that are missing, and
delete IDs that no longer exist. Re-running with an unchanged corpus embeds
nothing. Changing one document re-embeds only that document's changed chunks
(L1: "re-embedding without rebuilding the whole index").

Freshness (when, from which versions, how many chunks) and throughput are
written to Redis under `index:meta`; the API exports them as Prometheus
metrics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

import yaml

from ecs import retrieval
from ecs.config import settings
from ecs.ingestion.chunking import Chunk, clause_chunks, pdf_chunks
from ecs.logging_setup import log, setup_logging

logger = logging.getLogger("ingest")
BATCH = 64


def read_pdf(path: Path) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return [(p.extract_text() or "") for p in reader.pages]


def load_corpus(corpus_dir: Path) -> tuple[list[Chunk], list[dict]]:
    manifest = yaml.safe_load((corpus_dir / "manifest.yaml").read_text()) or {}
    docs = manifest.get("documents") or []
    chunks: list[Chunk] = []
    versions = []

    clauses_file = corpus_dir / "clauses.yaml"
    if clauses_file.exists():
        clauses = yaml.safe_load(clauses_file.read_text()) or []
        cc = clause_chunks(clauses)
        chunks.extend(cc)
        versions.append({"doc_id": "clauses.yaml", "sha256": _sha(clauses_file), "chunks": len(cc)})

    for d in docs:
        path = corpus_dir / "raw" / d["file"]
        if not path.exists():
            log(logger, logging.WARNING, "document listed in manifest is missing", file=d["file"])
            continue
        pages = read_pdf(path)
        if sum(len(p.strip()) for p in pages) < 200:
            log(logger, logging.WARNING, "no text layer; OCR required before indexing", file=d["file"])
            continue
        dc = pdf_chunks(d["doc_id"], d.get("title", d["doc_id"]), pages)
        for c in dc:
            c.extra.update({"so_number": d.get("so_number"), "doc_date": str(d.get("date") or "")})
        chunks.extend(dc)
        versions.append({"doc_id": d["doc_id"], "sha256": _sha(path), "chunks": len(dc),
                         "date": str(d.get("date") or ""), "so_number": d.get("so_number")})
    return chunks, versions


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def ingest(corpus_dir: Path, rebuild: bool = False, dry_run: bool = False) -> dict:
    t0 = time.time()
    chunks, versions = load_corpus(corpus_dir)
    desired = {c.id: c for c in chunks}
    log(logger, logging.INFO, "corpus parsed", chunks=len(desired), documents=len(versions))
    if dry_run:
        return {"chunks": len(desired), "documents": versions, "dry_run": True}

    client = retrieval.chroma_client()
    if rebuild:
        try:
            client.delete_collection(settings.chroma_collection)
        except Exception:
            pass
        retrieval.set_client(client)
    store = retrieval.vector_store()
    coll = client.get_or_create_collection(settings.chroma_collection)
    existing = set(coll.get(include=[])["ids"])

    to_add = [desired[i] for i in desired.keys() - existing]
    to_delete = sorted(existing - desired.keys())
    if to_delete:
        coll.delete(ids=to_delete)
    t_embed = time.time()
    for i in range(0, len(to_add), BATCH):
        batch = to_add[i:i + BATCH]
        store.add_texts(texts=[c.text for c in batch], metadatas=[c.metadata() for c in batch],
                        ids=[c.id for c in batch])
    embed_s = time.time() - t_embed

    fingerprint = hashlib.sha256("|".join(sorted(desired)).encode()).hexdigest()[:16]
    meta = {
        "built_at": time.time(),
        "fingerprint": fingerprint,
        "chunks": len(desired),
        "documents": versions,
        "added": len(to_add),
        "deleted": len(to_delete),
        "unchanged": len(desired) - len(to_add),
        "chunks_per_second": round(len(to_add) / embed_s, 2) if to_add and embed_s > 0 else 0.0,
        "duration_s": round(time.time() - t0, 2),
        "embedding_backend": settings.embedding_backend,
        "clause_ids": sorted({c.clause_id for c in chunks if c.clause_id}),
    }
    from ecs.runstore import get_redis

    get_redis().set(retrieval.INDEX_META_KEY, json.dumps(meta))
    log(logger, logging.INFO, "ingestion complete", **{k: v for k, v in meta.items() if k != "documents"})
    return meta


def main(argv=None) -> int:
    setup_logging("ingest")
    p = argparse.ArgumentParser(description="Build or update the EIA corpus index.")
    p.add_argument("--corpus", default="corpus", help="Corpus directory (manifest.yaml, clauses.yaml, raw/)")
    p.add_argument("--rebuild", action="store_true", help="Drop the collection and rebuild")
    p.add_argument("--dry-run", action="store_true", help="Parse and chunk only")
    a = p.parse_args(argv)
    meta = ingest(Path(a.corpus), rebuild=a.rebuild, dry_run=a.dry_run)
    print(json.dumps({k: v for k, v in meta.items() if k != "documents"}, indent=2))
    if not a.dry_run and meta["chunks"] == 0:
        log(logger, logging.WARNING, "index is empty - every request will escalate. Fill corpus/clauses.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
