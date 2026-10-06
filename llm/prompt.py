"""提示词构建（DESIGN.md §6）：全局默认人设 + 每联系人可覆盖 + 历史滑动窗口。

apply_signature：AI 回复标志（用户 2026-09-27 需求）——程序层强制附加，
不依赖 LLM 自觉（glm-4-flash 对格式指令的遵循不稳定）。
"""
from __future__ import annotations

import re

# 常见 emoji/符号块（含变体选择符与 ZWJ）：用于剥掉 LLM 自带的结尾 emoji 再换上标志
_TRAILING_EMOJI = re.compile(
    r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\s]+$"
)

DEFAULT_SYSTEM = (
    "你是微信好友的自动回复助手，替账号主人回复 1 对 1 文字消息。要求：\n"
    "1. 语气自然口语化，像本人随手回复，简短（一般不超过两三句话）；\n"
    "2. 只基于对话上下文回复，不编造事实，不替主人做重大承诺；\n"
    "3. 涉及退款、售后纠纷、投诉、报价、合同、法律等事务，回复'这个我确认一下再答复您'并建议联系人工；\n"
    "4. 对方如果发来图片、语音、表情包等（你只会看到文字描述），礼貌回应即可；\n"
    "5. 不输出任何多余解释、引号或前缀，只输出要发送的回复本身。"
)


def apply_signature(text: str, signature: str) -> str:
    """确保回复以 signature 结尾：剥掉 LLM 自带的结尾 emoji 再附加标志。

    signature 为空时原样返回（功能关闭）。
    """
    if not signature:
        return text
    stripped = _TRAILING_EMOJI.sub("", text.rstrip())
    return stripped + signature


def build_messages(
    system_prompt: str,
    contact_prompt: str | None,
    history: list[dict],
    incoming_text: str,
) -> list[dict]:
    """组装 chat completions 的 messages。

    system = 全局默认（或 config 覆盖）+ 该联系人个性化提示词（可覆盖人设）；
    history = state 层滑动窗口（role: user/assistant）；
    最后一条 = 本轮来信。
    """
    system = system_prompt.strip() or DEFAULT_SYSTEM
    if contact_prompt and contact_prompt.strip():
        system += f"\n\n关于当前联系人「回复风格/业务说明」：{contact_prompt.strip()}"
    messages = [{"role": "system", "content": system}]
    for item in history:
        role = item.get("role")
        text = (item.get("text") or "").strip()
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": incoming_text})
    return messages
