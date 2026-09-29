"""模型输出解析与校验：容错 JSON、ID 白名单、规则裁剪（场景≤5 等）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from labeler.prompts import MAX_SCENARIOS
from labeler.taxonomy import Taxonomy

VALID_BASIS = ("明确", "推测")


@dataclass
class ScenarioPick:
    id: str
    confidence: float


@dataclass
class PersonaPick:
    field: str
    value: str
    basis: str
    evidence: str


@dataclass
class Annotation:
    scenarios: list[ScenarioPick] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    persona: list[PersonaPick] = field(default_factory=list)
    unlabelable_scenario: bool = False
    unlabelable_intent: bool = False
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


def strip_fences(text: str) -> str:
    """去掉 ```json ... ``` 围栏与前后杂文，取第一个平衡的 JSON 对象。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start = t.find("{")
    if start == -1:
        return t
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(t[start:], start):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
        elif ch == '"' and not esc:
            in_str = not in_str
        elif not in_str and ch == "{":
            depth += 1
        elif not in_str and ch == "}":
            depth -= 1
            if depth == 0:
                return t[start : i + 1]
    return t[start:]


def parse_annotation(raw: str, tax: Taxonomy) -> tuple[Annotation | None, str]:
    """解析模型原始输出。返回 (annotation, error)；error 非空表示整体解析失败。"""
    text = strip_fences(raw)
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败: {exc}: {text[:120]}"
    if not isinstance(data, dict):
        return None, f"输出不是 JSON 对象: {str(data)[:120]}"

    ann = Annotation()
    scenario_ids = tax.scenario_ids()
    intent_ids = tax.intent_ids()
    field_names = tax.persona_field_names()

    for item in data.get("scenarios") or []:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            ann.warnings.append(f"场景项格式异常: {str(item)[:40]}")
            continue
        sid = str(item.get("id") or "").strip()
        if sid not in scenario_ids:
            ann.warnings.append(f"丢弃未知场景ID: {sid}")
            continue
        if any(p.id == sid for p in ann.scenarios):
            continue
        try:
            conf = min(max(float(item.get("confidence", 0.5)), 0.0), 1.0)
        except (TypeError, ValueError):
            conf = 0.5
        ann.scenarios.append(ScenarioPick(id=sid, confidence=round(conf, 2)))
    if len(ann.scenarios) > MAX_SCENARIOS:
        dropped = len(ann.scenarios) - MAX_SCENARIOS
        ann.scenarios = ann.scenarios[:MAX_SCENARIOS]
        ann.warnings.append(f"场景超过 {MAX_SCENARIOS} 个，截掉后 {dropped} 个")

    for iid in data.get("intents") or []:
        iid = str(iid).strip()
        if iid in intent_ids and iid not in ann.intents:
            ann.intents.append(iid)
        elif iid not in intent_ids:
            ann.warnings.append(f"丢弃未知意图ID: {iid}")

    for item in data.get("persona") or []:
        if not isinstance(item, dict):
            continue
        fname = str(item.get("field") or "").strip()
        if fname not in field_names:
            ann.warnings.append(f"丢弃未知画像字段: {fname}")
            continue
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        basis = str(item.get("basis") or "").strip()
        if basis not in VALID_BASIS:
            ann.warnings.append(f"画像 {fname} basis 非法({basis})，按推测处理")
            basis = "推测"
        ann.persona.append(
            PersonaPick(
                field=fname,
                value=value[:60],
                basis=basis,
                evidence=str(item.get("evidence") or "")[:80],
            )
        )

    unl = data.get("unlabelable")
    if isinstance(unl, dict):
        ann.unlabelable_scenario = bool(unl.get("scenario"))
        ann.unlabelable_intent = bool(unl.get("intent"))
        ann.reason = str(unl.get("reason") or "")[:200]
    if not ann.scenarios and not ann.unlabelable_scenario:
        ann.unlabelable_scenario = True
        ann.reason = ann.reason or "模型未返回任何场景"
        ann.warnings.append("场景为空且未声明无法标注，按无法标注处理")
    if not ann.intents and not ann.unlabelable_intent:
        ann.unlabelable_intent = True
        ann.reason = ann.reason or "模型未返回任何意图"
        ann.warnings.append("意图为空且未声明无法标注，按无法标注处理")

    return ann, ""
