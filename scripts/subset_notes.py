"""从 notes jsonl 里按首个 tag 分组等距抽样，生成复核用子集。

用途：创业1000问全量 1000 条 → 每章抽 20 条共 200 条的开一个小复核任务，
与全量复核互不干扰。组内等距（步长 = ceil(组大小/每组条数)），确定可复现。

用法：python scripts/subset_notes.py --notes data/chuangye1000_notes.jsonl \
        --out data/chuangye200_notes.jsonl --per-group 20
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description="notes jsonl 按首 tag 分组等距抽子集")
    ap.add_argument("--notes", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--per-group", type=int, default=20, help="每组抽多少条")
    args = ap.parse_args()

    rows = [json.loads(line) for line in
            args.notes.read_text(encoding="utf-8").splitlines() if line.strip()]
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for r in rows:
        tags = [str(t) for t in (r.get("tags") or [])]
        groups[tags[0] if tags else "（无tag）"].append(r)

    picked: list[dict[str, object]] = []
    for g, items in groups.items():
        step = max(1, math.ceil(len(items) / args.per_group))
        take = items[step - 1::step][: args.per_group]  # 等距，含组尾附近
        print(f"  {len(take):3d}/{len(items):4d}  {g}")
        picked.extend(take)

    args.out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in picked) + "\n",
        encoding="utf-8")
    print(f"→ 共 {len(picked)} 条 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
