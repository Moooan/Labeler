# 服务端口说明

## 8890：原始评测与改写台

启动：

```powershell
python -m labeler.review_server --host 0.0.0.0 --port 8890
```

- `/`：单人场景、意图和画像复核。
- `/adjudicate`：比较两位复核人并处理分歧。
- `/rewrite`：定稿场景并编辑 context、query。
- 主要结果保存在 `data/review_sessions`、`data/adjudications`、`data/rewrites`。

## 8891：context 与 query 审核

启动：

```powershell
python -m labeler.context_preview --port 8891 --workers 8
```

- 编辑并确认 context 和 query，可分别标记不需要。
- 支持知识库内容及三级路径判断；该部分不参与 8892 的场景复审。
- 每条记录独立保存到 `data/context_preview/{note_id}.json`，带版本号避免多人覆盖。

## 8892：改写后场景复审

启动：

```powershell
.\start-scenario-reaudit.ps1
```

- 按 context 在前、query 在后拼成完整用户发言，并重新生成摘要与场景。
- 默认比较改写台中的万芸希与 zlx，只把“两人相同”和“两人分歧”作为主要人工分类。
- 人工提交后从待处理分类移除，保留在“全部记录”和“已人工定稿”。
- context 保留、两位人工一致且 AI 独立结果也一致时，可自动定稿；AI 失败后的人工兜底不参与自动确认。
- 快照、AI 结果、定稿和历史分别保存在 `data/scenario_reaudit` 下，不写回 8890 或 8891。

## 局域网访问

本机直接使用 `http://127.0.0.1:端口/`。局域网访问使用运行服务电脑的 IPv4 地址，例如 `http://172.16.23.148:8892/`。Windows 端口转发脚本只适用于当前记录的 IP 和网段；IP 改变后需要更新脚本中的地址并重新执行。

不要把公网 IP 当作同一 Wi-Fi 地址。若同一局域网仍无法访问，依次检查服务监听状态、Windows 防火墙、无线网络的客户端隔离以及访问设备是否真的处于同一子网。
