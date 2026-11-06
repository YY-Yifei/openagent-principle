"""存储：原子写、版本、归档、案例与反馈。"""

from __future__ import annotations

import json

import pytest

from openagent_principle.models import Judgement, Principle, Verdict
from openagent_principle.store import PrincipleStore, StoreError, seed_principles


def _judgement(case_id="C-20260101-000000-abcd", principle_id="P-001", violated=True):
    return Judgement(
        case_id=case_id,
        subject="周报.md",
        material_chars=10,
        overall="violation" if violated else "pass",
        score=0.5,
        severity=4,
        verdicts=[Verdict(principle_id, "t", violated, severity=4)],
        backend="jev",
        created_at="2026-01-01T00:00:00+08:00",
    )


def test_save_assigns_ids_in_order(store, principle):
    first = store.save(principle())
    second = store.save(principle(id="", title="另一条", statement="s"))
    assert first.id == "P-001"
    assert second.id == "P-002"


def test_save_update_bumps_version_and_keeps_created_at(store, principle):
    created = store.save(principle())
    created_at = created.created_at
    updated = store.save(principle(statement="改过的陈述"))
    assert updated.version == 2
    assert updated.created_at == created_at
    assert store.get("P-001").statement == "改过的陈述"


def test_get_is_case_insensitive(store, principle):
    store.save(principle())
    assert store.get("p-001") is not None


def test_delete_archives_by_default(store, principle):
    store.save(principle())
    assert store.delete("P-001") is True
    archived = store.get("P-001")
    assert archived.archived is True
    assert store.all() == []  # 归档的不出现在默认列表里
    assert len(store.all(include_archived=True)) == 1


def test_hard_delete_removes(store, principle):
    store.save(principle())
    assert store.delete("P-001", hard=True) is True
    assert store.get("P-001") is None


def test_delete_missing_returns_false(store):
    assert store.delete("P-999") is False


def test_search_matches_text_and_tags(store, principle):
    store.save(principle())
    store.save(principle(id="", title="目标先行", statement="先写目标", tags=["目标"]))
    assert [p.title for p in store.search(query="目标")] == ["目标先行"]
    assert [p.id for p in store.search(tag="风险")] == ["P-001"]
    assert store.search(query="不存在的词") == []


def test_stats_counts(store, principle):
    store.save(principle())
    store.save(principle(id="", title="另一条", statement="s", severity=2))
    store.append_case(_judgement())
    stats = store.stats()
    assert stats["active"] == 2
    assert stats["cases"] == 1
    assert stats["by_severity"] == {2: 1, 4: 1}


def test_cases_are_append_only_and_filterable(store):
    store.append_case(_judgement())
    store.append_case(_judgement(case_id="C-2", principle_id="P-002", violated=False))
    assert len(store.list_cases()) == 2
    assert [c.case_id for c in store.list_cases(principle_id="P-002")] == ["C-2"]
    assert store.get_case("C-2").overall == "pass"
    assert store.get_case("C-404") is None


def test_feedback_and_summary(store):
    store.append_feedback("C-1", agree=False, principle_id="P-001", note="判太宽")
    store.append_feedback("C-2", agree=True, principle_id="P-001")
    store.append_feedback("C-3", agree=False)
    rows = store.feedback_summary()
    by_id = {row["principle_id"]: row for row in rows}
    assert by_id["P-001"]["total"] == 2
    assert by_id["P-001"]["disagree"] == 1
    assert by_id["P-001"]["rate"] == pytest.approx(0.5)
    assert by_id["(整体)"]["rate"] == pytest.approx(1.0)


def test_corrupt_json_reports_instead_of_resetting(store, principle):
    store.ensure()
    store.principles_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        store.load()


def test_write_is_atomic_and_leaves_no_temp_files(store, principle):
    store.save(principle())
    leftovers = [p.name for p in store.home.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    data = json.loads(store.principles_path.read_text(encoding="utf-8"))
    assert data["schema"] == 1 and len(data["principles"]) == 1


def test_seed_file_matches_code():
    """data/principles.seed.json 是镜像，store.seed_principles() 是唯一真源。"""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "data" / "principles.seed.json"
    mirror = json.loads(path.read_text(encoding="utf-8"))
    assert mirror == seed_principles()


def test_seed_principles_are_judgeable():
    for data in seed_principles():
        principle = Principle.from_dict(data)
        principle.validate()
        assert principle.criteria
