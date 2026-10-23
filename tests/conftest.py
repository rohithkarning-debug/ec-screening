"""Shared test fixtures: environment, fake Redis, in-memory Chroma, fixture corpus.

The tests need no Docker, no network and no model. The corpus used here is a
SYNTHETIC fixture (tests/fixtures/corpus), not the EIA Notification text.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import chromadb
import fakeredis
import pytest

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://unused:6379/0")
    monkeypatch.setenv("CHROMA_HOST", "unused")
    monkeypatch.setenv("CHROMA_PORT", "1")
    monkeypatch.setenv("CHROMA_COLLECTION", f"test_{uuid.uuid4().hex[:8]}")
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://gateway.test")
    monkeypatch.setenv("VERIFIER_URL", "http://verifier.test")
    monkeypatch.setenv("LLM_PROVIDER_BASE_URL", "http://provider.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1000")
    monkeypatch.setenv("LIVE_SCORE_SAMPLE_RATE", "1.0")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    yield


@pytest.fixture
def redis_client():
    from ecs import runstore

    r = fakeredis.FakeRedis(decode_responses=True)
    runstore.set_redis(r)
    yield r
    r.flushall()


@pytest.fixture
def chroma():
    from ecs import retrieval

    client = chromadb.EphemeralClient()
    retrieval.set_client(client)
    yield client


@pytest.fixture
def indexed(redis_client, chroma):
    """Fixture corpus ingested into the in-memory index."""
    from ecs.ingestion.ingest import ingest

    return ingest(FIXTURE_CORPUS)


def valid_request(**over) -> dict:
    body = {
        "project_name": "Test cement unit",
        "sector": "cement",
        "description": "Standalone cement grinding unit.",
        "capacity": {"value": 0.5, "unit": "MTPA"},
        "location": {"state": "Karnataka", "district": "Kalaburagi"},
        "protected_area_distance_km": 2,
        "protected_area_name": "Test National Park",
    }
    body.update(over)
    return body
