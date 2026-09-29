"""taxonomy_additions：人工补充场景叶的合并规则（原评测 xlsx 不动）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labeler.taxonomy import Taxonomy, _merge_additions


def _write(tmp_path: Path, leaves: list[dict]) -> Path:
    p = tmp_path / "taxonomy_additions.json"
    p.write_text(json.dumps({"leaves": leaves}, ensure_ascii=False), encoding="utf-8")
    return p


def test_merge_appends_new_leaves(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = _write(tmp_path, [{"id": "S220", "l1": "个人成长与生活", "l2": "自我成长",
                           "l3": "兴趣爱好培养", "definition": "爱好的选择与坚持"}])
    merged = _merge_additions(taxonomy, p)
    assert len(merged.scenarios) == len(taxonomy.scenarios) + 1
    s = merged.scenario_by_id("S220")
    assert s is not None
    assert s.path == "个人成长与生活 > 自我成长 > 兴趣爱好培养"   # path 省略时自动拼
    assert merged.intents == taxonomy.intents                     # 意图/画像不动
    assert merged.persona_fields == taxonomy.persona_fields
    assert "S220" in merged.scenario_ids()                        # 导入校验/速查表能识别


def test_merge_rejects_duplicate_id(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = _write(tmp_path, [{"id": "S001", "l1": "x", "l2": "y", "l3": "z"}])
    with pytest.raises(ValueError, match="S001"):
        _merge_additions(taxonomy, p)


def test_merge_rejects_incomplete_leaf(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = _write(tmp_path, [{"id": "S222", "l1": "个人成长与生活"}])
    with pytest.raises(ValueError, match="缺 id/l1/l3"):
        _merge_additions(taxonomy, p)


def test_merge_empty_is_noop(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = _write(tmp_path, [])
    assert _merge_additions(taxonomy, p) is taxonomy


def test_rename_changes_path_keeps_id_and_order(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = tmp_path / "taxonomy_additions.json"
    p.write_text(json.dumps({"renames": [{"id": "S004", "l3": "在校阶段规划"}]},
                            ensure_ascii=False), encoding="utf-8")
    merged = _merge_additions(taxonomy, p)
    assert len(merged.scenarios) == len(taxonomy.scenarios)   # 只改名不加条
    s = merged.scenario_by_id("S004")
    old = taxonomy.scenario_by_id("S004")
    assert s is not None and old is not None
    assert s.l3 == "在校阶段规划" and s.path == "个人成长与生活 > 执行与习惯 > 在校阶段规划"
    assert s.definition == old.definition                     # 没写的字段保持原值
    assert [x.id for x in merged.scenarios] == [x.id for x in taxonomy.scenarios]


def test_rename_unknown_id_raises(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = tmp_path / "taxonomy_additions.json"
    p.write_text(json.dumps({"renames": [{"id": "S999", "l3": "x"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="S999"):
        _merge_additions(taxonomy, p)


# ── updates / metadata / deletes（taxonomy_sync 写出的三种键）────────────

def test_update_patches_fields_and_rebuilds_path(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = tmp_path / "taxonomy_additions.json"
    p.write_text(json.dumps({"updates": {"S003": {"l2": "职业方向"}}}, ensure_ascii=False),
                 encoding="utf-8")
    merged = _merge_additions(taxonomy, p)
    s = merged.scenario_by_id("S003")
    assert s is not None
    assert s.l2 == "职业方向" and s.path == "就业 > 职业方向 > 网申"  # path 一律重拼
    assert s.definition == "央企网申流程"                              # 没写的字段保持
    assert [x.id for x in merged.scenarios] == [x.id for x in taxonomy.scenarios]


def test_update_can_patch_addition_leaf(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = tmp_path / "taxonomy_additions.json"
    p.write_text(json.dumps({
        "leaves": [{"id": "S220", "l1": "个人成长与生活", "l2": "自我成长",
                    "l3": "兴趣爱好培养", "definition": "爱好"}],
        "updates": {"S220": {"l3": "兴趣培养", "definition": "爱好的选择"}},
    }, ensure_ascii=False), encoding="utf-8")
    merged = _merge_additions(taxonomy, p)
    s = merged.scenario_by_id("S220")
    assert s is not None
    assert s.path == "个人成长与生活 > 自我成长 > 兴趣培养" and s.definition == "爱好的选择"


def test_update_unknown_id_or_field_raises(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = tmp_path / "taxonomy_additions.json"
    p.write_text(json.dumps({"updates": {"S999": {"l3": "x"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="S999"):
        _merge_additions(taxonomy, p)
    p.write_text(json.dumps({"updates": {"S001": {"foo": "x"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="未知字段"):
        _merge_additions(taxonomy, p)


def test_metadata_sets_kb_and_timing_only(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = tmp_path / "taxonomy_additions.json"
    p.write_text(json.dumps({"metadata": {"S001": {"kb_required": "1", "timing": "二阶段"}}},
                            ensure_ascii=False), encoding="utf-8")
    merged = _merge_additions(taxonomy, p)
    s = merged.scenario_by_id("S001")
    assert s is not None
    assert s.kb_required == "1" and s.timing == "二阶段"
    assert s.definition == "考研与就业等路径选择"        # 定义等字段不受影响
    p.write_text(json.dumps({"metadata": {"S001": {"foo": "x"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="未知字段"):
        _merge_additions(taxonomy, p)


def test_deletes_remove_and_keep_order(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = tmp_path / "taxonomy_additions.json"
    p.write_text(json.dumps({"deletes": ["S002", "S003"]}, ensure_ascii=False),
                 encoding="utf-8")
    merged = _merge_additions(taxonomy, p)
    assert [x.id for x in merged.scenarios] == ["S001", "S004"]   # 保序删除
    p.write_text(json.dumps({"deletes": ["S999"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="S999"):
        _merge_additions(taxonomy, p)
