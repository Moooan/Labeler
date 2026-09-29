"""taxonomy 解析测试：sheet 结构 → 标签对象。"""

from __future__ import annotations

from pathlib import Path

from conftest import build_taxonomy_xlsx

from labeler.taxonomy import load_taxonomy


def test_load_taxonomy_counts(tmp_path: Path) -> None:
    xlsx = tmp_path / "tax.xlsx"
    build_taxonomy_xlsx(xlsx)
    tax = load_taxonomy(xlsx, additions_path=None)
    assert len(tax.scenarios) == 4
    assert len(tax.intents) == 3
    assert len(tax.persona_fields) == 4


def test_scenario_fields(tmp_path: Path) -> None:
    xlsx = tmp_path / "tax.xlsx"
    build_taxonomy_xlsx(xlsx)
    tax = load_taxonomy(xlsx, additions_path=None)
    s = tax.scenario_by_id("S003")
    assert s is not None
    assert s.l1 == "就业"
    assert s.l2 == "国央企"
    assert s.path == "就业 > 国央企 > 网申"


def test_persona_groups_and_sensitivity(tmp_path: Path) -> None:
    xlsx = tmp_path / "tax.xlsx"
    build_taxonomy_xlsx(xlsx)
    tax = load_taxonomy(xlsx, additions_path=None)
    names = tax.persona_field_names()
    assert {"年龄段", "年级", "学校名称", "求职阶段"} <= names
    sens = {f.name: f.sensitivity for f in tax.persona_fields}
    assert sens["年级"] == "一般"
