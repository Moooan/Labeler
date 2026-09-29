"""场景标签 CSV → taxonomy_additions.json 同步。

用户在 exports/评测1.2标签体系v2 - 场景标签.csv 里手改场景标签；系统实际生效的
是 config/ 下的评测 xlsx（Finder 快照）+ data/taxonomy_additions.json（CSV 没有
代码消费）。本模块把两者的差异算出来写回 additions（原 xlsx 永远不动）::

    python -m labeler.taxonomy_sync                    # 只出报告（默认）
    python -m labeler.taxonomy_sync --apply            # 增/改/元数据生效（删除缓执行）
    python -m labeler.taxonomy_sync --apply --force-deletes   # 连删除也执行

删除默认缓执行：被删 ID 在 review_sessions / rewrites / 标注 jsonl 里往往还有
引用，直接删会让已保存的标签在页面/导出里显示成悬空 ID。建议等最终改写 xlsx
导出后再 --force-deletes。历史标注数据一律不改。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from labeler.review_tool import DEFAULT_XLSX
from labeler.taxonomy import DEFAULT_ADDITIONS, Taxonomy, load_taxonomy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "exports" / "评测1.2标签体系v2 - 场景标签.csv"

CSV_COLUMNS = ("场景ID", "一级场景", "二级主题", "三级叶节点", "完整路径",
               "叶节点定义", "是否需要知识库", "时机")

# CSV 新行有缺字段的拟稿（--apply 时会写进 additions，之后可随手改 JSON）。
# 拟稿只在该 ID 确实出现在本次差异里时才生效，不影响别的行。
PROPOSALS: dict[str, dict[str, str]] = {
    "S235": {  # 定义在 CSV 里清空 = 删去「含四六级…」分句（该部分挪去了 S238）
        "definition": "求职与职业发展相关的资格考试与证书（教资、法考、CPA/CFA、一建、"
                      "医师/护士、软考、PMP 等）：是否值得考与证书选择、报名条件与跨专业"
                      "报考、备考规划与科目安排、成绩与拿证流程。不含公务员/事业编录用考试"
                      "（考公考编 l1）、留学语言与标化考试（S047/S048）、在校通用证书考试"
                      "（S238）、校内课程考试（S062）",
    },
    "S236": {"definition": "专业知识与技能的提升：学什么、怎么学、学到什么程度"
                           "（区别于 S221 的提升方向选择）"},
    "S237": {"definition": "可迁移软技能提升：沟通表达、时间管理、逻辑思维、"
                           "学习能力等通用能力的训练"},
    "S238": {"l3": "常规证书考试",
             "definition": "在校通用证书考试：英语四六级、计算机二级、普通话等的"
                           "报考条件、时间与备考安排"},
}

_FORMAT_DOC = [
    "leaves: 追加新叶，id 必填且不得与已有重复，l1/l2/l3 必填，definition 短名词句",
    ("updates: 对已有叶（原表或 leaves 里的）打部分补丁，键为场景 ID，值只写要改的"
     "字段（l1/l2/l3/definition/business），path 按 l1>l2>l3 自动重拼"),
    "metadata: {场景ID: {kb_required/timing}}，存档元数据，不进 prompt",
    "deletes: 要删除的场景 ID 列表（最后应用；历史标注数据不跟着改，删前确认无悬空引用）",
    "renames: 旧式整叶改名（等价 updates，保留兼容；新改动请写 updates）",
]


@dataclass
class Diff:
    """CSV 与生效标签体系的差异。"""

    added: dict[str, dict[str, str]] = field(default_factory=dict)
    # CSV 里有、生效体系里没有（id → CSV 行）
    removed: list[str] = field(default_factory=list)   # 生效体系里有、CSV 里没有
    changed: dict[str, dict[str, str]] = field(default_factory=dict)
    # {id: {字段: CSV 值}}；CSV 留空 = 未改（不是清空）
    metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    # {id: {kb_required/timing: 值}}
    stale_paths: list[str] = field(default_factory=list)
    # CSV 完整路径列与自己的 l1/l2/l3 拼接不一致（仅提示；载入时反正重拼）
    suppressed: list[str] = field(default_factory=list)
    # 生效值就是 PROPOSALS 拟稿（有意覆写 CSV 的占位值），不算差异（如 S238.definition）


def read_scenario_csv(path: Path) -> list[dict[str, str]]:
    """读场景标签 CSV（utf-8-sig 去 BOM；只取 8 个命名列，忽略尾部多余空列）。"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{path}: 缺列 {missing}（导出的 8 列格式变了？）")
        rows = []
        for raw in reader:
            r = {c: (raw.get(c) or "").strip() for c in CSV_COLUMNS}
            if r["场景ID"]:
                rows.append(r)
    return rows


