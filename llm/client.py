"""OpenAI 兼容 chat completions 客户端（DESIGN.md §6）。

适配层 = 可配 base_url + api_key + model，覆盖智谱 GLM / DeepSeek / Kimi / 通义 /
Ollama。实现用 stdlib urllib（零新依赖——cp314 轮子教训，见 Progress.md）；
失败必须显式抛出（绝不静默，铁律 2），由调用方转人工。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30


def chat(base_url: str, api_key: str, model: str, messages: list[dict],
         temperature: float) -> tuple[str, int | None]:
    """一次对话补全，返回（助手回复文本, 总 token 数）。任何失败抛异常。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {"model": model, "messages": messages, "temperature": temperature}
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"LLM 网络失败: {exc}") from exc

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM 响应格式异常: {str(body)[:300]}") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"LLM 返回空内容: {str(body)[:300]}")
    usage = body.get("usage", {})
    tokens = usage.get("total_tokens")
    logger.info("LLM 完成: model=%s tokens=%s", model, tokens if tokens else "?")
    return content.strip(), (int(tokens) if tokens else None)
