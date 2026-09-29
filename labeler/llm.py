"""LLM 客户端工厂：GLM 双协议 + WorkBuddy 云服务免密钥通道。

多模型复标 = 不同 model 名 + 不同协议端点；GLM 客户端在 labeler/glm.py
（Finder 快照，本仓库自包含）；密钥解析：环境变量 GLM_API_KEY > key 文件；
workbuddy 协议读 config/workbuddy.local.json（publicConfig：endpoint +
publishableKey），本模块绝不打印任何密钥。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from labeler.glm import AnthropicClient, LLMError, V4Client, resolve_api_key
from labeler.workbuddy_client import WorkBuddyClient, load_public_config

WORKBUDDY_CONFIG = Path(__file__).resolve().parents[1] / "config" / "workbuddy.local.json"


class ChatClient(Protocol):
    def chat(
        self, system: str, user: str, *, temperature: float = 0.1, max_tokens: int = 600
    ) -> str: ...


def make_client(
    model: str,
    *,
    protocol: str = "anthropic",
    api_key: str | None = None,
    key_file: Path | None = None,
    timeout: float = 180.0,
    wb_config: Path | None = None,
) -> ChatClient:
    """timeout 默认 180s：思考型模型单条 45~65s，慢网络下 60s 默认值会大量误杀。"""
    if protocol == "workbuddy":
        cfg = load_public_config(wb_config or WORKBUDDY_CONFIG)
        return WorkBuddyClient(
            endpoint=cfg["endpoint"], publishable_key=cfg["publishableKey"],
            model=model, timeout=timeout,
        )
    key = api_key or resolve_api_key(key_file)
    if protocol == "v4":
        return V4Client(api_key=key, model=model, timeout=timeout)
    return AnthropicClient(api_key=key, model=model, timeout=timeout)


__all__ = [
    "AnthropicClient",
    "ChatClient",
    "LLMError",
    "V4Client",
    "WorkBuddyClient",
    "make_client",
]
