"""复标对比：两个运行的逐帖一致性（场景 Jaccard、意图 Jaccard、画像字段重合）。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from labeler.review import load_annotations


def _index(path: Path) -> dict[str, dict[str, Any]]:
    return {str(r.get("note_id")): r for r in load_annotations(path) if r.get("status") == "ok"}


def _scenario_ids(rec: dict[str, Any]) -> set[str]:
    return {str(p.get("id")) for p in (rec.get("annotation") or {}).get("scenarios") or []}


def _intent_ids(rec: dict[str, Any]) -> set[str]:
    return {str(i) for i in (rec.get("annotation") or {}).get("intents") or []}


def _persona_fields(rec: dict[str, Any]) -> set[str]:
    return {str(p.get("field")) for p in (rec.get("annotation") or {}).get("persona") or []}


def _jaccard(a: set[str], b: set[str]) -> float | None:
    if not a and not b:
        return None
    union = a | b
    return len(a & b) / len(union) if union else None


def main() -> int:
    ap = argparse.ArgumentParser(description="对比两次标注运行的一致性")
    ap.add_argument("--a", type=Path, required=True, help="运行 A 的 annotations.jsonl")
    ap.add_argument("--b", type=Path, required=True, help="运行 B 的 annotations.jsonl")
    args = ap.parse_args()

    ia, ib = _index(args.a), _index(args.b)
    common = sorted(set(ia) & set(ib))
    print(f"A {len(ia)} 条 | B {len(ib)} 条 | 共同 {len(common)} 条")

    scen_scores, intent_scores = [], []
    persona_agree = 0
    disagreements = []
    for nid in common:
        ra, rb = ia[nid], ib[nid]
        js = _jaccard(_scenario_ids(ra), _scenario_ids(rb))
        ji = _jaccard(_intent_ids(ra), _intent_ids(rb))
        if js is not None:
            scen_scores.append(js)
        if ji is not None:
            intent_scores.append(ji)
        pa, pb = _persona_fields(ra), _persona_fields(rb)
        if not (pa ^ pb):
            persona_agree += 1
        if (js is not None and js < 0.5) or (ji is not None and ji < 0.5):
            disagreements.append(
                (nid, sorted(_scenario_ids(ra)), sorted(_scenario_ids(rb)),
                 sorted(_intent_ids(ra)), sorted(_intent_ids(rb)))
            )

    def _stats(scores: list[float]) -> str:
        if not scores:
            return "n/a"
        return f"均值 {sum(scores) / len(scores):.3f} | 完全一致 {sum(s == 1.0 for s in scores)}/{len(scores)}"

    print(f"场景 Jaccard: {_stats(scen_scores)}")
    print(f"意图 Jaccard: {_stats(intent_scores)}")
    print(f"画像字段集完全一致: {persona_agree}/{len(common)}")
    print(f"\n低一致帖（任一 Jaccard<0.5）{len(disagreements)} 条：")
    for nid, sa, sb, ia_, ib_ in disagreements[:20]:
        print(f"  {nid}\n    A场景={sa} B场景={sb}\n    A意图={ia_} B意图={ib_}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
