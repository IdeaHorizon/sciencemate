"""Regression tests for topic-safe automatic KB context injection."""
from __future__ import annotations

import numpy as np

from core.context_engine import _search_kb_for_context


class _FakeState:
    project_id = "current-project"

    def __init__(self) -> None:
        self._records = {
            "claims": [
                {
                    "id": "claim_relevant",
                    "claim_text": "SOAP descriptor rank deficiency",
                    "claim_type": "empirical",
                    "status": "validated",
                },
                {
                    "id": "claim_unrelated",
                    "claim_text": "Lennard-Jones thermostat convergence",
                    "claim_type": "dead_end",
                    "status": "validated",
                },
            ],
            "concepts": [],
        }

    def list_kb(self, entity: str):
        return list(self._records.get(entity, []))


class _FakeEmbeddings:
    def embed(self, texts):
        return np.ones((len(texts), 4), dtype=np.float32)


def test_semantic_context_never_backfills_with_unmatched_org_claims(monkeypatch):
    """Fewer than cap semantic hits must stay fewer than cap, not add noise."""
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "0")
    monkeypatch.setattr(
        "core.embeddings.get_default_embedding_client", lambda: _FakeEmbeddings()
    )
    monkeypatch.setattr("core.kb_vector_index.should_rebuild", lambda *a, **k: False)

    def _query(entity, qvec, **kwargs):
        if entity == "claims":
            return [("claim_relevant", 0.83, "project")]
        return []

    monkeypatch.setattr("core.kb_vector_index.query_across_scopes", _query)
    result = _search_kb_for_context(_FakeState(), "SOAP descriptor completeness")
    assert [item["id"] for item in result["claims"]] == ["claim_relevant"]
    assert "claim_unrelated" not in {item["id"] for item in result["claims"]}