def diff_taxonomy(rows: list[dict[str, str]], tax: Taxonomy) -> Diff:
    """CSV 行 vs 生效标签体系，逐 ID 算增/删/改/元数据。"""
    csv_by_id: dict[str, dict[str, str]] = {}
    for r in rows:
        if r["场景ID"] in csv_by_id:
            raise SystemExit(f"CSV 里场景 ID 重复: {r['场景ID']}")
        csv_by_id[r["场景ID"]] = r
    tax_by_id = {s.id: s for s in tax.scenarios}

    diff = Diff(
        added={sid: r for sid, r in csv_by_id.items() if sid not in tax_by_id},
        removed=[s.id for s in tax.scenarios if s.id not in csv_by_id],
    )
    fields = (("l1", "一级场景"), ("l2", "二级主题"),
              ("l3", "三级叶节点"), ("definition", "叶节点定义"))
    for sid, s in tax_by_id.items():
        row = csv_by_id.get(sid)
        if row is None:
            continue
        patch: dict[str, str] = {}
        for f, col in fields:
            v = row[col]
            if not v or v == getattr(s, f):
                continue
            prop = PROPOSALS.get(sid, {}).get(f, "")
            if prop and prop == getattr(s, f):   # 拟稿有意覆写 CSV 的占位值
                diff.suppressed.append(f"{sid}.{f}")
                continue
            patch[f] = v
        if patch:
            diff.changed[sid] = patch
        meta = {k: row[c] for k, c in (("kb_required", "是否需要知识库"), ("timing", "时机"))
                if row[c] and row[c] != getattr(s, k)}
        if meta:
            diff.metadata[sid] = meta
        want = " > ".join(x for x in (row["一级场景"], row["二级主题"], row["三级叶节点"]) if x)
        if row["完整路径"] and row["完整路径"] != want:
            diff.stale_paths.append(sid)
    return diff


def check_references(ids: list[str], files: list[Path]) -> dict[str, list[tuple[Path, int]]]:
    """统计被删 ID 在标注数据里的引用（按 "SID" 带引号计数，即 JSON 值形态）。"""
    refs: dict[str, list[tuple[Path, int]]] = {}
    for sid in ids:
        needle = f'"{sid}"'
        hits = [(f, f.read_text(encoding="utf-8", errors="ignore").count(needle))
                for f in files if f.exists()]
        refs[sid] = [(f, n) for f, n in hits if n]
    return refs


def _data_files(root: Path) -> list[Path]:
    """可能引用场景 ID 的标注数据（复核会话/裁决/改写/机器标注）。"""
    files: list[Path] = []
    for pattern in ("data/review_sessions/*.json", "data/adjudications/*.json",
                    "data/rewrites/*.json", "data/runs/*/annotations.jsonl"):
        files.extend(sorted(root.glob(pattern)))
    return files


