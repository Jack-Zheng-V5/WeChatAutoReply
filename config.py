"""配置加载（DESIGN.md §7 config.yaml）。

config.yaml 含白名单等私密信息，不入库（.gitignore）；config.example.yaml 为
入库模板。缺省值集中在本模块，配置文件只需覆盖需要改的项。
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "send_enabled": False,  # 总开关（DESIGN.md §8）：M4 发送路径验证通过前必须 false
    "scan": {
        "interval_seconds": 2.0,  # 扫描间隔（§11 默认 1~2 秒）
        "phash_threshold": 2,     # pHash 汉明距离 ≤ 此值视为画面无变化
        "force_rescan_seconds": 600,  # 定期强制全量重扫：兜底任何未知的画面冻结
                                      # 模式（锁屏/息屏等），把静默漏检上界压到此时长
        "occlusion_wake_seconds": 30,  # 微信被完全覆盖时暂停渲染（2026-09-27 实测）；
                                       # 每 N 秒置顶唤醒（不抢焦点）恢复渲染并扫描
    },
    "send": {
        "retry": 2,           # 发送失败重试次数（§11 默认 2），仍失败转人工
    },
    "ocr": {
        "bubble_min_avg_score": 0.70,      # 闸门 2：长文本阈值
        "bubble_min_avg_score_short": 0.55,  # 闸门 2：短文本阈值（5 个误拦样本证实
                                             # 单行短文本均值系统性偏低，2026-09-26 实施）
        "short_text_max_len": 6,
    },
    "whitelist": {
        "mode": "list",       # list=仅名单内；all_contacts=所有非群聊 1对1（群聊自动排除，
                              # contacts 中 enabled:false 条目充当黑名单）
        "match_ratio": 0.75,  # 模糊匹配阈值（容忍 OCR 认错 1 字，§4）
        "match_all": False,   # 测试模式：所有会话（含群）视为白名单；与 send_enabled 互斥
        "contacts": {},       # 名字 -> {enabled: true, prompt: 个性化提示词（M3）}
    },
    "llm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",  # 智谱 OpenAI 兼容端点（§6）
        "api_key": "",        # 为空 = 不生成回复（仅记录来信）；key 只存本地不入库
        "model": "glm-4-flash",
        "temperature": 0.7,
        "system_prompt": "",  # 全局人设/业务说明；空 = 用 llm/prompt.py 内置默认
        "ctx_max_messages": 20,  # 每联系人上下文窗口（约 8~10 轮，§6）
        "reply_signature": "🤖",  # AI 回复标志：强制附加在每条回复结尾；空 = 不添加
    },
    "safety": {
        "banned_words": ["退款", "退货", "投诉", "报警", "骗子", "律师", "12315", "工商"],
        "reply_min_interval": 30,  # 同联系人最小回复间隔（§11）
        "reply_jitter": 5,         # 随机抖动上限（秒）
        "daily_per_contact": 50,   # 每联系人每日回复上限（§8）
        "daily_global": 200,       # 全局每日回复上限（§8）
        "night_start": 23,         # 夜间免扰 23:00–08:00 只记录不发送（§11）
        "night_end": 8,
    },
}


def load(path: Path) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for key, value in data.items():
            if key in cfg and isinstance(cfg[key], dict) and isinstance(value, dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg
