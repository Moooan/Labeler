# 新电脑接续说明

请下载 **0929_v2 分支**，不是默认分支。完整数据随代码保存在仓库内。

## Windows 启动

安装 Python 3.12 或更新版本。在项目根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
powershell -ExecutionPolicy Bypass -File .\start-handoff.ps1
```

首次安装依赖需要联网。已有兼容 Python 环境也可通过 `-Python '实际的python.exe路径'` 启动。

- http://127.0.0.1:8890/rewrite ：原始评测与改写记录。
- http://127.0.0.1:8891/ ：context、query 与知识库审核。
- http://127.0.0.1:8892/ ：改写后场景复审和最终定稿。

启动默认不批量重新调用 AI。已保存的记录无需重新生成。继续调用 AI 需要网络及可用的模型服务配置；服务可用性不能随数据迁移保证。8890 若需要 AI，可停止该服务后去掉 `--no-llm` 启动。

## 接续进度

8892 选择“全部记录”或“已人工定稿”可查看已完成记录。输入自己的操作人姓名；姓名保存在浏览器本地，换电脑后需要重新填写。顶部完整用户发言支持“修改拼接内容”，修改后点击“确认修改”，最后在下方“确认并提交”才落盘。

`data/scenario_reaudit/snapshot.json` 是本轮快照，不要删除或重新生成。`decisions` 是最终意见，`history` 是历史版本，`results_v2` 等目录保留 AI 结果。最终修改后的完整发言在 `final.utterance`（定稿文件中的 `utterance`）；没有该字段的旧定稿沿用快照原文。不要只取快照文本而忽略定稿文本。

其他进度保存在 `data/review_sessions*`、`data/adjudications`、`data/rewrites`、`data/context_preview`；标签配置在 `config`、`data/taxonomy_additions.json`，已有导出在 `exports`。旧 context 备份也已包含，但不用于覆盖当前数据。

`handoff-manifest.json` 记录此次交接的数据文件数量、大小和 SHA-256，可用于核对下载完整性。日志、测试临时文件、Python 环境和私人密钥不属于进度备份。

## 局域网与后续交接

新电脑本机使用 127.0.0.1。原来的 172.16.23.148 只属于旧电脑。需要其他设备访问时，按新电脑实际 IPv4 配置监听、端口转发和防火墙；不要直接执行写死旧 IP 的网络脚本。

此系统使用本地文件存储，GitHub 不实时同步。建议交接后只在一台主机写入，同事通过浏览器访问它。再次换机前先提交推送当前数据，再在另一台电脑拉取；多人独立副本同时修改会产生数据冲突。