def build_additions(current: dict[str, Any], diff: Diff, *, include_deletes: bool
                    ) -> dict[str, Any]:
    """现有 additions + 本次差异 → 新 additions（纯函数，不落盘）。

    旧式 renames 迁移进 updates（单一事实源）；同字段以本次 CSV 补丁为准。
    """
    out: dict[str, Any] = {}
    if "_说明" in current:
        out["_说明"] = current["_说明"]
    out["_格式"] = _FORMAT_DOC

    updates: dict[str, dict[str, str]] = {}
    for d in current.get("renames") or []:   # 旧式改名 → updates
        updates[str(d.get("id"))] = {k: str(v) for k, v in d.items()
                                     if k != "id" and v}
    for sid, patch in (current.get("updates") or {}).items():
        updates[sid] = {**updates.get(sid, {}), **{k: str(v) for k, v in patch.items()}}
    for sid, patch in diff.changed.items():
        updates[sid] = {**updates.get(sid, {}), **patch}
    for sid, patch in PROPOSALS.items():     # 拟稿只补确实出现在差异里的行
        if sid in diff.changed:
            updates[sid] = {**updates.get(sid, {}), **patch}
    if updates:
        out["updates"] = updates

    leaves = [dict(d) for d in current.get("leaves") or []]
    known = {str(d.get("id")) for d in leaves}
    for sid, r in diff.added.items():
        if sid in known:
            continue
        prop = PROPOSALS.get(sid, {})
        l3 = r["三级叶节点"] or prop.get("l3", "")
        if not l3:
            raise SystemExit(f"{sid} 的三级叶节点为空：请在 CSV 补上，"
                             f"或在 {__name__}.PROPOSALS 里拟稿")
        leaf: dict[str, str] = {"id": sid, "l1": r["一级场景"], "l2": r["二级主题"],
                                "l3": l3,
                                "definition": prop.get("definition") or r["叶节点定义"]}
        if r["是否需要知识库"]:
            leaf["kb_required"] = r["是否需要知识库"]
        if r["时机"]:
            leaf["timing"] = r["时机"]
        leaves.append(leaf)
    out["leaves"] = leaves

    # metadata/deletes 保留已有内容再合并（apply 是幂等的重复同步，不是覆盖重写）
    metadata: dict[str, dict[str, str]] = {
        sid: dict(m) for sid, m in (current.get("metadata") or {}).items()}
    for sid, patch in diff.metadata.items():
        metadata[sid] = {**metadata.get(sid, {}), **patch}
    if metadata:
        out["metadata"] = metadata
    deletes = [str(x) for x in current.get("deletes") or []]
    if include_deletes:
        deletes = list(dict.fromkeys(deletes + diff.removed))   # 去重保序
    if deletes:
        out["deletes"] = deletes
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="场景标签 CSV → additions JSON 同步（原 xlsx 不动）")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--additions", type=Path, default=DEFAULT_ADDITIONS)
    ap.add_argument("--apply", action="store_true", help="把差异写回 additions（默认只出报告）")
    ap.add_argument("--force-deletes", action="store_true",
                    help="连删除也执行（默认缓执行：先确认标注数据无悬空引用）")
    args = ap.parse_args()

    tax = load_taxonomy(args.xlsx, args.additions)
    rows = read_scenario_csv(args.csv)
    diff = diff_taxonomy(rows, tax)

    print(f"CSV {len(rows)} 叶 vs 生效体系 {len(tax.scenarios)} 叶")
    for sid, r in diff.added.items():
        prop = PROPOSALS.get(sid, {})
        l3 = r["三级叶节点"] or prop.get("l3", "")
        defi = prop.get("definition") or r["叶节点定义"] or "（⚠ 定义为空）"
        mark = "（l3 拟稿）" if not r["三级叶节点"] else ""
        print(f"  + {sid} {r['一级场景']} > {r['二级主题']} > {l3}{mark}：{defi[:60]}")
    for sid, patch in diff.changed.items():
        for f, v in patch.items():
            print(f"  ± {sid} {f} → {v[:60]}")
    if diff.metadata:
        n_kb = sum(1 for m in diff.metadata.values() if "kb_required" in m)
        n_tm = sum(1 for m in diff.metadata.values() if "timing" in m)
        print(f"  ◇ 元数据：是否需要知识库 {n_kb} 条、时机 {n_tm} 条")
    if diff.stale_paths:
        print(f"  ⚠ CSV 完整路径列与名称不一致（仅提示，载入时自动重拼）："
              f"{' '.join(diff.stale_paths)}")
    if diff.suppressed:
        print(f"  ⓘ 拟稿已覆写 CSV 占位值（不算差异）：{' '.join(diff.suppressed)}")
    if diff.removed:
        print(f"  - 删除 {len(diff.removed)}：{' '.join(diff.removed)}")
        for sid, hits in check_references(diff.removed, _data_files(ROOT)).items():
            detail = "、".join(f"{p.relative_to(ROOT)}×{n}" for p, n in hits)
            print(f"      {sid} ← {detail or '（无引用）'}")

    if not args.apply:
        print("\n只出报告未写盘；确认后 --apply（删除缓执行，最终改写导出后再 --force-deletes）")
        return 0

    current: dict[str, Any] = {}
    if args.additions.exists():
        loaded = json.loads(args.additions.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            current = loaded
    out = build_additions(current, diff, include_deletes=args.force_deletes)
    fd, tmp = tempfile.mkstemp(dir=args.additions.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    Path(tmp).replace(args.additions)

    # 回检：能完整载入，且剩余差异只剩缓执行的删除
    new_tax = load_taxonomy(args.xlsx, args.additions)
    rest = diff_taxonomy(read_scenario_csv(args.csv), new_tax)
    leftovers = [x for x in (
        f"+{list(rest.added)}" if rest.added else "",
        f"±{list(rest.changed)}" if rest.changed else "",
        f"◇{list(rest.metadata)}" if rest.metadata else "",
        f"-{rest.removed}" if args.force_deletes and rest.removed else "",
    ) if x]
    if leftovers:
        print(f"⚠ 写回后仍有差异（不应发生）：{' '.join(leftovers)}")
        return 1
    if rest.removed:
        print(f"删除已缓执行（{len(rest.removed)} 条：{' '.join(rest.removed)}），"
              f"最终改写导出后跑 --apply --force-deletes 补上")
    print(f"✅ 已写回 {args.additions}（生效 {len(new_tax.scenarios)} 叶）——重启 review_server 后生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
