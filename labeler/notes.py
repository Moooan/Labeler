"""笔记数据读取（容错：坏行跳过并告警，与 Finder 侧约定一致）。"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Note:
    note_id: str
    title: str
    desc: str
    tags: tuple[str, ...]
    raw: dict[str, Any]

    def text_for_prompt(self, desc_limit: int = 2000) -> str:
        parts = [f"标题：{self.title or '(无)'}"]
        desc = self.desc[:desc_limit]
        if len(self.desc) > desc_limit:
            desc += "…(截断)"
        parts.append(f"正文：{desc or '(无)'}")
        if self.tags:
            parts.append(f"话题标签：{' '.join(self.tags[:20])}")
        return "\n".join(parts)


def load_notes(path: Path) -> list[Note]:
    notes: list[Note] = []
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        nid = str(r.get("note_id") or "")
        if not nid:
            bad += 1
            continue
        tags = [str(t) for t in (r.get("tags") or []) if str(t).strip()]
        notes.append(
            Note(
                note_id=nid,
                title=str(r.get("title") or ""),
                desc=str(r.get("desc") or ""),
                tags=tuple(tags),
                raw=r,
            )
        )
    if bad:
        print(f"⚠ 跳过 {bad} 条坏行: {path}", file=sys.stderr)
    # 去重（后写覆盖先写），保持首次出现顺序
    seen: dict[str, Note] = {}
    for n in notes:
        seen.setdefault(n.note_id, n)
    return list(seen.values())
