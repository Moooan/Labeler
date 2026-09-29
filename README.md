# labeler — 标签体系识别

对小红书帖子按《评测1.2标签体系v2.xlsx》三 sheet（场景 219 叶 / 意图 7 类 / 用户画像 37 字段）做 LLM 标注，支持多模型、多 prompt 复标。

## 结构

```
config/runs.json      运行定义（模型 × prompt × 参数），复标=新增条目多次运行
config/评测1.2标签体系v2.xlsx  原始标签体系（Finder 快照，永远不动）
data/xhs_notes.jsonl  待标笔记（Finder 快照）
data/taxonomy_additions.json  标签增改（xlsx 之上的补丁层，taxonomy_sync 维护）
labeler/taxonomy.py   xlsx + additions → 生效标签体系
labeler/taxonomy_sync.py  手改的场景标签 CSV → additions 同步（--apply，删除缓执行）
labeler/prompts.py    prompt 构建器，PROMPTS / REWRITE_PROMPTS 注册表接新版本
labeler/parse.py      输出解析+校验：ID 白名单、场景≤5 截断、画像 basis 校验
labeler/run.py        标注主流程（断点续跑、失败重问一次、自动导审核表）
labeler/review.py     无法标注/解析失败 → data/review/*.xlsx 人工审核
labeler/review_server.py  网页复核 / 双人裁决 / 改写台（/  /adjudicate  /rewrite）
labeler/rewrite.py    改写台核心：建议定稿分桶 + 改写记录 IO + 最终改写导出
labeler/rewrite_batch.py  改写草稿批量生成 + 最终改写导出 CLI
labeler/compare_runs.py  两轮复标一致性（场景/意图 Jaccard）
labeler/glm.py        GLM 客户端（Finder 快照：v4 + Anthropic 双协议）
labeler/workbuddy_client.py  WorkBuddy 云服务客户端（免密钥）
```

自包含仓库：GLM 客户端与密钥约定在 `labeler/glm.py`，评测 xlsx 和笔记数据都
在仓库内（Finder 侧快照），不依赖 `../Finder` 目录。安装：

```bash
cd labeler
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"    # dev 组带 ruff/mypy/pytest 门禁工具
```

## 用法

```bash
# 首轮：glm-5.3-flash 全量
.venv/bin/python -m labeler.run --run glm53flash_v1

# 冒烟 3 条
.venv/bin/python -m labeler.run --run glm53flash_v1 --limit 3

# 复标第二模型，然后对比
.venv/bin/python -m labeler.run --run glm53_v1
.venv/bin/python -m labeler.compare_runs \
  --a data/runs/glm53flash_v1/annotations.jsonl \
  --b data/runs/glm53_v1/annotations.jsonl
```

密钥：环境变量 `GLM_API_KEY`，或 `labeler/.env` 中 `GLM_API_KEY=...`（默认路径，已 gitignore，绝不入库）。

## 网页复核 / 裁决 / 改写台

```bash
.venv/bin/python -m labeler.review_server            # 默认 127.0.0.1:8890
.venv/bin/python -m labeler.review_server --no-llm   # 不配 AI 草稿时
```

- `/` 单人复核：右上角填名字，会话按人分文件 `data/review_sessions/{名字}.json`
- `/adjudicate` 双人裁决：选两位复核人逐条比对（一致/分歧/仅单人）
- `/rewrite` **改写台**：不等复核齐，每条笔记按「双人一致 > 单人复核 > 机器」带出建议定稿，
  定标签 + 写改写（**背景 context + 标准问法 query** 两段）一步完成；操作人会话在
  `data/rewrites/{操作人}.json`，「导出最终改写」→ `exports/复核/改写/最终改写_{run}_{日期}.xlsx`
- 改写分工：**query 走 LLM 批量预生成**（rewrite_v3：把「大家是怎么处理的」这类征询式
  帖子转成明确的标准问题），**context 不走 LLM**——代码对原帖做清洗（去 #tag/表情码/emoji/@、
  砍「二编/三编」追加段、压缩连排标点），原文措辞和信息全保留。推荐流程：先全量批量
  预生成，人工在改写台只做核对和微调

改写草稿可批量预生成（GLM5.3 thinking 吃 token，max-tokens 已默认放大）：

```bash
.venv/bin/python -m labeler.rewrite_batch draft --dry-run   # 看分桶与条数
.venv/bin/python -m labeler.rewrite_batch draft --limit 20  # 试跑
.venv/bin/python -m labeler.rewrite_batch draft             # 全量断点续跑
.venv/bin/python -m labeler.rewrite_batch export --operator 名字
```

草稿落 `data/rewrites/drafts.jsonl`（网页与 CLI 共用，带 input_sig 过期标记，标签变了重跑即可）。

## 场景标签 CSV 同步

手改 `exports/评测1.2标签体系v2 - 场景标签.csv` 后，同步进生效体系（原 xlsx 永远不动）：

```bash
.venv/bin/python -m labeler.taxonomy_sync                  # 只出差异报告
.venv/bin/python -m labeler.taxonomy_sync --apply          # 增/改/元数据生效
.venv/bin/python -m labeler.taxonomy_sync --apply --force-deletes  # 连删除也执行
```

删除默认缓执行：被删 ID 在复核会话/标注里往往还有引用，等最终改写 xlsx 导出后再
`--force-deletes`。同步幂等，重复跑不会丢已写内容。

## 标注规则要点

- **场景**：最多 5 个最符合的叶节点（带 confidence 0~1，降序）；域外/信息不足 → `unlabelable.scenario`
- **意图**：多选无上限（作者发帖目的）；判断不了 → `unlabelable.intent`
- **画像**：非必标；每个字段标 `basis=明确|推测` + 原文引文 evidence，用于区分画像准确性
- 无法标注与解析失败的帖子自动进 `data/review/*_无法标注_*.xlsx` 供人工审核

## 质量门禁

```bash
cd labeler
.venv/bin/ruff check . && .venv/bin/mypy labeler && .venv/bin/pytest
```
