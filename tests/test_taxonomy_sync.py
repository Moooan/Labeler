"""taxonomy_sync：CSV 读取 / 差异计算 / additions 构建（纯逻辑，不碰真实数据）。"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from labeler.taxonomy import Taxonomy
from labeler.taxonomy_sync import (
    CSV_COLUMNS,
    Diff,
    build_additions,
    check_references,
    diff_taxonomy,
    read_scenario_csv,
)


def _write_csv(tmp_path: Path, rows: list[tuple[str, ...]]) -> Path:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS + ("", ""))          # Excel 导出常见的尾部空列
    for r in rows:
        w.writerow(list(r) + ["", ""])
    p = tmp_path / "场景.csv"
    p.write_bytes(("\ufeff" + buf.getvalue()).encode("utf-8"))   # 带 BOM
    return p


def test_read_scenario_csv_strips_bom_and_extra_cols(tmp_path: Path) -> None:
    p = _write_csv(tmp_path, [
        ("S001", "升学", "考研", "是否考研", "升学 > 考研 > 是否考研", "路径选择", "1", "二阶段"),
        ("", "空 ID 行跳过", "", "", "", "", "", ""),
    ])
    rows = read_scenario_csv(p)
    assert len(rows) == 1
    assert rows[0]["场景ID"] == "S001"
    assert rows[0]["是否需要知识库"] == "1"
    assert set(rows[0]) == set(CSV_COLUMNS)      # 尾部空列没混进来


def test_diff_taxonomy(tmp_path: Path, taxonomy: Taxonomy) -> None:
    p = _write_csv(tmp_path, [
        ("S001", "升学", "考研", "是否考研", "升学 > 考研 > 是否考研",
         "考研与就业等路径选择", "", ""),
        # l3 改了但完整路径列还是旧值 → changed + stale_paths
        ("S002", "升学", "考研", "备考规划", "升学 > 考研 > 院校选择", "", "1", "二阶段"),
        ("S003", "就业", "国央企", "网申", "就业 > 国央企 > 网申", "央企网申流程", "", ""),
        ("S005", "个人成长与生活", "自我成长", "新叶", "", "新叶定义", "", ""),
    ])   # S004 没出现 → removed
    diff = diff_taxonomy(read_scenario_csv(p), taxonomy)
    assert list(diff.added) == ["S005"]
    assert diff.removed == ["S004"]
    assert diff.changed == {"S002": {"l3": "备考规划"}}       # CSV 留空 = 未改，不算清空
    assert diff.metadata == {"S002": {"kb_required": "1", "timing": "二阶段"}}
    assert diff.stale_paths == ["S002"]


def test_diff_suppresses_fields_matching_proposal(tmp_path: Path, taxonomy: Taxonomy,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """生效值就是拟稿值（有意覆写 CSV 的占位笔记）时不算差异，sync 保持幂等。"""
    import labeler.taxonomy_sync as sync
    monkeypatch.setattr(sync, "PROPOSALS", {"S002": {"l3": "院校选择"}})
    p = _write_csv(tmp_path, [
        ("S001", "升学", "考研", "是否考研", "升学 > 考研 > 是否考研",
         "考研与就业等路径选择", "", ""),
        ("S002", "升学", "考研", "随便什么占位", "升学 > 考研 > 院校选择", "", "", ""),
        ("S003", "就业", "国央企", "网申", "就业 > 国央企 > 网申", "央企网申流程", "", ""),
        ("S004", "个人成长与生活", "执行与习惯", "拖延", "", "拖延与启动困难", "", ""),
    ])
    diff = diff_taxonomy(read_scenario_csv(p), taxonomy)
    assert "S002" not in diff.changed
    assert diff.suppressed == ["S002.l3"]


def _row5() -> dict[str, str]:
    return dict(zip(CSV_COLUMNS, ("S005", "个人成长与生活", "自我成长", "新叶",
                                  "", "新叶定义", "1", "")))


def test_build_additions_merges_renames_and_defers_deletes() -> None:
    current = {
        "renames": [{"id": "S001", "l3": "改名"}],
        "leaves": [{"id": "S006", "l1": "x", "l2": "y", "l3": "z", "definition": "d"}],
    }
    diff = Diff(added={"S005": _row5()}, removed=["S004"],
                changed={"S002": {"l3": "备考规划"}},
                metadata={"S003": {"kb_required": "1"}})
    out = build_additions(current, diff, include_deletes=False)
    assert out["updates"]["S001"] == {"l3": "改名"}                # 旧式 renames 迁进来
    assert out["updates"]["S002"] == {"l3": "备考规划"}
    assert [d["id"] for d in out["leaves"]] == ["S006", "S005"]    # 新叶追加到末尾
    assert out["leaves"][1]["definition"] == "新叶定义"
    assert out["leaves"][1]["kb_required"] == "1"                  # 新叶元数据内嵌
    assert out["metadata"] == {"S003": {"kb_required": "1"}}
    assert "deletes" not in out and "renames" not in out           # 删除缓执行

    out2 = build_additions(current, diff, include_deletes=True)
    assert out2["deletes"] == ["S004"]


def test_build_additions_missing_l3_without_proposal_raises() -> None:
    row = _row5()
    row["三级叶节点"] = ""
    with pytest.raises(SystemExit, match="三级叶节点为空"):
        build_additions({}, Diff(added={"S005": row}), include_deletes=False)


def test_check_references_counts_quoted_ids(tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    a.write_text('{"scenarios": ["S074", "S074"]}', encoding="utf-8")
    b = tmp_path / "b.jsonl"
    b.write_text('{"comment": "正文里提到 S074 但没带引号"}\n', encoding="utf-8")
    refs = check_references(["S074", "S130"], [a, b])
    assert refs["S074"] == [(a, 2)]
    assert refs["S130"] == []
