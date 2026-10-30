"""Ingestion, chunking, retrieval, caching, rate limiting, feedback (D2, L1, D7, L6)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ecs import cache, retrieval
from ecs.ingestion.chunking import Chunk, clause_chunks, extract_general_condition, pdf_chunks
from ecs.ingestion.ingest import ingest
from tests.conftest import FIXTURE_CORPUS, valid_request


def test_clause_chunks_skip_placeholders():
    chunks = clause_chunks([
        {"clause_id": "3(b)", "type": "schedule_row", "doc_id": "d", "text": "Row text"},
        {"clause_id": "8(a)", "type": "schedule_row", "doc_id": "d", "text": "PASTE EXACT TEXT HERE"},
    ])
    assert [c.clause_id for c in chunks] == ["3(b)"]


def test_chunk_id_is_content_hash():
    a = Chunk("d", "same text", "paragraph")
    b = Chunk("d", "same text", "paragraph")
    c = Chunk("d", "other text", "paragraph")
    assert a.id == b.id != c.id


def test_general_condition_extracted_from_pdf_text():
    pages = ["SCHEDULE\n1 Mining ...\n", "General Condition (GC):\nAny project in Category B will be treated as "
             "Category A if located within 10 km of a protected area.\n\nSpecific Condition (SC):\nOther text."]
    gc = extract_general_condition(pages)
    assert gc and "10 km" in gc[0] and "Specific" not in gc[0]
    chunks = pdf_chunks("doc", "Doc", pages)
    assert chunks[0].type == "modifier" and chunks[0].clause_id == "GC"


def test_ingest_is_idempotent(indexed):
    assert indexed["chunks"] == 3 and indexed["added"] == 3
    again = ingest(FIXTURE_CORPUS)
    assert again["added"] == 0 and again["unchanged"] == 3 and again["deleted"] == 0
    assert again["fingerprint"] == indexed["fingerprint"]


def test_reembeds_only_changed_chunks(indexed, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "raw").mkdir()
    (corpus / "manifest.yaml").write_text((FIXTURE_CORPUS / "manifest.yaml").read_text())
    text = (FIXTURE_CORPUS / "clauses.yaml").read_text().replace("below 500 MW.", "below 500 MW (edited).")
    (corpus / "clauses.yaml").write_text(text)
    out = ingest(corpus)
    assert out["added"] == 1 and out["deleted"] == 1 and out["unchanged"] == 2


def test_get_clause_and_modifier_search(indexed):
    rows = retrieval.get_clause("3(b)")
    assert rows and rows[0]["clause_id"] == "3(b)" and rows[0]["type"] == "schedule_row"
    mods = retrieval.search("cement plant", k=3, type_filter="modifier")
    assert mods and all(p["type"] == "modifier" for p in mods)


def test_run_cache_only_stores_verified(redis_client):
    key = cache.run_cache_key({"sector": "cement"}, "v2", "idx1")
    assert not cache.put_run(redis_client, key, {"route": "escalate_to_human", "verified": False})
    assert cache.get_run(redis_client, key) is None
    assert cache.put_run(redis_client, key, {"route": "category_b_state", "verified": True})
    assert cache.get_run(redis_client, key)["route"] == "category_b_state"
    assert key != cache.run_cache_key({"sector": "cement"}, "v2", "idx2")  # new index invalidates


def test_rate_limit(redis_client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    from ecs.api.main import app

    with TestClient(app) as c:
        codes = [c.post("/v1/screenings", json=valid_request(), headers={"X-Client-Id": "loop"}).status_code
                 for _ in range(3)]
        assert codes == [202, 202, 429]
        assert c.post("/v1/screenings", json=valid_request(), headers={"X-Client-Id": "other"}).status_code == 202


def test_feedback_round_trip(redis_client):
    from ecs import runstore
    from ecs.api.main import app

    rec = runstore.create_run("run_x", {"sector": "cement"}, "c", "v2")
    rec.update(status="done", result={"route": "category_b_state"})
    runstore.save_run(rec)
    with TestClient(app) as c:
        assert c.post("/v1/screenings/run_x/feedback", json={"satisfactory": False, "reason": "missed the GC"}
                      ).status_code == 201
        s = c.get("/v1/feedback/summary").json()
        assert s["total"] == 1 and s["share_satisfactory"] == 0.0
        assert s["recent_unsatisfactory"][0]["reason"] == "missed the GC"
        assert c.post("/v1/screenings/nope/feedback", json={"satisfactory": True, "reason": "ok!"}).status_code == 404


def test_live_quality_sampling(redis_client):
    from ecs import live_quality

    rec = {"run_id": "run_q", "passages": {"p1": {}}, "steps": [
        {"action": "final", "draft": {"citations": ["p1"]}}],
        "result": {"route": "category_b_state", "basis": "Cement capacity below one million tonnes is Category B.",
                   "citations": [{"text": "Category B: cement capacity below 1.0 million tonnes per annum."}]}}
    point = live_quality.maybe_score(redis_client, rec, rng=lambda: 0.0)
    assert point["hallucination"] == 0
    assert json.loads(redis_client.get(live_quality.EWMA_KEY))["groundedness"] == point["groundedness"]
