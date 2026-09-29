"""改写草稿批量生成 + 最终改写导出。

用法::

    cd labeler
    python -m labeler.rewrite_batch draft --dry-run          # 只看分桶与将调用条数
    python -m labeler.rewrite_batch draft --limit 20         # 试跑 20 条
    python -m labeler.rewrite_batch draft                    # 全量断点续跑
    python -m labeler.rewrite_batch export --operator 名字   # 导出最终改写 xlsx

draft 对每条笔记用「已有的最好标签」当定稿（双人一致 > 单人复核 > 机器，同
改写台的建议语义）；--operator 传了则优先用该操作人在改写台已保存的定稿；
drop 条目跳过不生成。query 由 LLM 生成（rewrite_v3：转成明确的标准问题），
context 不走 LLM——代码对原帖做清洗（去 #tag/表情/@、砍二编三编）直接存。
草稿落 data/rewrites/drafts.jsonl（网页改写台自动带出，带 input_sig 过期标记，
标签变了重跑即可）。推荐流程：先全量跑一遍，人工在改写台只做核对和微调。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from labeler.adjudicate import compare_pair
from labeler.llm import LLMError, make_client
from labeler.notes import Note
from labeler.prompts import DEFAULT_REWRITE_PROMPT, RewriteInput, build_rewrite_prompt
from labeler.review import load_annotations
from labeler.review_server import record_from_dict
from labeler.review_tool import (
    DEFAULT_NOTES,
    DEFAULT_RUN,
    DEFAULT_XLSX,
    _default_annotations,
    build_task,
)
from labeler.rewrite import (
    append_draft,
    clean_context,
    clean_rewrite_output,
    export_rewrites,
    input_sig,
    load_drafts,
    load_operator_session,
    split_context_query,
    suggest_final,
)
from labeler.taxonomy import load_taxonomy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEY_FILE = ROOT / ".env"
REWRITES_DIR = ROOT / "data" / "rewrites"
SESSIONS_DIR = ROOT / "data" / "review_sessions"
MAX_CONSECUTIVE_LLM_FAIL = 10  # 连续失败熔断（与 run.py 同款）


def _load_session(name: str) -> dict[str, Any]:
    path = SESSIONS_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"复核会话不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _final_labels(nid: str, ra: Any, rb: Any, rec: dict[str, Any], left: str, right: str,
                  op_records: dict[str, dict[str, Any]],
                  ) -> tuple[str, list[str], list[str], str, str]:
    """定稿优先级：操作人已保存 > 建议定稿。返回 (status, 场景, 意图, 依据, 分桶)。"""
    op = op_records.get(nid)
    if op and op.get("final_status") in ("keep", "drop", "insufficient"):
        return (str(op["final_status"]),
                [str(s) for s in op.get("scenarios") or []],
                [str(i) for i in op.get("intents") or []],
                str(op.get("based_on") or "") or "操作人定稿", "operator")
    sug = suggest_final(compare_pair(ra, rb, rec), ra, rb, rec, left, right)
    return sug.final_status, sug.scenarios, sug.intents, sug.based_on, sug.bucket


def cmd_draft(args: argparse.Namespace) -> int:
    ann_path = args.annotations or _default_annotations(args.run)
    task = build_task(load_annotations(ann_path), args.notes, False)
    tax = load_taxonomy(args.xlsx)
    sa, sb = _load_session(args.left), _load_session(args.right)
    op_records = (load_operator_session(REWRITES_DIR / f"{args.operator}.json")
                  if args.operator else {})
    cached = load_drafts(args.out)

    buckets: Counter[str] = Counter()
    # (note, status, 场景, 意图, 依据, input_sig)
    todo: list[tuple[Note, str, list[str], list[str], str, str]] = []
    n_drop = n_fresh = 0
    for note, rec in task:
        ra_d, rb_d = sa.get(note.note_id), sb.get(note.note_id)
        ra = record_from_dict(ra_d) if ra_d else None
        rb = record_from_dict(rb_d) if rb_d else None
        status, sids, iids, based, bucket = _final_labels(
            note.note_id, ra, rb, rec, args.left, args.right, op_records)
        buckets[bucket] += 1
        if status == "drop":
            n_drop += 1
            continue
        sig = input_sig(status, sids, iids)
        c = cached.get(note.note_id)
        if not args.overwrite and c and c.get("input_sig") == sig:
            n_fresh += 1
            continue
        todo.append((note, status, sids, iids, based, sig))
    todo = todo[: args.limit or None]

    src = f"左={args.left} 右={args.right}" + (f" + 操作人={args.operator}" if args.operator else "")
    print(f"定稿来源: {src} | 分桶 {dict(buckets)} | 跳过 drop {n_drop} 草稿新鲜 {n_fresh} | "
          f"将生成 {len(todo)} 条 → {args.out}", flush=True)
    if args.dry_run:
        return 0

    client = make_client(args.model, protocol=args.protocol, key_file=args.key_file,
                         timeout=args.timeout)
    intent_names = {i.id: i.name for i in tax.intents}
    write_lock = threading.Lock()

    def worker(item: tuple[Note, str, list[str], list[str], str, str]) -> dict[str, Any]:
        """线程体：LLM 级失败转 ok=False，不落盘（下次续跑重试）。"""
        note, status, sids, iids, based, sig = item
        paths = []
        for s in sids:
            scen = tax.scenario_by_id(s)
            paths.append(scen.path if scen else s)
        system, user = build_rewrite_prompt(args.prompt, RewriteInput(
            note=note, scenario_paths=tuple(paths),
            intent_names=tuple(intent_names.get(i, i) for i in iids)))
        try:
            raw = client.chat(system, user, temperature=args.temperature,
                              max_tokens=args.max_tokens)
        except LLMError as exc:
            return {"note_id": note.note_id, "ok": False, "error": str(exc)[:180]}
        context = clean_context(note)              # context 一律代码清洗，不走 LLM
        if args.prompt == "rewrite_v2":            # 旧双段 prompt 才从输出里解析 context
            llm_ctx, draft = split_context_query(raw)
            context = llm_ctx or context
        else:
            draft = clean_rewrite_output(raw)
        if not draft:
            return {"note_id": note.note_id, "ok": False, "error": "模型返回为空"}
        return {"note_id": note.note_id, "ok": True, "draft": draft,
                "context": context,
                "final_status": status, "scenarios": sids, "intents": iids,
                "based_on": based, "input_sig": sig, "model": args.model,
                "prompt": args.prompt,
                "drafted_at": datetime.now(UTC).isoformat(timespec="seconds")}

    ok = fail = 0
    aborted = False
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as pool:
        futures: dict[Future[dict[str, Any]], str] = {
            pool.submit(worker, item): item[0].note_id for item in todo}
        streak = 0
        for done_count, fut in enumerate(as_completed(futures), start=1):
            rec_out = fut.result()
            if rec_out.get("ok"):
                ok += 1
                streak = 0
                with write_lock:
                    append_draft(args.out, {k: v for k, v in rec_out.items() if k != "ok"})
            else:
                fail += 1
                streak += 1
                print(f"  ✗ {futures[fut]}: {rec_out.get('error') or ''}（{streak}连）",
                      file=sys.stderr, flush=True)
                if streak >= MAX_CONSECUTIVE_LLM_FAIL:
                    print(f"连续 {streak} 条调用失败，疑似断网/服务不可用，中止"
                          "（重跑同一命令即可续）", file=sys.stderr)
                    aborted = True
                    for pending in futures:
                        pending.cancel()
                    break
            if done_count % 20 == 0 or done_count == len(todo):
                rate = done_count / max(time.time() - t0, 1e-6)
                eta = (len(todo) - done_count) / max(rate, 1e-6) / 60
                print(f"[{done_count}/{len(todo)}] ok={ok} fail={fail} | "
                      f"{rate:.1f}条/s ETA {eta:.0f}分", flush=True)

    print(f"\n{'中止' if aborted else '完成'}: ok={ok} fail={fail} → {args.out}")
    print("网页改写台刷新后会自动带出这些草稿（标签已变的会标「可能过期」）")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    ann_path = args.annotations or _default_annotations(args.run)
    task = build_task(load_annotations(ann_path), args.notes, False)
    tax = load_taxonomy(args.xlsx)
    records = load_operator_session(REWRITES_DIR / f"{args.operator}.json")
    if not records:
        raise SystemExit(f"操作人 {args.operator} 还没有任何定稿记录（先在改写台保存）")
    date = datetime.now(UTC).date().isoformat()
    out = args.out_dir / f"最终改写_{args.run}_{date}.xlsx"
    path, st = export_rewrites(task, tax, records, out)
    print(f"✅ {path}")
    print(json.dumps(st, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="改写草稿批量生成 / 最终改写导出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_d = sub.add_parser("draft", help="按建议定稿批量生成改写草稿（断点续跑）")
    ap_d.add_argument("--run", default=DEFAULT_RUN)
    ap_d.add_argument("--left", default="WH", help="左复核人（会话文件名）")
    ap_d.add_argument("--right", default="zlx", help="右复核人（会话文件名）")
    ap_d.add_argument("--operator", default="",
                      help="有则优先用该操作人在改写台已保存的定稿标签")
    ap_d.add_argument("--model", default="glm-5.3-flash")
    ap_d.add_argument("--prompt", default=DEFAULT_REWRITE_PROMPT,
                      help="rewrite_v3 = 明确问题（默认，context 由代码清洗）；"
                           "rewrite_v1 = 纯query；rewrite_v2 = context+query 双段 JSON")
    ap_d.add_argument("--protocol", choices=["anthropic", "v4", "workbuddy"],
                      default="workbuddy")
    ap_d.add_argument("--temperature", type=float, default=0.3)
    ap_d.add_argument("--max-tokens", type=int, default=3000)  # GLM5.3 thinking 吃 token，短改写也要留足预算
    ap_d.add_argument("--workers", type=int, default=4)
    ap_d.add_argument("--timeout", type=float, default=180.0)
    ap_d.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    ap_d.add_argument("--limit", type=int, default=0, help="只生成前 N 条（试跑用）")
    ap_d.add_argument("--overwrite", action="store_true",
                      help="草稿未过期也重新生成")
    ap_d.add_argument("--dry-run", action="store_true", help="只打印计划，不调 LLM")
    ap_d.add_argument("--out", type=Path, default=REWRITES_DIR / "drafts.jsonl")
    ap_d.add_argument("--annotations", type=Path, default=None)
    ap_d.add_argument("--notes", type=Path, default=DEFAULT_NOTES)
    ap_d.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)

    ap_e = sub.add_parser("export", help="导出操作人的最终改写 xlsx")
    ap_e.add_argument("--operator", required=True)
    ap_e.add_argument("--run", default=DEFAULT_RUN)
    ap_e.add_argument("--annotations", type=Path, default=None)
    ap_e.add_argument("--notes", type=Path, default=DEFAULT_NOTES)
    ap_e.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap_e.add_argument("-o", "--out-dir", type=Path, default=ROOT / "exports" / "复核" / "改写")

    args = ap.parse_args()
    return cmd_draft(args) if args.cmd == "draft" else cmd_export(args)


if __name__ == "__main__":
    sys.exit(main())
