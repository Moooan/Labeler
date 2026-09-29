"""标签体系解析：从 评测1.2标签体系v2.xlsx 读 场景/意图/画像字段。

xlsx 是标签定义的单一事实源，本模块只做结构化，不做业务改写。
复核期发现体系缺口时，往 data/taxonomy_additions.json 加补充叶，
load_taxonomy 自动合并（原 xlsx 不动，重复 ID 报错拒绝覆盖）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADDITIONS = ROOT / "data" / "taxonomy_additions.json"


@dataclass(frozen=True)
class Scenario:
    id: str
    l1: str
    l2: str
    l3: str
    path: str
    definition: str
    business: str
    kb_required: str = ""   # 是否需要知识库（存档元数据，不进 prompt）
    timing: str = ""        # 时机（同上）


@dataclass(frozen=True)
class Intent:
    id: str
    name: str
    definition: str
    typical: str


@dataclass(frozen=True)
class PersonaField:
    group: str
    name: str
    sensitivity: str


@dataclass(frozen=True)
class Taxonomy:
    scenarios: tuple[Scenario, ...]
    intents: tuple[Intent, ...]
    persona_fields: tuple[PersonaField, ...]

    def scenario_ids(self) -> set[str]:
        return {s.id for s in self.scenarios}

    def intent_ids(self) -> set[str]:
        return {i.id for i in self.intents}

    def persona_field_names(self) -> set[str]:
        return {f.name for f in self.persona_fields}

    def scenario_by_id(self, sid: str) -> Scenario | None:
        for s in self.scenarios:
            if s.id == sid:
                return s
        return None


def _cell(value: object) -> str:
    return str(value).strip() if value is not None else ""


def load_taxonomy(xlsx_path: Path,
                  additions_path: Path | None = DEFAULT_ADDITIONS) -> Taxonomy:
    """读三个 sheet：场景标签 / 意图标签 / 用户画像。行不完整即跳过。

    additions_path 默认指向 data/taxonomy_additions.json（存在则合并人工补充叶，
    不存在则跳过）；显式传 None 可禁用合并（如严格对照原表的场景）。
    """
    # 注：此 xlsx 在 read_only=True 流式模式下 iter_rows 返回空，用普通模式
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    try:
        scenarios = _load_scenarios(wb)
        intents = _load_intents(wb)
        persona = _load_persona(wb)
    finally:
        wb.close()
    if not scenarios or not intents:
        raise ValueError(f"标签表不完整: 场景 {len(scenarios)} / 意图 {len(intents)}: {xlsx_path}")
    tax = Taxonomy(scenarios, intents, persona)
    if additions_path is not None and additions_path.exists():
        tax = _merge_additions(tax, additions_path)
    return tax


_UPDATE_FIELDS = ("l1", "l2", "l3", "definition", "business")
_META_FIELDS = ("kb_required", "timing")


def _merge_additions(tax: Taxonomy, additions_path: Path) -> Taxonomy:
    """在原表之上打补丁（原 xlsx 永远不动），按序应用五种键：

    - renames：旧式整叶改名（等价于只改 l2/l3/definition 的 updates，保留兼容）
    - leaves：追加新叶到末尾（ID 重复报错）
    - updates：对已有叶做部分字段补丁，path 一律按 l1>l2>l3 重拼
    - metadata：只补 kb_required/timing 元数据（不进 prompt）
    - deletes：最后统一删除

    ID 未知或字段名拼错直接报错，绝不静默覆盖。意图/画像不动。
    """
    data: Any = json.loads(additions_path.read_text(encoding="utf-8"))
    raw_renames = data.get("renames", []) if isinstance(data, dict) else []
    raw_leaves = data.get("leaves", []) if isinstance(data, dict) else data
    raw_updates = data.get("updates", {}) if isinstance(data, dict) else {}
    raw_metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
    raw_deletes = data.get("deletes", []) if isinstance(data, dict) else []

    by_id = {s.id: s for s in tax.scenarios}
    for d in raw_renames:
        sid = _cell(d.get("id"))
        old = by_id.get(sid)
        if old is None:
            raise ValueError(f"{additions_path}: 要改名的 {sid or '(空id)'} 不在原表里")
        l2 = _cell(d.get("l2")) or old.l2
        l3 = _cell(d.get("l3")) or old.l3
        by_id[sid] = Scenario(old.id, old.l1, l2, l3, f"{old.l1} > {l2} > {l3}",
                              _cell(d.get("definition")) or old.definition, old.business)
    scenarios = tuple(by_id[s.id] for s in tax.scenarios)   # 保持原顺序

    known = set(by_id)
    extra: list[Scenario] = []
    for d in raw_leaves:
        scen = Scenario(
            id=_cell(d.get("id")),
            l1=_cell(d.get("l1")), l2=_cell(d.get("l2")), l3=_cell(d.get("l3")),
            path=_cell(d.get("path")) or " > ".join(
                (_cell(d.get("l1")), _cell(d.get("l2")), _cell(d.get("l3")))),
            definition=_cell(d.get("definition")),
            business=_cell(d.get("business")),
            kb_required=_cell(d.get("kb_required")),
            timing=_cell(d.get("timing")),
        )
        if not scen.id or not scen.l1 or not scen.l3:
            raise ValueError(f"{additions_path}: 补充场景缺 id/l1/l3: {d}")
        if scen.id in known:
            raise ValueError(f"{additions_path}: 补充场景 {scen.id} 与原表重复，禁止覆盖")
        known.add(scen.id)
        extra.append(scen)
    scenarios = scenarios + tuple(extra)

    # updates/metadata 可打在原表叶或补充叶上；dict 插入序即补丁序
    by_id = {s.id: s for s in scenarios}
    for sid, patch in raw_updates.items():
        sid = _cell(sid)
        old = by_id.get(sid)
        if old is None:
            raise ValueError(f"{additions_path}: 要更新的 {sid or '(空id)'} 不在标签体系里")
        bad = {str(k) for k in patch} - set(_UPDATE_FIELDS)
        if bad:
            raise ValueError(f"{additions_path}: updates[{sid}] 有未知字段 {sorted(bad)}"
                             f"（可用: {list(_UPDATE_FIELDS)}）")
        new = replace(old, **{k: _cell(v) for k, v in patch.items()})
        by_id[sid] = replace(new, path=f"{new.l1} > {new.l2} > {new.l3}")

    for sid, patch in raw_metadata.items():
        sid = _cell(sid)
        old = by_id.get(sid)
        if old is None:
            raise ValueError(f"{additions_path}: metadata 的 {sid or '(空id)'} 不在标签体系里")
        bad = {str(k) for k in patch} - set(_META_FIELDS)
        if bad:
            raise ValueError(f"{additions_path}: metadata[{sid}] 有未知字段 {sorted(bad)}"
                             f"（可用: {list(_META_FIELDS)}）")
        by_id[sid] = replace(old, **{k: _cell(v) for k, v in patch.items()})

    for sid in raw_deletes:
        sid = _cell(sid)
        if sid not in by_id:
            raise ValueError(f"{additions_path}: 要删除的 {sid or '(空id)'} 不在标签体系里")
        del by_id[sid]

    scenarios = tuple(by_id.values())   # dict 保序：改名/补丁不动顺序，删除只是去掉
    if scenarios == tax.scenarios:
        return tax
    return Taxonomy(scenarios, tax.intents, tax.persona_fields)


def _load_scenarios(wb: openpyxl.Workbook) -> tuple[Scenario, ...]:
    ws = wb["场景标签"]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        sid = _cell(row[0]) if row else ""
        if not sid:
            continue
        rows.append(
            Scenario(
                id=sid,
                l1=_cell(row[1]) if len(row) > 1 else "",
                l2=_cell(row[2]) if len(row) > 2 else "",
                l3=_cell(row[3]) if len(row) > 3 else "",
                path=_cell(row[4]) if len(row) > 4 else "",
                definition=_cell(row[5]) if len(row) > 5 else "",
                business=_cell(row[6]) if len(row) > 6 else "",
            )
        )
    return tuple(rows)


def _load_intents(wb: openpyxl.Workbook) -> tuple[Intent, ...]:
    ws = wb["意图标签"]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        iid = _cell(row[0]) if row else ""
        if not iid:
            continue
        rows.append(
            Intent(
                id=iid,
                name=_cell(row[1]) if len(row) > 1 else "",
                definition=_cell(row[2]) if len(row) > 2 else "",
                typical=_cell(row[3]) if len(row) > 3 else "",
            )
        )
    return tuple(rows)


def _load_persona(wb: openpyxl.Workbook) -> tuple[PersonaField, ...]:
    ws = wb["用户画像"]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        name = _cell(row[1]) if row and len(row) > 1 else ""
        if not name:
            continue
        rows.append(
            PersonaField(
                group=_cell(row[0]) if len(row) > 0 else "",
                name=name,
                sensitivity=_cell(row[2]) if len(row) > 2 else "",
            )
        )
    return tuple(rows)
