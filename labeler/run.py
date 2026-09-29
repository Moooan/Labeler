"""标注主流程：配置驱动的单轮标注（多模型/多 prompt 复标 = 多次以不同配置运行）。

用法::

    cd labeler
    python -m labeler.run --run glm53flash_v1                # 用 config/runs.json 里定义的运行
    python -m labeler.run --model glm-5.3-flash --limit 3    # 冒烟
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from labeler.llm import LLMError, make_client
from labeler.notes import Note, load_notes
from labeler.parse import parse_annotation
from labeler.prompts import build_prompt
from labeler.taxonomy import Taxonomy, load_taxonomy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NOTES = ROOT / "data" / "xhs_notes.jsonl"
DEFAULT_XLSX = ROOT / "config" / "评测1.2标签体系v2.xlsx"
DEFAULT_KEY_FILE = ROOT / ".env"
RUNS_FILE = ROOT / "config" / "runs.json"
RETRY_PER_NOTE = 2  # 单条笔记：解析失败再给一次机会（把原始输出扔回去要求修正）
MAX_CONSECUTIVE_LLM_FAIL = 10  # 连续网络/API失败熔断（断网时别把清单空转完）


def load_run_config(name: str) -> dict[str, Any]:
    if not RUNS_FILE.exists():
        raise SystemExit(f"运行配置不存在: {RUNS_FILE}")
    cfg = json.loads(RUNS_FILE.read_text(encoding="utf-8")).get("runs", {})
    if name not in cfg:
        raise SystemExit(f"未定义的运行: {name}（可用: {sorted(cfg)}）")
    return cfg[name]


def load_done(out_path: Path) -> set[str]:
    done: set[str] = set()
    if not out_path.exists():
        return done
    for line in out_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            done.add(str(json.loads(line).get("note_id")))
        except json.JSONDecodeError:
            continue
    return done


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def annotation_to_dict(ann: Any) -> dict[str, Any]:
    return {
        "scenarios": [{"id": p.id, "confidence": p.confidence} for p in ann.scenarios],
        "intents": list(ann.intents),
        "persona": [dataclasses.asdict(p) for p in ann.persona],
        "unlabelable": {
            "scenario": ann.unlabelable_scenario,
            "intent": ann.unlabelable_intent,
            "reason": ann.reason,
        },
        "warnings": ann.warnings,
    }


def label_one(client: Any, tax: Taxonomy, prompt_name: str, note: Note,
              temperature: float, max_tokens: int) -> dict[str, Any]:
    """单条标注：调用 → 解析 →（失败则带错误反馈重试一次）。"""
    system, user = build_prompt(prompt_name, tax, note)
    last_raw, last_err = "", ""
    for attempt in range(1, RETRY_PER_NOTE + 1):
        if attempt > 1:
            user = (
                f"你上次的输出无法解析（{last_err}），请只重新输出修正后的 JSON：\n{last_raw[:800]}"
            )
        raw = client.chat(system, user, temperature=temperature, max_tokens=max_tokens)
        ann, err = parse_annotation(raw, tax)
        if ann is not None:
            return {
                "note_id": note.note_id,
                "status": "ok",
                "annotation": annotation_to_dict(ann),
                "attempts": attempt,
                "raw_output": raw,
            }
        last_raw, last_err = raw, err
    return {
        "note_id": note.note_id,
        "status": "parse_failed",
        "annotation": None,
        "attempts": RETRY_PER_NOTE,
        "error": last_err,
        "raw_output": last_raw,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="标签体系标注（单轮，可断点续跑）")
    ap.add_argument("--run", type=str, help="config/runs.json 中的运行名")
    ap.add_argument("--model", type=str, help="模型名（与 --run 二选一）")
    ap.add_argument("--prompt", type=str, default="v1")
    ap.add_argument("--protocol", choices=["anthropic", "v4", "workbuddy"], default="anthropic")
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--max-tokens", type=int, default=6000)
    ap.add_argument("--notes", type=Path, default=DEFAULT_NOTES)
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    ap.add_argument("--out-dir", type=Path, default=None, help="默认 data/runs/{run_name}")
    ap.add_argument("--limit", type=int, default=0, help="只标前 N 条（冒烟用）")
    ap.add_argument(
        "--workers", type=int, default=1, help="并发线程数（思考型模型单条 45~65s，建议 16）"
    )
    ap.add_argument(
        "--timeout", type=float, default=180.0, help="单请求读超时秒（默认 180）"
    )
    ap.add_argument("--no-review", action="store_true", help="跑完不导出审核表")
    args = ap.parse_args()

    if args.run:
        cfg = load_run_config(args.run)
        model = str(cfg.get("model") or args.model)
        prompt = str(cfg.get("prompt") or args.prompt)
        temperature = float(cfg.get("temperature", args.temperature))
        max_tokens = int(cfg.get("max_tokens", args.max_tokens))
        protocol = str(cfg.get("protocol") or args.protocol)
        run_name = args.run
    elif args.model:
        model, prompt = args.model, args.prompt
        temperature, max_tokens, protocol = args.temperature, args.max_tokens, args.protocol
        run_name = f"{model.replace('.', '_').replace('-', '_')}_{prompt}"
    else:
        raise SystemExit("需要 --run 或 --model")

    out_dir = args.out_dir or ROOT / "data" / "runs" / run_name
    out_path = out_dir / "annotations.jsonl"

    tax = load_taxonomy(args.xlsx)
    notes = load_notes(args.notes)
    done = load_done(out_path)
    todo = [n for n in notes if n.note_id not in done][: args.limit or None]
    print(
        f"运行 {run_name}: model={model} prompt={prompt} 协议={protocol} | "
        f"标签: 场景{len(tax.scenarios)} 意图{len(tax.intents)} 画像{len(tax.persona_fields)} | "
        f"待标 {len(todo)} / 总 {len(notes)}（已完成 {len(done)}）→ {out_path}",
        flush=True,
    )

    client = make_client(model, protocol=protocol, key_file=args.key_file, timeout=args.timeout)
    write_lock = threading.Lock()

    def worker(note: Note) -> dict[str, Any]:
        """线程体：网络级失败转为 llm_failed 哨兵，不落盘（下次续跑重试）。"""
        try:
            return label_one(client, tax, prompt, note, temperature, max_tokens)
        except LLMError as exc:
            return {"note_id": note.note_id, "status": "llm_failed", "error": str(exc)[:180]}

    def persist(rec: dict[str, Any]) -> str:
        status = str(rec.get("status"))
        if status != "llm_failed":
            rec["run"] = run_name
            rec["model"] = model
            rec["prompt"] = prompt
            rec["annotated_at"] = datetime.now(UTC).isoformat()
            with write_lock:
                append_jsonl(out_path, rec)
        return status

    ok = fail = llm_fails = 0
    aborted = False
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as pool:
        futures: dict[Future[dict[str, Any]], str] = {
            pool.submit(worker, note): note.note_id for note in todo
        }
        streak = 0
        for done_count, fut in enumerate(as_completed(futures), start=1):
            rec = fut.result()
            status = persist(rec)
            if status == "ok":
                ok += 1
                streak = 0
            elif status == "llm_failed":
                llm_fails += 1
                streak += 1
                print(
                    f"  ✗ {futures[fut]}: LLM 调用失败({streak}连): {rec.get('error') or ''}",
                    file=sys.stderr,
                    flush=True,
                )
                if streak >= MAX_CONSECUTIVE_LLM_FAIL:
                    print(
                        f"连续 {streak} 条调用失败，疑似断网/服务不可用，中止"
                        "（重跑同一命令即可续）",
                        file=sys.stderr,
                    )
                    aborted = True
                    for pending in futures:
                        pending.cancel()
                    break
            else:
                fail += 1
                streak = 0
            if done_count % 20 == 0 or done_count == len(todo):
                rate = done_count / max(time.time() - t0, 1e-6)
                eta = (len(todo) - done_count) / max(rate, 1e-6) / 60
                print(
                    f"[{done_count}/{len(todo)}] ok={ok} parse_failed={fail} "
                    f"llm_failed={llm_fails} | {rate:.1f}条/s ETA {eta:.0f}分",
                    flush=True,
                )

    print(f"\n{'中止' if aborted else '完成'}: ok={ok} parse_failed={fail} → {out_path}")
    if not args.no_review:
        from labeler.review import export_review

        review_path = export_review(out_path, args.notes, run_name)
        print(f"审核表: {review_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
