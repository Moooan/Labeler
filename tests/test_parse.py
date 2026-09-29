"""parse 模块测试：容错 JSON、白名单、截断、空值兜底。"""

from __future__ import annotations

import json

from labeler.parse import parse_annotation, strip_fences
from labeler.taxonomy import Taxonomy


def _tax() -> Taxonomy:
    from labeler.taxonomy import Intent, PersonaField, Scenario

    return Taxonomy(
        scenarios=(
            Scenario(f"S{i:03d}", "升学", "考研", f"叶{i}", f"升学 > 考研 > 叶{i}", "", "")
            for i in range(1, 8)
        ),
        intents=(Intent("I01", "信息获取", "", ""), Intent("I02", "评估决策", "", "")),
        persona_fields=(PersonaField("教育背景", "年级", "一般"),),
    )


def test_strip_fences_github_style() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert strip_fences(raw) == '{"a": 1}'


def test_strip_fences_picks_first_balanced_object() -> None:
    raw = '好的，结果如下：{"scenarios": [], "note": "包含 } 符号"} 以上。'
    assert strip_fences(raw) == '{"scenarios": [], "note": "包含 } 符号"}'


def test_parse_ok_basic() -> None:
    out = {
        "scenarios": [{"id": "S001", "confidence": 0.9}, {"id": "S002", "confidence": 1.7}],
        "intents": ["I02", "I02", "I01"],
        "persona": [
            {"field": "年级", "value": "大三", "basis": "明确", "evidence": "我大三"},
            {"field": "婚姻状况", "value": "未婚", "basis": "明确"},
        ],
        "unlabelable": {"scenario": False, "intent": False, "reason": ""},
    }
    ann, err = parse_annotation(json.dumps(out), _tax())
    assert err == "" and ann is not None
    assert [p.id for p in ann.scenarios] == ["S001", "S002"]
    assert ann.scenarios[1].confidence == 1.0  # 超界截断
    assert ann.intents == ["I02", "I01"]  # 去重保序
    assert len(ann.persona) == 1  # 未知字段丢弃
    assert any("婚姻状况" in w for w in ann.warnings)


def test_parse_scenario_cap_truncated() -> None:
    out = {
        "scenarios": [{"id": f"S{i:03d}"} for i in range(1, 8)],
        "intents": ["I01"],
    }
    ann, _ = parse_annotation(json.dumps(out, ensure_ascii=False), _tax())
    assert ann is not None
    assert len(ann.scenarios) == 5
    assert any("截掉" in w for w in ann.warnings)


def test_parse_empty_scenarios_becomes_unlabelable() -> None:
    ann, err = parse_annotation('{"scenarios": [], "intents": ["I01"]}', _tax())
    assert err == "" and ann is not None
    assert ann.unlabelable_scenario
    assert not ann.unlabelable_intent


def test_parse_broken_json_returns_error() -> None:
    ann, err = parse_annotation("不是JSON{{", _tax())
    assert ann is None
    assert "JSON 解析失败" in err


def test_parse_bad_basis_corrected() -> None:
    out = {
        "scenarios": [{"id": "S001"}],
        "intents": ["I01"],
        "persona": [{"field": "年级", "value": "大三", "basis": "肯定是"}],
    }
    ann, _ = parse_annotation(json.dumps(out, ensure_ascii=False), _tax())
    assert ann is not None
    assert ann.persona[0].basis == "推测"
