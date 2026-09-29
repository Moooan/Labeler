"""人工复核工具：双人复核的 Excel 导入导出 + 一致性合并。

数据流（复核人在不同电脑上，用 Excel 文件传数据）::

    python -m labeler.review_tool export --reviewer A        # 生成复核任务 A.xlsx（预填机器标注）
    python -m labeler.review_server --reviewer A             # 或本机网页打标，导出同款 xlsx
    # 两位复核人各交回一个 xlsx（两个 Excel = 需要比对复核）
    python -m labeler.review_tool merge --a A.xlsx --b B.xlsx
        → 复核合并_*.xlsx：采纳一致 / 不一致待裁决 / 新场景建议 / 待另一人复核 / 统计

「复核」sheet 是交换契约，网页和手填都长一样（先分诊后打标）：
- 「状态」= 分诊结论：保留（继续打标）/ 不需要（不打标）/ 缺少信息（照常打标，
  并在「需补充信息」写这条还缺什么，如 目标专业、当前年级）/ 跳过
- 复核场景/复核意图 预填机器结果的 ID，改成本人结论（S107、I03，空格/逗号/换行分隔均可；
  用「场景速查」页查 ID）；清空 = 判定无适用标签。保留/缺少信息都需要打标
- 「场景不够用」填 是 并在「建议新场景」写建议路径，如：就业 > 求职流程 > 内推渠道
- 手填忘了写状态但改动过标签的，会被识别为「保留」
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openpyxl

from labeler.export_xlsx import _new_wb, _ws
from labeler.notes import Note, load_notes
from labeler.review import load_annotations
from labeler.taxonomy import Taxonomy, load_taxonomy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NOTES = ROOT / "data" / "xhs_notes.jsonl"
DEFAULT_XLSX = ROOT / "config" / "评测1.2标签体系v2.xlsx"
DEFAULT_RUN = "glm53flash_v1"

_SID_RE = re.compile(r"S\d{3}")
_IID_RE = re.compile(r"I\d{2}")
_KEEP_WORDS = {"保留", "已复核", "完成", "done", "ok", "keep"}
_SKIP_WORDS = {"跳过", "skip"}
_DROP_WORDS = {"丢弃", "不要", "不需要", "drop", "discard"}
_INS_WORDS = {"缺少信息", "缺信息", "信息不足", "insufficient", "lacking"}
_YES_WORDS = {"是", "y", "yes", "true", "1"}

REVIEW_HEADER = [
    "note_id", "标题", "正文(完整)",
    "机器场景", "复核场景", "场景不够用", "建议新场景",
    "机器意图", "复核意图",
    "机器画像", "复核画像", "需补充信息",
    "备注", "状态", "复核人", "复核时间",
]


@dataclass
class ReviewRecord:
    """单条复核结论（Excel 交换与网页会话共用的结构）。"""

    note_id: str
    scenarios: list[str] = field(default_factory=list)  # 复核后场景 ID（有序）
    intents: list[str] = field(default_factory=list)
    bad_fit: bool = False  # 场景不够用
    new_scenario: str = ""
    persona: list[str] = field(default_factory=list)  # "字段: 值 [明确]" 行
    missing_info: str = ""  # 缺少信息时的补充需求（缺什么才能判准）
    comment: str = ""
    status: str = ""  # 分诊结论: ""未复核 / keep保留 / drop不需要 / insufficient缺少信息 / skip跳过
    reviewer: str = ""
    reviewed_at: str = ""


def machine_scen_ids(rec: dict[str, Any]) -> list[str]:
    return [str(p.get("id")) for p in (rec.get("annotation") or {}).get("scenarios") or []]


def machine_intent_ids(rec: dict[str, Any]) -> list[str]:
    return [str(i) for i in (rec.get("annotation") or {}).get("intents") or []]


def machine_persona_lines(rec: dict[str, Any]) -> list[str]:
    lines = []
    for p in (rec.get("annotation") or {}).get("persona") or []:
        ev = f"｜依据: {p['evidence']}" if p.get("evidence") else ""
        lines.append(f"{p.get('field')}: {p.get('value')} [{p.get('basis')}]{ev}")
    return lines


def build_task(
    annotations: list[dict[str, Any]], notes_path: Path, include_unlabeled: bool = False,
) -> list[tuple[Note, dict[str, Any]]]:
    """复核任务清单：(笔记, 机器标注记录)。默认只含 ≥1 场景或意图的高质量子集。"""
    notes = {n.note_id: n for n in load_notes(notes_path)}
    by_id = {str(r.get("note_id")): r for r in annotations}
    task: list[tuple[Note, dict[str, Any]]] = []
    for nid, rec in by_id.items():
        if rec.get("status") != "ok":
            continue
        ann = rec.get("annotation") or {}
        if not include_unlabeled and not (ann.get("scenarios") or ann.get("intents")):
            continue
        note = notes.get(nid)
        if note is not None:
            task.append((note, rec))
    return task


def _scen_display(rec: dict[str, Any], tax: Taxonomy) -> str:
    lines = []
    for p in (rec.get("annotation") or {}).get("scenarios") or []:
        sid = str(p.get("id"))
        s = tax.scenario_by_id(sid)
        lines.append(f"{sid} {s.path if s else '?'} ({p.get('confidence')})")
    return "\n".join(lines)


def _intent_display(rec: dict[str, Any], tax: Taxonomy) -> str:
    names = {i.id: i.name for i in tax.intents}
    return "；".join(f"{i} {names.get(i, '?')}" for i in machine_intent_ids(rec))


def parse_ids(text: str, pattern: re.Pattern[str], valid: set[str], warn_out: list[str],
              note_id: str) -> list[str]:
    ids: list[str] = []
    for m in pattern.findall(text or ""):
        if m in valid:
            if m not in ids:
                ids.append(m)
        else:
            warn_out.append(f"{note_id}: 无效ID {m}（已忽略）")
    return ids


def record_to_row(note: Note, rec: dict[str, Any], tax: Taxonomy,
                  rv: ReviewRecord | None) -> list[Any]:
    """复核 sheet 一行。rv=None（未复核）时预填机器结果；rv 存在则原样输出——
    空的场景/意图列表是复核人「判定无适用标签」的明确结论，不能回填机器值。"""
    if rv is None:
        scen_txt, intent_txt = " ".join(machine_scen_ids(rec)), " ".join(machine_intent_ids(rec))
        pers_txt, missing, comment = "\n".join(machine_persona_lines(rec)), "", ""
        status_cn, bad_fit, new_scen = "", "", ""
    else:
        scen_txt, intent_txt = " ".join(rv.scenarios), " ".join(rv.intents)
        pers_txt = "\n".join(rv.persona)
        missing, comment = rv.missing_info, rv.comment
        status_cn = {"keep": "保留", "done": "保留", "drop": "不需要",
                     "insufficient": "缺少信息", "skip": "跳过"}.get(rv.status, "")
        bad_fit = "是" if rv.bad_fit else ""
        new_scen = rv.new_scenario
    return [
        note.note_id, note.title, note.desc,
        _scen_display(rec, tax), scen_txt, bad_fit, new_scen,
        _intent_display(rec, tax), intent_txt,
        "\n".join(machine_persona_lines(rec)), pers_txt, missing,
        comment, status_cn, rv.reviewer if rv else "", rv.reviewed_at if rv else "",
    ]


def export_workbook(task: list[tuple[Note, dict[str, Any]]], tax: Taxonomy,
                    records: dict[str, ReviewRecord], reviewer: str, out_path: Path) -> Path:
    """写复核任务/进度 xlsx（未复核的行预填机器结果，复核过的填复核结果）。"""
    wb = _new_wb()
    ws = wb.create_sheet("复核")
    ws.append(REVIEW_HEADER)
    for note, rec in task:
        ws.append(record_to_row(note, rec, tax, records.get(note.note_id)))
    for col, width in zip("ABCDEFGHIJKLMNOP",
                          (24, 28, 60, 36, 22, 10, 36, 22, 14, 36, 36, 28, 24, 8, 8, 20)):
        ws.column_dimensions[col].width = width

    ws2 = wb.create_sheet("场景速查")
    ws2.append(["场景ID", "一级", "二级", "三级", "完整路径", "定义", "是否需要知识库", "时机"])
    for s in tax.scenarios:
        ws2.append([s.id, s.l1, s.l2, s.l3, s.path, s.definition, s.kb_required, s.timing])
    ws3 = wb.create_sheet("意图速查")
    ws3.append(["意图ID", "名称", "定义"])
    for i in tax.intents:
        ws3.append([i.id, i.name, i.definition])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def _cell(row: tuple[Any, ...], idx: int) -> str:
    v = row[idx] if 0 <= idx < len(row) else None   # idx=-1 表示旧文件没有该列
    return str(v).strip() if v is not None else ""


def import_workbook(path: Path, tax: Taxonomy) -> dict[str, ReviewRecord]:
    """读回复核 xlsx → {note_id: ReviewRecord}。所有行都解析，是否算已复核由 merge 判定。"""
    wb = openpyxl.load_workbook(path, data_only=True)
    try:
        ws = wb["复核"]
        header = {str(c.value): idx for idx, c in enumerate(next(ws.iter_rows(max_row=1)))}
        col = {name: header[name] for name in REVIEW_HEADER}
        records: dict[str, ReviewRecord] = {}
        warns: list[str] = []
        valid_s = tax.scenario_ids()
        valid_i = tax.intent_ids()
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[col["note_id"]]:
                continue
            nid = _cell(row, col["note_id"])
            status_raw = _cell(row, col["状态"]).lower()
            status = ("keep" if status_raw in _KEEP_WORDS
                      else "skip" if status_raw in _SKIP_WORDS
                      else "drop" if status_raw in _DROP_WORDS
                      else "insufficient" if status_raw in _INS_WORDS else "")
            records[nid] = ReviewRecord(
                note_id=nid,
                scenarios=parse_ids(_cell(row, col["复核场景"]), _SID_RE, valid_s, warns, nid),
                intents=parse_ids(_cell(row, col["复核意图"]), _IID_RE, valid_i, warns, nid),
                bad_fit=_cell(row, col["场景不够用"]).lower() in _YES_WORDS,
                new_scenario=_cell(row, col["建议新场景"]),
                persona=[ln for ln in _cell(row, col["复核画像"]).splitlines() if ln.strip()],
                missing_info=_cell(row, header.get("需补充信息", -1)),  # 旧文件无此列
                comment=_cell(row, col["备注"]),
                status=status,
                reviewer=_cell(row, col["复核人"]),
                reviewed_at=_cell(row, col["复核时间"]),
            )
    finally:
        wb.close()
    for w in warns[:20]:
        print(f"⚠ {w}")
    if len(warns) > 20:
        print(f"⚠ …共 {len(warns)} 条无效ID告警")
    return records


def _is_reviewed(rv: ReviewRecord, rec: dict[str, Any]) -> bool:
    """显式状态优先；没写状态但改动过场景/意图或给了建议/备注也算已复核。"""
    if rv.status in ("keep", "done", "drop", "insufficient"):
        return True
    if rv.status == "skip":
        return False
    return (set(rv.scenarios) != set(machine_scen_ids(rec))
            or set(rv.intents) != set(machine_intent_ids(rec))
            or bool(rv.new_scenario) or bool(rv.comment))


def _verdict(rv: ReviewRecord | None, rec: dict[str, Any]) -> str | None:
    """分诊结论：None未完成/跳过 / keep保留并给标签 / drop不需要 / insufficient缺少信息。"""
    if rv is None:
        return None
    if rv.status == "skip":
        return None
    if rv.status == "drop":
        return "drop"
    if rv.status == "insufficient":
        return "insufficient"
    return "keep" if _is_reviewed(rv, rec) else None


def _fmt_ids(ids: list[str], tax: Taxonomy, kind: str) -> str:
    if not ids:
        return "（无）"
    if kind == "scenario":
        parts = []
        for sid in ids:
            s = tax.scenario_by_id(sid)
            parts.append(f"{sid} {s.path if s else '?'}")
        return "\n".join(parts)
    names = {i.id: i.name for i in tax.intents}
    return "；".join(f"{i} {names.get(i, '?')}" for i in ids)


def merge_reviews(
    a: dict[str, ReviewRecord], b: dict[str, ReviewRecord],
    task: list[tuple[Note, dict[str, Any]]], tax: Taxonomy, out_path: Path,
) -> tuple[Path, dict[str, Any]]:
    """双人复核合并：一致采纳、一致剔除、不一致待裁决、新场景建议、单人完成、统计。"""
    agree_rows: list[list[Any]] = []
    drop_rows: list[list[Any]] = []
    conflict_rows: list[list[Any]] = []
    suggest_rows: list[list[Any]] = []
    single_rows: list[list[Any]] = []
    n_both = n_agree = n_drop = n_ins = n_a_only = n_b_only = 0
    persona_diff = 0
    seen_sugg: set[tuple[str, str]] = set()
    verdict_name = {"keep": "给标签", "drop": "不需要", "insufficient": "缺少信息"}

    def verdict_txt(rv: ReviewRecord, v: str, kind: str) -> str:
        if v == "drop":
            return "（不需要）"
        txt = _fmt_ids(rv.scenarios if kind == "scenario" else rv.intents, tax, kind)
        if v == "insufficient":   # 缺少信息也打了标，附上缺什么的说明
            txt += f"\n（缺少信息{': ' + rv.missing_info if rv.missing_info else ''}）"
        return txt

    for note, rec in task:
        nid = note.note_id
        ra, rb = a.get(nid), b.get(nid)
        va, vb = _verdict(ra, rec), _verdict(rb, rec)

        for rv, who in ((ra, "A"), (rb, "B")):
            if rv is not None and (rv.bad_fit or rv.new_scenario):
                sugg = rv.new_scenario or "（勾了场景不够用但未填建议）"
                key = (nid, sugg)
                if key not in seen_sugg:
                    seen_sugg.add(key)
                    suggest_rows.append([nid, note.title, who, "是" if rv.bad_fit else "", sugg])

        if va and vb:
            assert ra is not None and rb is not None
            n_both += 1
            if va == vb == "drop":                          # 双方一致剔除
                n_drop += 1
                drop_rows.append([nid, note.title, "不需要",
                                  "；".join(filter(None, [ra.comment, rb.comment]))])
            elif (va != "drop" and vb != "drop"             # 保留/缺少信息 都按标签比对采纳
                  and set(ra.scenarios) == set(rb.scenarios)
                  and set(ra.intents) == set(rb.intents)):
                ins = va == "insufficient" or vb == "insufficient"
                if ins:
                    n_ins += 1
                else:
                    n_agree += 1
                pd = "" if {ln.split(":")[0] for ln in ra.persona} == \
                    {ln.split(":")[0] for ln in rb.persona} else "两人画像字段不一致"
                if pd:
                    persona_diff += 1
                miss = "；".join(filter(None, [
                    ra.missing_info if va == "insufficient" else "",
                    rb.missing_info if vb == "insufficient" else ""]))
                agree_rows.append([
                    nid, note.title, _fmt_ids(ra.scenarios, tax, "scenario"),
                    _fmt_ids(ra.intents, tax, "intent"),
                    "\n".join(ra.persona), pd, "是" if ins else "", miss,
                    "；".join(filter(None, [ra.comment, rb.comment])),
                ])
            else:                                        # 分诊不一致或标签不一致
                conflict_rows.append([
                    nid, note.title, note.desc,
                    verdict_txt(ra, va, "scenario"), verdict_txt(rb, vb, "scenario"),
                    _fmt_ids(machine_scen_ids(rec), tax, "scenario"),
                    verdict_txt(ra, va, "intent"), verdict_txt(rb, vb, "intent"),
                    _fmt_ids(machine_intent_ids(rec), tax, "intent"),
                    "；".join(filter(None, [ra.new_scenario, rb.new_scenario])),
                    "；".join(filter(None, [ra.comment, rb.comment])),
                ])
        elif va:
            n_a_only += 1
            assert ra is not None
            single_rows.append([nid, note.title,
                                f"A已完成（{verdict_name[va]}），B未复核",
                                verdict_txt(ra, va, "scenario")])
        elif vb:
            n_b_only += 1
            assert rb is not None
            single_rows.append([nid, note.title,
                                f"B已完成（{verdict_name[vb]}），A未复核",
                                verdict_txt(rb, vb, "scenario")])

    wb = _new_wb()
    _ws(wb, "采纳一致",
        ["note_id", "标题", "最终场景", "最终意图", "画像", "画像差异",
         "缺少信息", "需补充信息", "备注"],
        agree_rows, [24, 28, 44, 22, 36, 14, 10, 28, 28])
    _ws(wb, "一致剔除",
        ["note_id", "标题", "处置", "备注"],
        drop_rows, [24, 28, 12, 50])
    _ws(wb, "不一致待裁决",
        ["note_id", "标题", "正文(完整)", "A场景", "B场景", "机器场景",
         "A意图", "B意图", "机器意图", "新场景建议", "备注"],
        conflict_rows, [24, 28, 60, 36, 36, 36, 18, 18, 18, 30, 24])
    _ws(wb, "新场景建议",
        ["note_id", "标题", "复核人", "场景不够用", "建议（如：就业 > 求职流程 > 内推渠道）"],
        suggest_rows, [24, 28, 8, 10, 60])
    _ws(wb, "待另一人复核",
        ["note_id", "标题", "情况", "已完成方结论"],
        single_rows, [24, 28, 24, 44])
    stats: list[list[Any]] = [
        ["任务总数", len(task)],
        ["两人均完成", n_both],
        ["一致（保留·采纳）", n_agree],
        ["一致（缺少信息·采纳）", n_ins],
        ["一致（剔除·不需要）", n_drop],
        ["不一致（待裁决）", n_both - n_agree - n_drop - n_ins],
        ["仅A完成", n_a_only],
        ["仅B完成", n_b_only],
        ["两人均未完成", len(task) - n_both - n_a_only - n_b_only],
        ["一致率（按两人均完成，剔除也计入一致）",
         round((n_agree + n_drop + n_ins) / n_both, 3) if n_both else ""],
        ["一致中画像字段不一致", persona_diff],
        ["场景不够用/新场景建议条数", len(suggest_rows)],
    ]
    _ws(wb, "统计", ["指标", "数值"], stats, [28, 12])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path, {"total": len(task), "both": n_both, "agree": n_agree,
                      "drop": n_drop, "insufficient": n_ins,
                      "conflict": n_both - n_agree - n_drop - n_ins,
                      "a_only": n_a_only, "b_only": n_b_only}


def _default_annotations(run: str) -> Path:
    return ROOT / "data" / "runs" / run / "annotations.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser(description="双人复核：导出任务 / 校验导入 / 合并两份 Excel")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_e = sub.add_parser("export", help="生成复核任务 xlsx（预填机器标注）")
    ap_e.add_argument("--reviewer", required=True)
    ap_e.add_argument("--run", default=DEFAULT_RUN)
    ap_e.add_argument("--annotations", type=Path, default=None)
    ap_e.add_argument("--notes", type=Path, default=DEFAULT_NOTES)
    ap_e.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap_e.add_argument("--include-unlabeled", action="store_true",
                      help="把无法标注的笔记也放进任务（默认只放高质量子集）")
    ap_e.add_argument("-o", "--out", type=Path, default=None)

    ap_i = sub.add_parser("import", help="校验/预览一份复核 xlsx")
    ap_i.add_argument("--file", type=Path, required=True)
    ap_i.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)

    ap_m = sub.add_parser("merge", help="合并两位复核人的 Excel")
    ap_m.add_argument("--a", type=Path, required=True)
    ap_m.add_argument("--b", type=Path, required=True)
    ap_m.add_argument("--run", default=DEFAULT_RUN)
    ap_m.add_argument("--annotations", type=Path, default=None)
    ap_m.add_argument("--notes", type=Path, default=DEFAULT_NOTES)
    ap_m.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap_m.add_argument("--include-unlabeled", action="store_true")
    ap_m.add_argument("-o", "--out-dir", type=Path,
                      default=ROOT / "exports" / "复核")

    args = ap.parse_args()
    if args.cmd == "export":
        ann_path = args.annotations or _default_annotations(args.run)
        task = build_task(load_annotations(ann_path), args.notes, args.include_unlabeled)
        out = args.out or (ROOT / "exports" / "复核" /
                           f"复核任务_{args.run}_{args.reviewer}_{datetime.now(UTC).date()}.xlsx")
        path = export_workbook(task, load_taxonomy(args.xlsx), {}, args.reviewer, out)
        print(f"✅ 复核任务 {len(task)} 条 → {path}")
    elif args.cmd == "import":
        records = import_workbook(args.file, load_taxonomy(args.xlsx))
        kept = sum(1 for r in records.values() if r.status in ("keep", "done"))
        skipped = sum(1 for r in records.values() if r.status == "skip")
        dropped = sum(1 for r in records.values() if r.status == "drop")
        ins = sum(1 for r in records.values() if r.status == "insufficient")
        no_status = sum(1 for r in records.values()
                        if not r.status and (r.new_scenario or r.comment))
        bad = sum(1 for r in records.values() if r.bad_fit)
        cleared = sum(1 for r in records.values() if r.status in ("keep", "done") and not r.scenarios)
        print(f"解析 {len(records)} 条 | 保留 {kept} | 不需要 {dropped} | 缺少信息 {ins} "
              f"| 跳过 {skipped} | 未写状态但有建议/备注 {no_status} | 场景不够用 {bad} "
              f"| 判定无场景 {cleared}")
    else:
        ann_path = args.annotations or _default_annotations(args.run)
        task = build_task(load_annotations(ann_path), args.notes, args.include_unlabeled)
        tax = load_taxonomy(args.xlsx)
        ra = import_workbook(args.a, tax)
        rb = import_workbook(args.b, tax)
        out = args.out_dir / f"复核合并_{args.run}_{datetime.now(UTC).date()}.xlsx"
        path, st = merge_reviews(ra, rb, task, tax, out)
        print(f"✅ 合并 → {path}")
        print(json.dumps(st, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
