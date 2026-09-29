"""创业1000问.md → LLM 打标用的笔记数据。

解析规则（该 md 是 PDF 解析版，结构稳定）：
- `Q001｜…` 行起一条；问题可跨行，到 `答：` 开头的行为止（答案不进打标数据，
  打标对象是问题本身）；
- `第N章 …` 行记录所属章节，存进 tags；`## 第 N 页` 仅作条目边界；
- 问题文本按「去空白+去标点」归一后去重（保留首个，报告被去掉的 Q 号）。

产出：
- data/chuangye1000_notes.jsonl —— 笔记（note_id 即 Q 号，可回查原 md），
  供 labeler.run --run chuangye1000（prompt=cy_v1）与复核页使用。

用法：python scripts/prep_chuangye1000.py [--input 资料/创业1000问.md]
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

Q_RE = re.compile(r"^Q(\d{3,4})[｜|](.*)$")
ANS_RE = re.compile(r"^答[：:](.*)$")
CH_RE = re.compile(r"^第(\d+)章\s+(.+)$")
PAGE_RE = re.compile(r"^##\s+第\s*\d+\s+页\s*$")


@dataclass
class Entry:
    q_no: int
    page: int
    chapter: str
    question: str
    answer: str


def parse(md_text: str) -> list[Entry]:
    entries: list[Entry] = []
    page = 0
    chapter = ""
    cur: Entry | None = None
    in_answer = False

    def close() -> None:
        nonlocal cur
        if cur is not None:
            entries.append(cur)
        cur = None

    for line in md_text.splitlines():
        if PAGE_RE.match(line.strip()):
            page += 1
            continue
        m_ch = CH_RE.match(line.strip())
        if m_ch:
            close()
            chapter = f"第{m_ch.group(1)}章 {m_ch.group(2).strip()}"
            in_answer = False
            continue
        m_q = Q_RE.match(line)
        if m_q:
            close()
            cur = Entry(int(m_q.group(1)), page, chapter,
                        m_q.group(2).strip(), "")
            in_answer = False
            continue
        if cur is None:
            continue  # 文件头/章节页的散行
        m_a = ANS_RE.match(line)
        if m_a:
            in_answer = True
            cur.answer += m_a.group(1)
            continue
        if in_answer:
            cur.answer += line.strip()
        else:
            cur.question += line.strip()  # 问题跨行续接（PDF 换行）
    close()
    return entries


def norm(text: str) -> str:
    """去空白+去标点，只留文字与数字（用于完全重复检测）。"""
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def main() -> int:
    ap = argparse.ArgumentParser(description="创业1000问 md → notes + 空标注 stub")
    ap.add_argument("--input", type=Path,
                    default=ROOT.parent / "资料" / "创业1000问.md")
    ap.add_argument("--notes-out", type=Path,
                    default=ROOT / "data" / "chuangye1000_notes.jsonl")
    args = ap.parse_args()

    entries = parse(args.input.read_text(encoding="utf-8"))

    # ── 完整性检查 ──────────────────────────────────────────────
    q_nos = [e.q_no for e in entries]
    missing = sorted(set(range(1, 1001)) - set(q_nos))
    no_answer = [e.q_no for e in entries if not e.answer]
    no_question = [e.q_no for e in entries if not norm(e.question)]
    dup_ids = sorted({q for q in q_nos if q_nos.count(q) > 1})

    # ── 按归一文本去重（保留首个）───────────────────────────────
    seen: dict[str, int] = {}
    kept: list[Entry] = []
    dropped: list[tuple[int, int]] = []  # (被去掉的, 保留的)
    for e in entries:
        key = norm(e.question)
        if key in seen:
            dropped.append((e.q_no, seen[key]))
        else:
            seen[key] = e.q_no
            kept.append(e)

    notes_out: list[dict[str, object]] = []
    for e in kept:
        notes_out.append({
            "note_id": f"Q{e.q_no:03d}",
            "title": e.question,
            "desc": e.question,
            "tags": (e.chapter,) if e.chapter else (),
            "source": "创业1000问.md",
            "page": e.page,
        })

    args.notes_out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in notes_out) + "\n",
        encoding="utf-8")

    # ── 报告 ────────────────────────────────────────────────────
    ch_cnt: dict[str, int] = {}
    for e in kept:
        ch_cnt[e.chapter or "（无章节）"] = ch_cnt.get(e.chapter or "（无章节）", 0) + 1
    print(f"解析 {len(entries)} 条 | 去重后 {len(kept)} 条")
    for ch, n in ch_cnt.items():
        print(f"  {n:4d}  {ch}")
    if missing:
        print(f"⚠ 缺号: {missing}")
    if dup_ids:
        print(f"⚠ 重复 Q 号: {dup_ids}")
    if no_answer:
        print(f"⚠ 无答案（仅问题）: {no_answer}")
    if no_question:
        print(f"⚠ 空问题: {no_question}")
    if dropped:
        for a, b in dropped:
            print(f"⇂ 去重 Q{a:03d}（与 Q{b:03d} 同文）")
    print(f"→ 笔记 {args.notes_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
