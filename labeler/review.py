"""审核表导出：无法标注（场景/意图）+ 解析失败 → xlsx 供人工一一审核。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openpyxl

from labeler.notes import load_notes

ROOT = Path(__file__).resolve().parents[1]
DESC_LIMIT = 500


def load_annotations(path: Path) -> list[dict[str, Any]]:
    recs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def export_review(annotations_path: Path, notes_path: Path, run_name: str) -> Path:
    """挑出需要人工审核的记录，写出 xlsx。返回审核表路径（无待审行也写表头）。"""
    recs = load_annotations(annotations_path)
    by_id = {n.note_id: n for n in load_notes(notes_path)}

    rows = []
    for rec in recs:
        kinds = []
        reason = ""
        if rec.get("status") == "parse_failed":
            kinds.append("解析失败")
            reason = str(rec.get("error") or "")
        else:
            ann = rec.get("annotation") or {}
            unl = ann.get("unlabelable") or {}
            if unl.get("scenario"):
                kinds.append("场景无法标注")
            if unl.get("intent"):
                kinds.append("意图无法标注")
            reason = str(unl.get("reason") or "")
            if not kinds and (ann.get("warnings") or []):
                kinds.append("仅告警")
        if not kinds:
            continue
        note = by_id.get(str(rec.get("note_id")))
        rows.append((
            str(rec.get("note_id") or ""),
            note.title if note else "",
            (note.desc[:DESC_LIMIT] if note else ""),
            "、".join(kinds),
            reason,
            "; ".join(ann.get("warnings") or []) if rec.get("status") == "ok" else "",
            str(rec.get("raw_output") or "")[:500],
        ))

    out_dir = ROOT / "data" / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{run_name}_无法标注_{datetime.now(UTC).date().isoformat()}.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None  # Workbook() 总是带一个活动 sheet
    ws.title = "待人工审核"
    header = ["note_id", "标题", "正文(截断)", "问题类型", "模型原因/失败原因", "校验告警", "模型原始输出(截断)"]
    ws.append(header)
    for row in rows:
        ws.append(list(row))
    for col, width in zip("ABCDEFG", (24, 32, 60, 16, 36, 36, 60)):
        ws.column_dimensions[col].width = width
    wb.save(out)
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="从运行结果导出待审核 xlsx")
    ap.add_argument("--annotations", type=Path, required=True)
    ap.add_argument("--notes", type=Path, required=True)
    ap.add_argument("--run-name", type=str, default="run")
    args = ap.parse_args()
    out = export_review(args.annotations, args.notes, args.run_name)
    print(f"审核表: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
