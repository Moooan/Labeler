"""Prompt 构建器：多 prompt 复标通过注册新版本函数接入（PROMPTS 注册表）。

改写 prompt 另立 REWRITE_PROMPTS 注册表：输入是「原帖 + 已定稿标签」，
不吃标签体系，输出一行纯文本 query（不走 parse.py 的 JSON 校验）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from labeler.notes import Note
from labeler.taxonomy import Taxonomy

SystemBuilder = Callable[[Taxonomy], str]
UserBuilder = Callable[[Note], str]

MAX_SCENARIOS = 5

_OUTPUT_SCHEMA = """{
  "scenarios": [{"id": "S018", "confidence": 0.9}],
  "intents": ["I02", "I03"],
  "persona": [{"field": "年级", "value": "大三", "basis": "明确", "evidence": "我大三…"}],
  "unlabelable": {"scenario": false, "intent": false, "reason": ""}
}"""


def _scenario_block(tax: Taxonomy) -> str:
    lines = []
    for s in tax.scenarios:
        line = f"{s.id}｜{s.path}"
        if s.definition:
            line += f"｜{s.definition}"
        lines.append(line)
    return "\n".join(lines)


def _intent_block(tax: Taxonomy) -> str:
    lines = []
    for i in tax.intents:
        line = f"{i.id}｜{i.name}｜{i.definition}"
        if i.typical:
            line += f"｜典型表达：{i.typical}"
        lines.append(line)
    return "\n".join(lines)


def _persona_block(tax: Taxonomy) -> str:
    groups: dict[str, list[str]] = {}
    for f in tax.persona_fields:
        groups.setdefault(f.group, []).append(f.name)
    return "\n".join(f"{g}：{'、'.join(names)}" for g, names in groups.items())


def _v1_system(tax: Taxonomy) -> str:
    return f"""你是小红书帖子的标签标注员。给定一篇帖子（标题+正文+话题标签），按下方标签体系输出标注结果。

## 场景标签（共 {len(tax.scenarios)} 个叶节点，格式：ID｜完整路径｜定义）
{_scenario_block(tax)}

## 意图标签（共 {len(tax.intents)} 个，格式：ID｜名称｜定义｜典型表达）
{_intent_block(tax)}

## 用户画像字段（可选标注，按字段组）
{_persona_block(tax)}

## 标注规则
1. 场景：从列表中选**最符合的 1~{MAX_SCENARIOS} 个**，按契合度降序排列；confidence 取 0~1。
   只能使用列表里的 ID。帖子确实涉及该场景即算（本人求助、替人问、讨论、经验分享都算涉及）。
   若帖子与所有场景都不符（如宠物、美食、纯娱乐、广告），或信息太少无法判断，不硬选：
   置 unlabelable.scenario=true 并在 reason 写明原因。
2. 意图：识别**作者发帖的目的**（不是内容主题），可多选、无数量上限，按主要程度排序。
   只能使用 I01~I{len(tax.intents):02d} 的 ID。判断不了时置 unlabelable.intent=true 并写 reason。
3. 用户画像：**不是必标项**。只标帖子里能直接读到或合理推断的字段，宁缺毋滥。
   每个字段给 basis：明确=原文直接陈述；推测=从语境推断（如提到"室友都在实习"推为在校生）。
   evidence 给原文短引文（40 字内）。字段名必须严格使用画像字段表里的名称。
4. 一篇帖子常有多个侧面（挂科+焦虑+要不要考研），场景和意图都应如实多标。
5. 只输出一个 JSON 对象，不要 markdown 代码块、不要解释。结构如下：
{_OUTPUT_SCHEMA}"""


def _v1_user(note: Note) -> str:
    return f"请标注以下帖子：\n\n{note.text_for_prompt()}"


def _cy_v1_system(tax: Taxonomy) -> str:
    return f"""你是 PathPal 产品的标签标注员。给定一条来自《创业1000问》的用户提问（问题原文+所属章节），按下方标签体系输出标注结果。

## 场景标签（共 {len(tax.scenarios)} 个叶节点，格式：ID｜完整路径｜定义）
{_scenario_block(tax)}

## 意图标签（共 {len(tax.intents)} 个，格式：ID｜名称｜定义｜典型表达）
{_intent_block(tax)}

## 用户画像字段（可选标注，按字段组）
{_persona_block(tax)}

## 标注规则
1. 场景：从列表中选**最符合的 1~{MAX_SCENARIOS} 个**，按契合度降序排列；confidence 取 0~1。
   只能使用列表里的 ID。问题确实涉及该场景即算（本人求助、替人问、设想讨论都算涉及）。
   若问题与所有场景都不符，或信息太少无法判断，不硬选：
   置 unlabelable.scenario=true 并在 reason 写明原因。
2. 意图：识别**提问者发问的目的**（不是内容主题），可多选、无数量上限，按主要程度排序。
   只能使用 I01~I{len(tax.intents):02d} 的 ID。判断不了时置 unlabelable.intent=true 并写 reason。
3. 用户画像：**不是必标项**。只标问题里能直接读到或合理推断的字段，宁缺毋滥。
   每个字段给 basis：明确=原文直接陈述；推测=从语境推断（如提到"实验室"推为研究生）。
   evidence 给原文短引文（40 字内）。字段名必须严格使用画像字段表里的名称。
4. 一个问题常有多个侧面（想创业+家里反对+学业压力），场景和意图都应如实多标。
5. 只输出一个 JSON 对象，不要 markdown 代码块、不要解释。结构如下：
{_OUTPUT_SCHEMA}"""


def _cy_v1_user(note: Note) -> str:
    out = f"请标注以下问题：\n\n问题：{note.title}"
    if note.tags:
        out += f"\n所属章节：{note.tags[0]}"
    return out


# 多 prompt 注册表：复标时新增版本（如 v2 加 few-shot）在此登记
PROMPTS: dict[str, tuple[SystemBuilder, UserBuilder]] = {
    "v1": (_v1_system, _v1_user),
    "cy_v1": (_cy_v1_system, _cy_v1_user),  # 创业1000问：纯问题文本（title=问题，tags=章节）
}


def build_prompt(name: str, tax: Taxonomy, note: Note) -> tuple[str, str]:
    try:
        sys_builder, user_builder = PROMPTS[name]
    except KeyError as exc:
        raise KeyError(f"未注册的 prompt 版本: {name}（可用: {sorted(PROMPTS)}）") from exc
    return sys_builder(tax), user_builder(note)


# ── 改写 prompt（改写台用）─────────────────────────────────────────
@dataclass(frozen=True)
class RewriteInput:
    """改写 prompt 输入：原帖 + 已确认的最终场景/意图（标签约束改写方向）。"""

    note: Note
    scenario_paths: tuple[str, ...]
    intent_names: tuple[str, ...]


_REWRITE_SYSTEM = """你是 PathPal 产品的 query 改写员。给定一篇小红书原帖（标题+正文+话题标签）和已确认的场景/意图标签，把原帖改写成 PathPal 可用的标准用户查询。

## 规则
1. 只输出一行纯文本 query：不要 JSON、markdown、引号、前后缀说明或任何解释。
2. 第一人称求助口吻，像用户在搜索/咨询框里输入的问题。
3. 开头带关键身份或阶段限定（如「大三」「金融在职」「二战考研」），只用原文出现的信息。
4. 保留具体细节：专业、年级、目标院校/岗位、城市、证书名等；不添加原文没有的假设。
5. 去掉话题标签、表情、寒暄、情绪宣泄和平台用语（uu、救命、家人们等）。
6. 聚焦帖子最核心的诉求，一条 query，不超过 60 字。

## 示例
原帖：uu们救命！！二战跨考计算机还有戏吗[哭惹R] 本科双非机械 #考研 #跨考
改写：本科双非机械专业，二战跨考计算机，想知道上岸可能性和备考建议"""


_REWRITE_SYSTEM_V2 = """你是 PathPal 产品的语料改写员。给定一篇小红书原帖（标题+正文+话题标签）和已确认的场景/意图标签，把原帖改写成 PathPal 可用的一组「背景 context + 标准查询 query」。

## 规则
1. 只输出一个 JSON 对象：{"context": "...", "query": "..."}，不要 markdown 围栏、引号包裹或任何解释。
2. query：第一人称求助口吻的标准用户查询，一行，聚焦帖子最核心的诉求，不超过 60 字；只带问题理解必需的最少限定。
3. context：原帖的清洗版——保留原文的措辞、语气和全部实质信息，不概括、不合并、不改述、不省略细节；标题和正文里的信息都保留（标题是纯口号时可略）。长度不限，原帖有多少保留多少。
4. 清洗只删这些东西：话题标签（#xx#）、表情（emoji 和 [哭惹R] 之类表情码）、@提及、平台呼语（uu、家人们、救命、姐妹们等）、连排的感叹号/问号。数字、时间、地点、证书名、家庭情况、纠结的情绪等只要出现在原文里都算信息，保留。
5. 不添加原文没有的假设。原帖没有实质内容时 context 给空字符串。

