"""本机/局域网网页复核：python -m labeler.review_server [--reviewer 默认名] [--host 0.0.0.0]

复核人身份记在各浏览器里（页面右上角填名字，存 localStorage），保存/导出/导入
都按该名字落到 data/review_sessions/{复核人}.json——一个服务多人共用互不干扰，
不再跟服务进程绑定。进度实时落盘，随时「导出Excel」生成跨电脑交换的 xlsx，
「导入Excel」接续别处进度。

跨机器协作（对方无需本 repo）：
- Excel 方式：把导出的任务 xlsx 发给对方，对方用 Excel/WPS 改「复核」页的复核列后发回，
  本机跑 review_tool merge 合并两人的表；
- 局域网方式：--host 0.0.0.0 启动，同一网络的复核人浏览器打开 http://本机IP:端口，
  各自在右上角填自己的名字即可。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from labeler.adjudicate import compare_pair, export_adjudications, load_pair_session
from labeler.llm import ChatClient, LLMError, make_client
from labeler.notes import Note
from labeler.prompts import DEFAULT_REWRITE_PROMPT, RewriteInput, build_rewrite_prompt
from labeler.review import load_annotations
from labeler.review_tool import (
    DEFAULT_NOTES,
    DEFAULT_RUN,
    DEFAULT_XLSX,
    ReviewRecord,
    _default_annotations,
    build_task,
    export_workbook,
    import_workbook,
    machine_intent_ids,
    machine_persona_lines,
    machine_scen_ids,
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
from labeler.taxonomy import Taxonomy, load_taxonomy

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
ADJ_DIR = ROOT / "data" / "adjudications"
REWRITES_DIR = ROOT / "data" / "rewrites"


def safe_reviewer(name: Any) -> str:
    """复核人名 → 安全文件名片段（保留中文/字母/数字/下划线/短横线，≤16 字符）。"""
    s = re.sub(r"[^\w一-鿿-]", "_", str(name or "").strip())
    return s[:16]


_DONE_STATUSES = ("keep", "done", "drop", "insufficient")  # 已完成分诊（跳过不算）


def _done(session: dict[str, Any]) -> int:
    return sum(1 for r in session.values() if r.get("status") in _DONE_STATUSES)


def record_from_dict(d: dict[str, Any]) -> ReviewRecord:
    return ReviewRecord(
        note_id=str(d.get("note_id") or ""),
        scenarios=[str(s) for s in d.get("scenarios") or []],
        intents=[str(i) for i in d.get("intents") or []],
        bad_fit=bool(d.get("bad_fit")),
        new_scenario=str(d.get("new_scenario") or ""),
        persona=[str(p) for p in d.get("persona") or []],
        missing_info=str(d.get("missing_info") or ""),
        comment=str(d.get("comment") or ""),
        status=str(d.get("status") or ""),
        reviewer=str(d.get("reviewer") or ""),
        reviewed_at=str(d.get("reviewed_at") or ""),
    )


class ReviewApp:
    """复核 Web 服务：任务与标签体系全局共享，会话按复核人分文件。"""

    def __init__(self, task: list[tuple[Note, dict[str, Any]]], tax: Taxonomy, run: str,
                 sessions_dir: Path, export_dir: Path, default_reviewer: str = "",
                 adj_dir: Path | None = None, rewrites_dir: Path | None = None,
                 llm_client: ChatClient | None = None, llm_model: str = "",
                 llm_prompt: str = DEFAULT_REWRITE_PROMPT) -> None:
        self.task = task
        self.tax = tax
        self.run = run
        self.sessions_dir = sessions_dir
        self.export_dir = export_dir
        self.default_reviewer = default_reviewer
        self.adj_dir = adj_dir or ADJ_DIR
        self.rewrites_dir = rewrites_dir or REWRITES_DIR
        self.llm_client = llm_client
        self.llm_model = llm_model
        self.llm_prompt = llm_prompt
        self.sessions: dict[str, dict[str, Any]] = {}  # reviewer -> {note_id: record}
        self.adj_sessions: dict[str, dict[str, Any]] = {}  # "左__右" -> {note_id: 裁决}
        self.rw_sessions: dict[str, dict[str, Any]] = {}  # 操作人 -> {note_id: 改写定稿}
        self.app = Flask(__name__)
        self.app.add_url_rule("/", view_func=self.index)
        self.app.add_url_rule("/adjudicate", view_func=self.adjudicate_page)
        self.app.add_url_rule("/rewrite", view_func=self.rewrite_page)
        self.app.add_url_rule("/api/state", view_func=self.state, methods=["GET"])
        self.app.add_url_rule("/api/save", view_func=self.save, methods=["POST"])
        self.app.add_url_rule("/api/export", view_func=self.export, methods=["POST"])
        self.app.add_url_rule("/api/import", view_func=self.import_xlsx, methods=["POST"])
        self.app.add_url_rule("/api/adj/state", view_func=self.adj_state, methods=["GET"])
        self.app.add_url_rule("/api/adj/save", view_func=self.adj_save, methods=["POST"])
        self.app.add_url_rule("/api/adj/export", view_func=self.adj_export, methods=["POST"])
        self.app.add_url_rule("/api/rewrite/state", view_func=self.rw_state, methods=["GET"])
        self.app.add_url_rule("/api/rewrite/save", view_func=self.rw_save, methods=["POST"])
        self.app.add_url_rule("/api/rewrite/draft", view_func=self.rw_draft, methods=["POST"])
        self.app.add_url_rule("/api/rewrite/export", view_func=self.rw_export, methods=["POST"])
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.adj_dir.mkdir(parents=True, exist_ok=True)
        self.rewrites_dir.mkdir(parents=True, exist_ok=True)

    # ── 按复核人分文件会话（原子写）─────────────────────────────────
    def _session(self, reviewer: str) -> dict[str, Any]:
        if reviewer not in self.sessions:
            path = self.sessions_dir / f"{reviewer}.json"
            self.sessions[reviewer] = (json.loads(path.read_text(encoding="utf-8"))
                                       if path.exists() else {})
        return self.sessions[reviewer]

    def _persist(self, reviewer: str) -> None:
        path = self.sessions_dir / f"{reviewer}.json"
        fd, tmp = tempfile.mkstemp(dir=self.sessions_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.sessions.get(reviewer, {}), f, ensure_ascii=False)
        Path(tmp).replace(path)

    def _known_reviewers(self) -> list[str]:
        return sorted(p.stem for p in self.sessions_dir.glob("*.json"))

    # ── 路由 ─────────────────────────────────────────────────────────
    def index(self) -> Any:
        return send_from_directory(STATIC, "review.html")

    def adjudicate_page(self) -> Any:
        return send_from_directory(STATIC, "adjudicate.html")

    # ── 裁决：两位复核人结果的逐条比对 + 最终改写 ─────────────────────
    @staticmethod
    def _side(rec_d: dict[str, Any] | None) -> dict[str, Any] | None:
        if not rec_d:
            return None
        rv = record_from_dict(rec_d)
        return {"scenarios": rv.scenarios, "intents": rv.intents, "status": rv.status,
                "missing_info": rv.missing_info, "comment": rv.comment,
                "bad_fit": rv.bad_fit, "new_scenario": rv.new_scenario,
                "persona": rv.persona, "reviewer": rv.reviewer}

    def _adj_session(self, left: str, right: str) -> dict[str, Any]:
        key = f"{safe_reviewer(left)}__{safe_reviewer(right)}"
        if key not in self.adj_sessions:
            self.adj_sessions[key] = load_pair_session(self.adj_dir / f"{key}.json")
        return self.adj_sessions[key]

    def _adj_persist(self, left: str, right: str) -> None:
        key = f"{safe_reviewer(left)}__{safe_reviewer(right)}"
        path = self.adj_dir / f"{key}.json"
        fd, tmp = tempfile.mkstemp(dir=self.adj_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.adj_sessions.get(key, {}), f, ensure_ascii=False)
        Path(tmp).replace(path)

    def adj_state(self) -> Any:
        left = safe_reviewer(request.args.get("left"))
        right = safe_reviewer(request.args.get("right"))
        notes = []
        if left and right and left != right:
            sa, sb = self._session(left), self._session(right)
            for note, rec in self.task:
                ra_d, rb_d = sa.get(note.note_id), sb.get(note.note_id)
                cmp_ = compare_pair(record_from_dict(ra_d) if ra_d else None,
                                    record_from_dict(rb_d) if rb_d else None, rec)
                notes.append({
                    "note_id": note.note_id, "title": note.title, "desc": note.desc,
                    "machine": self._notes_payload_machine(rec),
                    "a": self._side(ra_d), "b": self._side(rb_d), "cmp": cmp_,
                })
        return jsonify({
            "left": left, "right": right,
            "total": len(self.task), "max_scenarios": 5,
            "scenarios": [{"id": s.id, "path": s.path, "definition": s.definition}
                          for s in self.tax.scenarios],
            "intents": [{"id": i.id, "name": i.name} for i in self.tax.intents],
            "l1s": sorted({s.l1 for s in self.tax.scenarios}),
            "reviewers": self._known_reviewers(),
            "notes": notes,
            "session": self._adj_session(left, right) if left and right else {},
        })

    def _notes_payload_machine(self, rec: dict[str, Any]) -> dict[str, Any]:
        names = {i.id: i.name for i in self.tax.intents}
        scen = []
        for sid in machine_scen_ids(rec):
            s = self.tax.scenario_by_id(sid)
            scen.append({"id": sid, "path": s.path if s else "?"})
        return {"scenarios": scen,
                "intents": [{"id": i, "name": names.get(i, "?")}
                            for i in machine_intent_ids(rec)],
                "persona": machine_persona_lines(rec)}

    def adj_save(self) -> Any:
        d = request.get_json(force=True)
        left = safe_reviewer(d.get("left"))
        right = safe_reviewer(d.get("right"))
        record = d.get("record") or {}
        adjudicator = safe_reviewer(d.get("adjudicator"))
        if not (left and right and left != right):
            return jsonify({"ok": False, "error": "请先选择两位不同的复核人"}), 400
        if not adjudicator:
            return jsonify({"ok": False, "error": "请先填写裁决人名字"}), 400
        nid = str(record.get("note_id") or "")
        if not any(n.note_id == nid for n, _ in self.task):
            return jsonify({"ok": False, "error": f"任务里没有 {nid}"}), 400
        session = self._adj_session(left, right)
        record["adjudicator"] = adjudicator
        record["decided_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        session[nid] = record
        self._adj_persist(left, right)
        done = sum(1 for r in session.values()
                   if r.get("final_status") in ("keep", "drop", "insufficient"))
        return jsonify({"ok": True, "done": done, "total": len(self.task)})

    def adj_export(self) -> Any:
        body = request.get_json(silent=True) or {}
        left = safe_reviewer(body.get("left"))
        right = safe_reviewer(body.get("right"))
        if not (left and right and left != right):
            return jsonify({"ok": False, "error": "请先选择两位不同的复核人"}), 400
        session = self._adj_session(left, right)
        date = datetime.now(UTC).date().isoformat()
        out = (self.export_dir / "裁决" /
               f"最终标注_{left}_{right}_{date}.xlsx")
        path, st = export_adjudications(self.task, self.tax, session, out)
        return jsonify({"ok": True, "path": str(path), **st})

    # ── 改写台：不等双人复核齐，按「已有的最好标签」定稿 + 出改写 query ──
    def rewrite_page(self) -> Any:
        return send_from_directory(STATIC, "rewrite.html")

    def _rw_session(self, operator: str) -> dict[str, Any]:
        if operator not in self.rw_sessions:
            self.rw_sessions[operator] = load_operator_session(
                self.rewrites_dir / f"{operator}.json")
        return self.rw_sessions[operator]

    def _rw_persist(self, operator: str) -> None:
        path = self.rewrites_dir / f"{operator}.json"
        fd, tmp = tempfile.mkstemp(dir=self.rewrites_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.rw_sessions.get(operator, {}), f, ensure_ascii=False)
        Path(tmp).replace(path)

    @staticmethod
    def _rw_done(session: dict[str, Any]) -> int:
        return sum(1 for r in session.values()
                   if r.get("final_status") in ("keep", "drop", "insufficient")
                   and (r.get("final_status") == "drop"
                        or str(r.get("rewrite") or "").strip()))

    def _default_pair(self) -> list[str]:
        """复核进度最多的两位（改写台默认拿他们的结论当建议）。"""
        ranked = sorted(self._known_reviewers(), key=lambda r: -_done(self._session(r)))
        return ranked[:2]

    def rw_state(self) -> Any:
        left = safe_reviewer(request.args.get("left"))
        right = safe_reviewer(request.args.get("right"))
        operator = safe_reviewer(request.args.get("operator"))
        notes: list[dict[str, Any]] = []
        buckets = {"agree": 0, "drop": 0, "conflict": 0, "single": 0, "machine": 0}
        if left and right and left != right:
            sa, sb = self._session(left), self._session(right)
            for reviewer in self._known_reviewers():   # 展示列要齐：所有会话都载入
                self._session(reviewer)
            for note, rec in self.task:
                ra_d, rb_d = sa.get(note.note_id), sb.get(note.note_id)
                ra = record_from_dict(ra_d) if ra_d else None
                rb = record_from_dict(rb_d) if rb_d else None
                cmp_ = compare_pair(ra, rb, rec)
                sug = suggest_final(cmp_, ra, rb, rec, left, right)
                buckets[sug.bucket] += 1
                sides = []
                for reviewer, sess in sorted(self.sessions.items()):
                    d = sess.get(note.note_id)
                    if d is None:
                        continue
                    side = self._side(d) or {}
                    side["session"] = reviewer
                    sides.append(side)
                notes.append({
                    "note_id": note.note_id, "title": note.title, "desc": note.desc,
                    "machine": self._notes_payload_machine(rec),
                    "cmp": cmp_, "suggested": asdict(sug), "sides": sides,
                    "ctx_clean": clean_context(note),   # 前端无草稿时直接带出
                })
        session = self._rw_session(operator) if operator else {}
        rewritten = sum(1 for r in session.values() if str(r.get("rewrite") or "").strip())
        return jsonify({
            "left": left, "right": right, "operator": operator,
            "run": self.run, "total": len(self.task), "max_scenarios": 5,
            "scenarios": [{"id": s.id, "path": s.path, "definition": s.definition}
                          for s in self.tax.scenarios],
            "intents": [{"id": i.id, "name": i.name} for i in self.tax.intents],
            "l1s": sorted({s.l1 for s in self.tax.scenarios}),
            "reviewers": self._known_reviewers(),
            "default_pair": self._default_pair(),
            "notes": notes, "session": session,
            "drafts": load_drafts(self.rewrites_dir / "drafts.jsonl"),
            "llm_ready": self.llm_client is not None,
            "counts": {**buckets, "done": self._rw_done(session), "rewritten": rewritten},
        })

    def rw_save(self) -> Any:
        d = request.get_json(force=True)
        operator = safe_reviewer(d.get("operator"))
        record = d.get("record") or {}
        if not operator:
            return jsonify({"ok": False, "error": "请先填写操作人名字"}), 400
        nid = str(record.get("note_id") or "")
        if not any(n.note_id == nid for n, _ in self.task):
            return jsonify({"ok": False, "error": f"任务里没有 {nid}"}), 400
        if str(record.get("final_status")) not in ("keep", "drop", "insufficient"):
            return jsonify({"ok": False, "error": "分诊未定（保留/不需要/缺少信息）"}), 400
        record["operator"] = operator
        record["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        session = self._rw_session(operator)
        session[nid] = record
        self._rw_persist(operator)
        return jsonify({"ok": True, "done": self._rw_done(session), "total": len(self.task)})

    def rw_draft(self) -> Any:
        if self.llm_client is None:
            return jsonify({"ok": False, "error": "服务未启用 LLM 草稿"
                            "（启动时省略 --no-llm 并配好密钥，或先用 "
                            "python -m labeler.rewrite_batch 批量生成）"}), 400
        d = request.get_json(force=True)
        nid = str(d.get("note_id") or "")
        note = next((n for n, _ in self.task if n.note_id == nid), None)
        if note is None:
            return jsonify({"ok": False, "error": f"任务里没有 {nid}"}), 400
        status = str(d.get("final_status") or "keep")
        sids = [str(s) for s in d.get("scenarios") or []]
        iids = [str(i) for i in d.get("intents") or []]
        paths = []
        for s in sids:
            scen = self.tax.scenario_by_id(s)
            paths.append(scen.path if scen else s)
        names = {i.id: i.name for i in self.tax.intents}
        system, user = build_rewrite_prompt(self.llm_prompt, RewriteInput(
            note=note, scenario_paths=tuple(paths),
            intent_names=tuple(names.get(i, i) for i in iids)))
        try:
            # GLM5.3 thinking 吃 token；再留足余量防「SSE 结束但无正文」的 502
            raw = self.llm_client.chat(system, user, temperature=0.3, max_tokens=4000)
        except LLMError as exc:
            return jsonify({"ok": False, "error": f"LLM 调用失败: {exc}"}), 502
        context = clean_context(note)              # context 一律代码清洗，不走 LLM
        if self.llm_prompt == "rewrite_v2":        # 旧双段 prompt 才从输出里解析 context
            llm_ctx, draft = split_context_query(raw)
            context = llm_ctx or context
        else:
            draft = clean_rewrite_output(raw)
        if not draft:
            return jsonify({"ok": False, "error": "模型返回为空"}), 502
        sig = input_sig(status, sids, iids)
        append_draft(self.rewrites_dir / "drafts.jsonl", {
            "note_id": nid, "draft": draft, "context": context,
            "final_status": status,
            "scenarios": sids, "intents": iids, "input_sig": sig,
            "model": self.llm_model, "prompt": self.llm_prompt,
            "drafted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        })
        return jsonify({"ok": True, "draft": draft, "context": context, "input_sig": sig})

    def rw_export(self) -> Any:
        body = request.get_json(silent=True) or {}
        operator = safe_reviewer(body.get("operator"))
        if not operator:
            return jsonify({"ok": False, "error": "请先填写操作人名字"}), 400
        session = self._rw_session(operator)
        date = datetime.now(UTC).date().isoformat()
        out = self.export_dir / "改写" / f"最终改写_{self.run}_{date}.xlsx"
        path, st = export_rewrites(self.task, self.tax, session, out)
        return jsonify({"ok": True, "path": str(path), **st})

    def _notes_payload(self) -> list[dict[str, Any]]:
        names = {i.id: i.name for i in self.tax.intents}
        out = []
        for note, rec in self.task:
            scen = []
            for sid in machine_scen_ids(rec):
                s = self.tax.scenario_by_id(sid)
                scen.append({"id": sid, "path": s.path if s else "?"})
            out.append({
                "note_id": note.note_id, "title": note.title, "desc": note.desc,
                "machine": {
                    "scenarios": scen,
                    "intents": [{"id": i, "name": names.get(i, "?")}
                                for i in machine_intent_ids(rec)],
                    "persona": machine_persona_lines(rec),
                },
            })
        return out

    def state(self) -> Any:
        reviewer = safe_reviewer(request.args.get("reviewer")) or self.default_reviewer
        return jsonify({
            "reviewer": reviewer,
            "run": self.run,
            "total": len(self.task),
            "max_scenarios": 5,
            "scenarios": [{"id": s.id, "path": s.path, "definition": s.definition}
                          for s in self.tax.scenarios],
            "intents": [{"id": i.id, "name": i.name} for i in self.tax.intents],
            "l1s": sorted({s.l1 for s in self.tax.scenarios}),
            "reviewers": self._known_reviewers(),
            "notes": self._notes_payload(),
            "session": self._session(reviewer) if reviewer else {},
        })

    def save(self) -> Any:
        d = request.get_json(force=True)
        reviewer = safe_reviewer(d.get("reviewer")) or self.default_reviewer
        if not reviewer:
            return jsonify({"ok": False, "error": "请先在右上角填写复核人名字"}), 400
        nid = str(d.get("note_id") or "")
        if not any(n.note_id == nid for n, _ in self.task):
            return jsonify({"ok": False, "error": f"任务里没有 {nid}"}), 400
        d["reviewer"] = reviewer
        d["reviewed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        session = self._session(reviewer)
        session[nid] = d
        self._persist(reviewer)
        return jsonify({"ok": True, "done": _done(session), "total": len(self.task)})

    def export(self) -> Any:
        body = request.get_json(silent=True) or {}
        reviewer = safe_reviewer(body.get("reviewer")) or self.default_reviewer
        if not reviewer:
            return jsonify({"ok": False, "error": "请先在右上角填写复核人名字"}), 400
        session = self._session(reviewer)
        records = {nid: record_from_dict(d) for nid, d in session.items()}
        date = datetime.now(UTC).date().isoformat()
        out = self.export_dir / f"复核任务_{self.run}_{reviewer}_{date}.xlsx"
        export_workbook(self.task, self.tax, records, reviewer, out)
        return jsonify({"ok": True, "path": str(out), "done": _done(session),
                        "total": len(self.task)})

    def import_xlsx(self) -> Any:
        f = request.files.get("file")
        if f is None:
            return jsonify({"ok": False, "error": "没收到文件"}), 400
        reviewer = safe_reviewer(request.form.get("reviewer")) or self.default_reviewer
        if not reviewer:
            return jsonify({"ok": False, "error": "请先在右上角填写复核人名字"}), 400
        fd, tmp = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        tmp_path = Path(tmp)
        f.save(tmp_path)
        try:
            incoming = import_workbook(tmp_path, self.tax)
        finally:
            tmp_path.unlink(missing_ok=True)
        session = self._session(reviewer)
        for nid, rv in incoming.items():
            if any(n.note_id == nid for n, _ in self.task):
                session[nid] = {
                    "note_id": nid, "scenarios": rv.scenarios, "intents": rv.intents,
                    "bad_fit": rv.bad_fit, "new_scenario": rv.new_scenario,
                    "persona": rv.persona, "missing_info": rv.missing_info,
                    "comment": rv.comment,
                    "status": rv.status, "reviewer": rv.reviewer or reviewer,
                    "reviewed_at": rv.reviewed_at,
                }
        self._persist(reviewer)
        return jsonify({"ok": True, "imported": len(incoming),
                        "done": _done(session), "total": len(self.task)})


def _lan_ips() -> list[str]:
    """本机局域网 IP（UDP connect 技巧，不真正发包）。"""
    import socket

    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                ips.add(str(ip))
        finally:
            s.close()
    except OSError:
        pass
    return sorted(ips)


def main() -> int:
    ap = argparse.ArgumentParser(description="网页复核（复核人在页面右上角填名字，多人可共用）")
    ap.add_argument("--reviewer", default="", help="默认复核人名（仅作页面预填，可页面里改）")
    ap.add_argument("--run", default=DEFAULT_RUN)
    ap.add_argument("--annotations", type=Path, default=None)
    ap.add_argument("--notes", type=Path, default=DEFAULT_NOTES)
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--include-unlabeled", action="store_true")
    ap.add_argument("--port", type=int, default=8890)
    ap.add_argument("--host", default="127.0.0.1",
                    help="默认只监听本机；填 0.0.0.0 可让同一局域网的复核人用浏览器直接打标")
    ap.add_argument("--session-dir", type=Path, default=ROOT / "data" / "review_sessions")
    ap.add_argument("--export-dir", type=Path, default=ROOT / "exports" / "复核")
    ap.add_argument("--rewrites-dir", type=Path, default=REWRITES_DIR)
    ap.add_argument("--model", default="glm-5.3-flash", help="改写台 AI 草稿模型")
    ap.add_argument("--prompt", default=DEFAULT_REWRITE_PROMPT,
                    help="改写 prompt 版本（可用: rewrite_v1 纯query / rewrite_v2 context+query）")
    ap.add_argument("--protocol", choices=["anthropic", "v4", "workbuddy"],
                    default="workbuddy", help="AI 草稿走的协议端点")
    ap.add_argument("--no-llm", action="store_true",
                    help="禁用改写台的 AI 草稿按钮（密钥/网络不便时）")
    args = ap.parse_args()

    ann_path = args.annotations or _default_annotations(args.run)
    task = build_task(load_annotations(ann_path), args.notes, args.include_unlabeled)
    tax = load_taxonomy(args.xlsx)
    llm: ChatClient | None = None
    if not args.no_llm:
        try:
            llm = make_client(args.model, protocol=args.protocol)
        except LLMError as exc:  # 密钥/配置缺失不该挡住复核服务本身
            print(f"⚠ AI 草稿不可用（{exc}）；加 --no-llm 可关掉本提示")
    server = ReviewApp(task, tax, args.run, args.session_dir, args.export_dir,
                       safe_reviewer(args.reviewer), rewrites_dir=args.rewrites_dir,
                       llm_client=llm, llm_model=args.model, llm_prompt=args.prompt)
    print(f"任务 {len(task)} 条 | 会话目录 {args.session_dir} | 复核人在页面右上角填名字")
    print(f"→ 复核 http://127.0.0.1:{args.port} | 双人裁决+query改写 "
          f"http://127.0.0.1:{args.port}/adjudicate")
    print(f"→ 改写台（定稿+改写一步完成） http://127.0.0.1:{args.port}/rewrite"
          f"{'' if llm is not None else '（AI 草稿未启用）'}")
    if args.host not in ("127.0.0.1", "localhost"):
        for ip in _lan_ips():
            print(f"→ 局域网复核人打开 http://{ip}:{args.port}（右上角填各自名字即可）")
    server.app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
