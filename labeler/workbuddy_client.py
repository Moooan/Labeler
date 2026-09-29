"""WorkBuddy 云服务 LLM 客户端（免密钥，走 publicConfig 的 publishableKey）。

无官方 Python SDK，线上协议依 @tencent-ai/workbuddy-cloud-sdk 源码复刻：
- POST {endpoint}/.cloud/llm/chat/completions
- 头: x-wb-webapp-access-key: <publishableKey>、Accept: text/event-stream
- 仅支持流式（stream: true 必传），SSE data: 帧累积 delta.content，[DONE] 结束
- 每条请求必须以 system 消息开头，否则服务端拒绝

publishableKey 仅标识应用、无独立权限（服务端校验 Origin），但仍不打印、不写日志。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from labeler.glm import LLMError

LLM_CHAT_PATH = "/.cloud/llm/chat/completions"
RETRYABLE_STATUS = {429, 500, 502, 503}


def load_public_config(path: Path) -> dict[str, str]:
    """读取云服务 publicConfig（endpoint + publishableKey），来自 activate 结果。"""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LLMError(f"云服务配置读取失败: {path}: {exc}") from exc
    endpoint = str(cfg.get("endpoint", "")).rstrip("/")
    key = str(cfg.get("publishableKey", ""))
    if not endpoint.startswith("https://") or not key.startswith("wbpk_"):
        raise LLMError(f"云服务配置不完整: {path}（需要 endpoint 与 publishableKey）")
    return {"endpoint": endpoint, "publishableKey": key}


def _collect_sse_content(resp: requests.Response) -> str:
    """从 SSE 流中累积 delta.content；error 帧直接抛错。"""
    parts: list[str] = []
    for raw in resp.iter_lines():
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            raise LLMError(f"SSE error 帧: {str(chunk['error'])[:200]}")
        choices = chunk.get("choices") or []
        delta = choices[0].get("delta") if choices else None
        content = (delta or {}).get("content")
        if content:
            parts.append(str(content))
    return "".join(parts)


@dataclass
class WorkBuddyClient:
    """WorkBuddy 云服务 LLM 客户端（ChatClient 协议兼容）。"""

    endpoint: str
    publishable_key: str
    model: str = "glm-5.3-flash"
    timeout: float = 180.0
    retries: int = 3
    min_delay: float = 0.4
    max_delay: float = 1.2

    def __post_init__(self) -> None:
        self._rng = random.Random()
        self._url = f"{self.endpoint}{LLM_CHAT_PATH}"

    def chat(
        self, system: str, user: str, *, temperature: float = 0.1, max_tokens: int = 600
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = {
            "x-wb-webapp-access-key": self.publishable_key,
            "Origin": self.endpoint,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        last_err = ""
        for attempt in range(self.retries + 1):
            try:
                resp = requests.post(
                    self._url, json=payload, headers=headers,
                    timeout=self.timeout, stream=True,
                )
            except requests.RequestException as exc:
                last_err = f"网络异常: {exc}"
                time.sleep(2.0 * (attempt + 1))
                continue

            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                resp.close()
                if resp.status_code in RETRYABLE_STATUS and attempt < self.retries:
                    time.sleep(2.0 * (attempt + 1) + self._rng.uniform(0, 1))
                    continue
                break

            try:
                text = _collect_sse_content(resp)
            except requests.RequestException as exc:
                last_err = f"流读取异常: {exc}"
                resp.close()
                if attempt < self.retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                break
            finally:
                resp.close()  # 幂等，重复 close 安全

            time.sleep(self._rng.uniform(self.min_delay, self.max_delay))  # 成功也限速
            if not text.strip():
                raise LLMError("SSE 流结束但无正文（可能 max_tokens 被 thinking 耗尽）")
            return text

        raise LLMError(f"WorkBuddy云LLM调用失败(model={self.model}): {last_err}")