## 示例
原帖：uu们救命！！二战跨考计算机还有戏吗[哭惹R] 本科双非机械，绩点2.8，家里想让我考公，纠结好久了每天睡不好 #考研 #跨考
输出：{"context": "二战跨考计算机还有戏吗？本科双非机械，绩点2.8，家里想让我考公，纠结好久了每天睡不好", "query": "二战跨考计算机上岸的可能性有多大，该怎么评估和准备？"}"""


def _rewrite_user(inp: RewriteInput, ask: str) -> str:
    desc = inp.note.desc[:1000] + ("…(截断)" if len(inp.note.desc) > 1000 else "")
    scen = "\n".join(f"- {p}" for p in inp.scenario_paths) or "（无）"
    intents = "、".join(inp.intent_names) or "（无）"
    tags = " ".join(inp.note.tags[:10]) or "（无）"
    return f"""标题：{inp.note.title or '(无)'}
正文：{desc or '(无)'}
话题标签：{tags}

已确认的场景标签：
{scen}
已确认的意图：{intents}

{ask}"""


def _rewrite_v1(inp: RewriteInput) -> tuple[str, str]:
    return _REWRITE_SYSTEM, _rewrite_user(inp, "请把上面的原帖改写成一条标准 query。")


def _rewrite_v2(inp: RewriteInput) -> tuple[str, str]:
    return _REWRITE_SYSTEM_V2, _rewrite_user(
        inp, "请把上面的原帖改写成一组 context + query，按规则输出 JSON。")


_REWRITE_SYSTEM_V3 = """你是 PathPal 产品的 query 改写员。给定一篇小红书原帖（标题+正文+话题标签）和已确认的场景/意图标签，把帖子的核心诉求改写成一个明确的标准问题。

## 规则
1. 只输出一行纯文本问题：不要 JSON、markdown、引号、前后缀说明或任何解释。
2. 必须是明确、具体的问题（通常以问号结尾），能直接当作搜索/咨询输入，如「我该怎么办」「该怎么选」「怎么准备」。
3. 征询大家经验的间接问法要转换成当事人视角的核心问题：「大家是怎么处理的」→「我该怎么处理」；「有同样经历的吗」→「我这种情况该怎么办」。征询经历本身不是问题，背后的困境才是。
4. 可以不是第一人称替自己问，但问题要独立成立、指向明确，保留「大三」「二战考研」这类理解问题必需的关键限定。
5. 保留原文出现的具体细节：专业、年级、目标院校/岗位、城市、证书名等；不添加原文没有的假设。
6. 去掉话题标签、表情、寒暄、情绪宣泄和平台用语（uu、救命、家人们等）。
7. 聚焦帖子最核心的诉求，一个问题，不超过 60 字。

## 示例
原帖：uu们救命！！二战跨考计算机还有戏吗[哭惹R] 本科双非机械 #考研 #跨考
输出：本科双非机械专业，二战跨考计算机，上岸的可能性有多大、该怎么准备？

原帖：有没有姐妹考研期间也天天失眠的，大家都是怎么熬过来的 #考研
输出：考研期间天天失眠，该怎么调整？"""


def _rewrite_v3(inp: RewriteInput) -> tuple[str, str]:
    return _REWRITE_SYSTEM_V3, _rewrite_user(inp, "请把上面的原帖改写成一个明确的标准问题。")


RewriteBuilder = Callable[[RewriteInput], tuple[str, str]]

# 改写 prompt 注册表：调规范时新增版本在此登记
# v1 纯query | v2 context+query 双段 JSON | v3 纯query=明确问题（context 由代码清洗）
REWRITE_PROMPTS: dict[str, RewriteBuilder] = {
    "rewrite_v1": _rewrite_v1,
    "rewrite_v2": _rewrite_v2,
    "rewrite_v3": _rewrite_v3,
}
DEFAULT_REWRITE_PROMPT = "rewrite_v3"


def build_rewrite_prompt(name: str, inp: RewriteInput) -> tuple[str, str]:
    try:
        builder = REWRITE_PROMPTS[name]
    except KeyError as exc:
        raise KeyError(
            f"未注册的改写 prompt 版本: {name}（可用: {sorted(REWRITE_PROMPTS)}）") from exc
    return builder(inp)
