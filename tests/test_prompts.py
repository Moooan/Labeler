"""prompt 构建测试：标签全集注入 + 笔记字段注入。"""

from __future__ import annotations

from labeler.notes import Note
from labeler.prompts import build_prompt
from labeler.taxonomy import Taxonomy


def test_v1_prompt_contains_taxonomy_and_note() -> None:
    from labeler.taxonomy import Intent, PersonaField, Scenario

    tax = Taxonomy(
        scenarios=(
            Scenario("S001", "升学", "考研", "是否考研", "升学 > 考研 > 是否考研", "路径选择", ""),
        ),
        intents=(Intent("I01", "信息获取", "获得事实", "多少钱"),),
        persona_fields=(PersonaField("基础信息", "年龄段", "一般"),),
    )
    note = Note(
        note_id="n1",
        title="求助 考研还是工作",
        desc="我是大三学生，很纠结",
        tags=("考研",),
        raw={},
    )
    system, user = build_prompt("v1", tax, note)
    assert "S001" in system and "升学 > 考研 > 是否考研" in system
    assert "I01" in system and "信息获取" in system
    assert "年龄段" in system
    assert "求助 考研还是工作" in user and "我是大三学生" in user and "#考研" not in user


def test_cy_v1_prompt_renders_question_and_chapter() -> None:
    from labeler.taxonomy import Taxonomy as T

    tax = T((), (), ())
    note = Note(
        note_id="Q001",
        title="我大二,刷到同龄人创业年入百万,该开始吗?",
        desc="我大二,刷到同龄人创业年入百万,该开始吗?",
        tags=("第1章 要不要开始——冲动、时机与身份",),
        raw={},
    )
    system, user = build_prompt("cy_v1", tax, note)
    assert "用户提问" in system and "提问者发问的目的" in system
    assert "问题：我大二" in user
    assert "所属章节：第1章 要不要开始" in user
    # 无章节时也能渲染
    _, user2 = build_prompt("cy_v1", tax, Note("Q2", "没有章节的问题", "", (), {}))
    assert "问题：没有章节的问题" in user2 and "所属章节" not in user2


def test_unknown_prompt_raises() -> None:
    import pytest

    from labeler.taxonomy import Taxonomy as T

    tax = T((), (), ())
    with pytest.raises(KeyError):
        build_prompt("nope", tax, Note("n", "", "", (), {}))
