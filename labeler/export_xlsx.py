"""标注结果导出 xlsx：总表（含汇总页签）+ 按一级场景拆分的独立 excel。

输出结构（--out-dir，默认 exports/）::

    标注总表_{run}.xlsx        全部标注(高质量子集) / 场景汇总 / 意图汇总 / 画像汇总
                              / 未完成与待审核(无法标注+缺标都在这)
    一级场景/{一级}.xlsx        本类汇总(含零覆盖叶) / 明细(逐条query，正文完整不截断)

高质量子集 = 至少标出 1 个场景或 1 个意图的笔记；场景/意图/画像所有汇总页签
只基于该子集计算。无法标注的笔记（域外/纯宣泄帖）连同缺标条目只出现在
「未完成与待审核」页签供人工审核。
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font

from labeler.notes import load_notes
from labeler.review import load_annotations
from labeler.taxonomy import Taxonomy, load_taxonomy


def _ws(wb: openpyxl.Workbook, title: str, header: list[str], rows: list[list[Any]],
        widths: list[int]) -> openpyxl.Workbook:
    sheet = wb.create_sheet(title)
    sheet.append(header)
    for row in rows:
        sheet.append(list(row))
    wrap = Alignment(wrap_text=True, vertical="top")
    for idx, width in enumerate(widths, start=1):
        letter = sheet.cell(row=1, column=idx).column_letter
        sheet.column_dimensions[letter].width = width
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap
    return wb


def _new_wb() -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None  # Workbook() 总是带一个活动 sheet
    wb.remove(ws)
    return wb


def _fmt_scenarios(rec: dict[str, Any], tax: Taxonomy) -> tuple[str, int]:
    picks = (rec.get("annotation") or {}).get("scenarios") or []
    lines = []
    for p in picks:
        s = tax.scenario_by_id(str(p.get("id")))
        path = s.path if s else "?"
        lines.append(f"{p.get('id')} {path} ({p.get('confidence')})")
    return "\n".join(lines), len(lines)


def _fmt_intents(rec: dict[str, Any], tax: Taxonomy) -> tuple[str, int]:
    ids = (rec.get("annotation") or {}).get("intents") or []
    names = {i.id: i.name for i in tax.intents}
    return "；".join(f"{i} {names.get(i, '?')}" for i in ids), len(ids)


def _fmt_persona(rec: dict[str, Any]) -> tuple[str, int]:
    items = (rec.get("annotation") or {}).get("persona") or []
    lines = []
    for p in items:
        ev = f"｜依据: {p.get('evidence')}" if p.get("evidence") else ""
        lines.append(f"{p.get('field')}: {p.get('value')} [{p.get('basis')}]{ev}")
    return "\n".join(lines), len(lines)


def _dedupe(recs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for r in recs:  # 后写覆盖（续跑重标时保留最新）
        by_id[str(r.get("note_id"))] = r
    return by_id


def export_all(
    annotations: list[dict[str, Any]],
    notes_path: Path,
    tax: Taxonomy,
    run_name: str,
    out_dir: Path,
) -> list[Path]:
    by_id = _dedupe(annotations)
    notes = {n.note_id: n for n in load_notes(notes_path)}
    # 高质量子集：至少 1 个场景或意图标注；所有汇总只算这些
    kept = {
        nid for nid, rec in by_id.items()
        if (rec.get("annotation") or {}).get("scenarios")
        or (rec.get("annotation") or {}).get("intents")
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # ── 场景/意图/画像 计数（仅高质量子集）──────────────────────────
    leaf_count: Counter[str] = Counter()
    leaf_conf: dict[str, list[float]] = defaultdict(list)
    l1_of = {s.id: s.l1 for s in tax.scenarios}
    l1_count: Counter[str] = Counter()
    intent_count: Counter[str] = Counter()
    field_basis: dict[str, Counter[str]] = defaultdict(Counter)
    n_ok = n_unl_s = n_unl_i = 0
    for nid in kept:
        rec = by_id[nid]
        if rec.get("status") != "ok":
            continue
        n_ok += 1
        ann = rec.get("annotation") or {}
        for p in ann.get("scenarios") or []:
            sid = str(p.get("id"))
            leaf_count[sid] += 1
            try:
                leaf_conf[sid].append(float(p.get("confidence", 0.5)))
            except (TypeError, ValueError):
                pass
            l1_count[l1_of.get(sid, "?")] += 1
        for iid in ann.get("intents") or []:
            intent_count[str(iid)] += 1
        for p in ann.get("persona") or []:
            field_basis[str(p.get("field"))][str(p.get("basis"))] += 1
        unl = ann.get("unlabelable") or {}
        n_unl_s += bool(unl.get("scenario"))
        n_unl_i += bool(unl.get("intent"))

    # ── 总表 ─────────────────────────────────────────────────────────
    wb = _new_wb()
    rows_master = []
    for nid in kept:
        rec = by_id[nid]
        note = notes.get(nid)
        scen_txt, scen_n = _fmt_scenarios(rec, tax)
        intent_txt, intent_n = _fmt_intents(rec, tax)
        pers_txt, pers_n = _fmt_persona(rec)
        unl = (rec.get("annotation") or {}).get("unlabelable") or {}
        rows_master.append([
            nid,
            note.title if note else "",
            (note.desc if note else ""),
            scen_n, scen_txt,
            intent_n, intent_txt,
            pers_n, pers_txt,
            "是" if unl.get("scenario") else "",
            "是" if unl.get("intent") else "",
            str(unl.get("reason") or ""),
            "；".join((rec.get("annotation") or {}).get("warnings") or []),
        ])
    _ws(
        wb, "全部标注",
        ["note_id", "标题", "正文(完整)", "场景数", "场景标注(ID 路径 置信度)",
         "意图数", "意图标注", "画像字段数", "画像标注(字段: 值 [依据])",
         "场景无法标注", "意图无法标注", "原因", "校验告警"],
        rows_master,
        [24, 30, 50, 8, 44, 8, 24, 10, 40, 12, 12, 30, 26],
    )

    rows_leaf = []
    for s in tax.scenarios:
        confs = leaf_conf.get(s.id) or []
        avg = round(sum(confs) / len(confs), 2) if confs else ""
        rows_leaf.append([s.id, s.path, s.definition, leaf_count.get(s.id, 0), avg])
    _ws(wb, "场景汇总", ["场景ID", "完整路径", "定义", "标注笔记数", "平均置信度"],
        rows_leaf, [10, 40, 44, 12, 10])

    total_int = sum(intent_count.values())
    rows_intent = [[i.id, i.name, i.definition, intent_count.get(i.id, 0),
                    round(intent_count.get(i.id, 0) / max(total_int, 1), 3)]
                   for i in tax.intents]
    _ws(wb, "意图汇总", ["意图ID", "名称", "定义", "标注笔记数", "占标注次比"],
        rows_intent, [8, 14, 50, 12, 10])

    rows_pers: list[list[Any]] = []
    pers_counts: list[int] = []
    for f in tax.persona_fields:
        c = field_basis.get(f.name) or Counter()
        total = sum(c.values())
        rows_pers.append([f.group, f.name, f.sensitivity,
                          c.get("明确", 0), c.get("推测", 0), total])
        pers_counts.append(total)
    rows_pers = [row for _, row in sorted(zip(pers_counts, rows_pers), key=lambda t: -t[0])]
    _ws(wb, "画像汇总", ["字段组", "字段", "敏感级别", "明确数", "推测数", "合计"],
        rows_pers, [12, 14, 10, 8, 8, 8])

    rows_review = []
    for nid, note in notes.items():
        rec_r: dict[str, Any] | None = by_id.get(nid)
        if rec_r is None:
            rows_review.append([nid, note.title, "缺标(反复失败)", "", ""])
            continue
        if rec_r.get("status") != "ok":
            rows_review.append([nid, note.title, str(rec_r.get("status")),
                                str(rec_r.get("error") or ""), ""])
            continue
        unl = (rec_r.get("annotation") or {}).get("unlabelable") or {}
        if unl.get("scenario") or unl.get("intent"):
            kinds = "、".join(filter(None, [
                "场景" if unl.get("scenario") else "",
                "意图" if unl.get("intent") else "",
            ]))
            rows_review.append([nid, note.title, f"{kinds}无法标注",
                                str(unl.get("reason") or ""), ""])
    _ws(wb, "未完成与待审核", ["note_id", "标题", "问题类型", "原因/错误", ""],
        rows_review, [24, 32, 16, 40, 8])

    master = out_dir / f"标注总表_{run_name}.xlsx"
    wb.save(master)
    written.append(master)

    # ── 一级场景分表 ─────────────────────────────────────────────────
    l1_dir = out_dir / "一级场景"
    l1_dir.mkdir(parents=True, exist_ok=True)
    l1_names = sorted({s.l1 for s in tax.scenarios})
    for l1 in l1_names:
        leaves = [s for s in tax.scenarios if s.l1 == l1]
        leaf_ids = {s.id for s in leaves}
        wbx = _new_wb()
        rows_sum = []
        for s in leaves:
            confs = leaf_conf.get(s.id) or []
            avg = round(sum(confs) / len(confs), 2) if confs else ""
            rows_sum.append([s.id, s.l2, s.l3, s.path, leaf_count.get(s.id, 0), avg])
        _ws(wbx, "本类汇总", ["场景ID", "二级主题", "三级叶节点", "完整路径", "标注笔记数", "平均置信度"],
            rows_sum, [10, 16, 16, 40, 12, 10])
        rows_detail = []
        for nid, rec in by_id.items():
            picks = (rec.get("annotation") or {}).get("scenarios") or []
            hit = [p for p in picks if str(p.get("id")) in leaf_ids]
            if not hit:
                continue
            note = notes.get(nid)
            scen_txt, _ = _fmt_scenarios(rec, tax)
            intent_txt, _ = _fmt_intents(rec, tax)
            pers_txt, _ = _fmt_persona(rec)
            for p in hit:
                sid = str(p.get("id"))
                sc = tax.scenario_by_id(sid)
                rows_detail.append([
                    nid, note.title if note else "", (note.desc if note else ""),
                    f"{sid} {sc.path if sc else '?'} ({p.get('confidence')})",
                    scen_txt, intent_txt, pers_txt,
                ])
        _ws(wbx, "明细",
            ["note_id", "标题", "正文(完整)", "本类命中场景", "全部场景标注", "意图标注", "画像标注"],
            rows_detail, [24, 30, 46, 40, 44, 22, 38])
        path = l1_dir / f"{l1}.xlsx"
        wbx.save(path)
        written.append(path)

    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="标注结果导出 xlsx（总表+一级场景分表）")
    ap.add_argument("--annotations", type=Path, required=True)
    ap.add_argument("--notes", type=Path, required=True)
    ap.add_argument("--xlsx", type=Path, required=True)
    ap.add_argument("--run-name", type=str, default="run")
    ap.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parents[1] / "exports")
    args = ap.parse_args()
    written = export_all(load_annotations(args.annotations), args.notes,
                         load_taxonomy(args.xlsx), args.run_name, args.out_dir)
    for p in written:
        print(f"✅ {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
