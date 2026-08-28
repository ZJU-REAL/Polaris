"""Cross-discipline query channels preserve user limits and auditable provenance."""

from app.services.interdisciplinary_retrieval import build_query_matrix, rerank_interdisciplinary
from app.services.literature.discovery_ranking import RankedCandidate


def test_query_matrix_has_domain_and_bridge_channels():
    rows = build_query_matrix(
        topic="dynamic impact response",
        primary_domain="Structural engineering",
        related_domains=["Computer vision", "Data science"],
        keywords=["segmentation"],
    )

    assert [row["role"] for row in rows].count("primary") == 1
    assert [row["role"] for row in rows].count("bridge") == 2
    assert all("dynamic impact response" in row["query"] for row in rows)
    assert any("Computer vision" in row["query"] for row in rows)


def test_bridge_evidence_reranks_without_discarding_base_quality():
    base = [
        RankedCandidate(
            identity="single",
            candidate={"metadata": {"retrieval_hits": [{"role": "primary", "discipline": "A"}]}},
            score=0.9,
            tier="core",
            dimensions={"relevance": 0.9},
            reasons=("high relevance",),
        ),
        RankedCandidate(
            identity="bridge",
            candidate={"retrieval_hits": [{"role": "bridge", "discipline": "A + B"}]},
            score=0.85,
            tier="supporting",
            dimensions={"relevance": 0.85},
            reasons=("relevant",),
        ),
    ]

    ranked = rerank_interdisciplinary(
        base, query_plan={"interdisciplinary": {"profile_id": "p"}}, limit=2
    )

    assert ranked[0].identity == "bridge"
    assert ranked[0].dimensions["interdisciplinary_bridge"] == 1.0
    assert ranked[0].tier == "core"
