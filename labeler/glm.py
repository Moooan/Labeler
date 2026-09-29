"""GLM API 客户端：原生 v4 与 Anthropic 兼容两种协议。

自 Finder/src/finder/common/llm.py 搬迁的快照（2026-09-28），labeler 自包含
不依赖 Finder 仓库；Finder 侧后续改动不会自动同步到这里。

智谱 Coding 订阅套餐的额度只覆盖 Anthropic 兼容端点（/api/anthropic），
原生 v4 端点需要按量付费余额——因此默认走 Anthropic 端点。
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests

V4_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ANTHROPIC_MESSAGES_URL = "https://open.bigmodel.cn/api/anthropic/v1/messages"
RETRYABLE_STATUS = {429, 500, 502, 503}


class LLMError(RuntimeError):
    """GLM 调用失败（含重试耗尽）。"""


class ChatClient(Protocol):
    """各协议客户端需满足的统一接口。"""

    def chat(
        self, system: str, user: str, *, temperature: float = 0.1, max_tokens: int = 600
    ) -> str: ...


def parse_openai_content(data: dict[str, Any]) -> str:
    """从 v4 chat/completions 响应中提取回复文本。"""
    return str(data["choices"][0]["message"]["content"])


def parse_anthropic_content(data: dict[str, Any]) -> str:
    """从 Anthropic Messages 响应提取文本（拼接所有 text 块，忽略 thinking 块）。"""
    blocks = data.get("content") or []
    return "".join(
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    )


def load_env_file(path: Path) -> dict[str, str]:
    """解析极简 KEY=VALUE 文件，# 开头为注释行。"""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("'\"")
    return values


def resolve_api_key(key_file: Path | None = None) -> str:
    """API key 优先级：环境变量 GLM_API_KEY > key 文件（KEY=VALUE）。找不到抛 LLMError。"""
    env_key = os.environ.get("GLM_API_KEY")
    if env_key:
        return env_key
    if key_file is not None:
        file_key = load_env_file(key_file).get("GLM_API_KEY", "")
        if file_key:
            return file_key
    raise LLMError(
        "未找到 GLM_API_KEY：请设置环境变量，或将其写入 "
        f"{key_file or 'labeler/.env'}（格式 GLM_API_KEY=xxx，该文件已 gitignore）"
    )


def _post_with_retries(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    *,
    timeout: float,
    retries: int,
    min_delay: float,
    max_delay: float,
    rng: random.Random,
) -> dict[str, Any]:
    """POST + 429/5xx 指数退避重试，成功返回解析后的 JSON。"""
    last_err = ""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_err = f"网络异常: {exc}"
            time.sleep(2.0 * (attempt + 1))
            continue

        if resp.status_code == 200:
            data: dict[str, Any] = resp.json()
            time.sleep(rng.uniform(min_delay, max_delay))  # 成功也限速，保护配额
            return data

        last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if resp.status_code in RETRYABLE_STATUS and attempt < retries:
            time.sleep(2.0 * (attempt + 1) + rng.uniform(0, 1))
            continue
        break

    raise LLMError(last_err)


@dataclass
class AnthropicClient:
    """Anthropic Messages 协议客户端（用 Coding 套餐额度）。"""

    api_key: str
    model: str = "glm-5.3"
    timeout: float = 60.0
    retries: int = 3
    min_delay: float = 0.4
    max_delay: float = 1.2

    def __post_init__(self) -> None:
        self._rng = random.Random()

    def chat(
        self, system: str, user: str, *, temperature: float = 0.1, max_tokens: int = 600
    ) -> str:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        try:
            data = _post_with_retries(
                ANTHROPIC_MESSAGES_URL,
                headers,
                payload,
                timeout=self.timeout,
                retries=self.retries,
                min_delay=self.min_delay,
                max_delay=self.max_delay,
                rng=self._rng,
            )
        except LLMError as exc:
            raise LLMError(f"Anthropic端点调用失败(model={self.model}): {exc}") from exc
        text = parse_anthropic_content(data)
        if not text.strip():
            raise LLMError(f"响应无文本内容（可能 max_tokens 不足）: {str(data)[:200]}")
        return text


@dataclass
class V4Client:
    """原生 v4 chat/completions 客户端（需按量付费余额）。"""

    api_key: str
    model: str = "glm-5.3"
    timeout: float = 60.0
    retries: int = 3
    min_delay: float = 0.4
    max_delay: float = 1.2

    def __post_init__(self) -> None:
        self._rng = random.Random()

    def chat(
        self, system: str, user: str, *, temperature: float = 0.1, max_tokens: int = 600
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            data = _post_with_retries(
                V4_API_URL,
                headers,
                payload,
                timeout=self.timeout,
                retries=self.retries,
                min_delay=self.min_delay,
                max_delay=self.max_delay,
                rng=self._rng,
            )
        except LLMError as exc:
            raise LLMError(f"v4端点调用失败(model={self.model}): {exc}") from exc
        try:
            return parse_openai_content(data)
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"v4响应结构异常: {str(data)[:200]}") from exc
