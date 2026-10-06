"""闸门 1：会话准入（DESIGN.md §4/§8）。

两种模式（config whitelist.mode）：
- "list"（默认）：仅 contacts 名单内会话通过；
- "all_contacts"：所有**非群聊** 1对1 会话通过（用户 2026-09-27 决定），
  名单内 enabled:false 的条目充当黑名单。

群聊判定（承重墙，fail-closed）：微信 4.x 群聊顶栏名固定带成员数后缀，如
"东冠15号业主群禁止发广(227)"，1对1 会话无此后缀（2026-09-20/22/27 实测，
6+ 个群全部吻合）。采用"任意括号后缀"宽匹配而非仅纯数字——OCR 误读数字
（如 261→26I）时仍能判定为群，宁可漏回个别昵称带括号的 1对1，绝不回进群。

匹配结果必须记日志（来信名 → 匹配到的白名单名，§9 防误伤审计）。
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 群聊判定（v2，2026-09-27 事故后重写）：输入必须是顶栏【全宽】OCR 文本——
# 250px 名字裁剪会截掉长群名尾部的成员数（13:17 发进 300 人社区群的事故根因）。
# 规则：任意位置出现「括号 + 成员数」，成员数允许 OCR 常见误读字符
# （数字、O/o/I/l 及全角数字，如 (261)→(3OO)/(26I)）。微信 4.x 群聊顶栏
# 固定渲染 "群名(N)"，1对1 会话无此结构。
_GROUP_COUNT = re.compile(r"[（(]\s*[0-9０-９OoIlＯｏＩｌ]{1,4}\s*[）)]")


def is_group_name(top_bar_full_text: str) -> bool:
    """顶栏【全宽】OCR 文本判群聊（fail-closed，见上方规则说明）。"""
    return bool(_GROUP_COUNT.search(top_bar_full_text or ""))


# 顶栏瞬态状态文本：对方打字时微信 4.x 以「对方正在输入...」**代替**会话名显示
# （2026-09-27 实测：all_contacts 模式曾把该状态文本当身份放行并真实发送，
# 群聊排除、身份、闸门 4 频控账户全部跑在假身份上）。命中即"身份不可知"。
_TRANSIENT_TOP_BAR = re.compile(r"正在输入")


def is_transient_top_bar(text: str) -> bool:
    """顶栏瞬态状态文本判定（fail-closed）：此刻顶栏不显示会话名，
    一切基于顶栏名的判定（群聊排除、身份匹配、频控记账）均不可信，
    调用方必须跳过本轮、待主循环下周期重扫。"""
    return bool(_TRANSIENT_TOP_BAR.search(text or ""))


@dataclass
class MatchResult:
    name: str    # 白名单条目名，或全联系人模式下取自顶栏名的身份
    ratio: float
    how: str     # exact / substring / fuzzy / all_contacts / match_all


class Whitelist:
    def __init__(self, contacts: dict[str, dict], match_ratio: float = 0.75,
                 match_all: bool = False, mode: str = "list") -> None:
        self._entries = {
            name: conf for name, conf in contacts.items()
            if conf.get("enabled", True)
        }
        self._all_contacts = contacts  # 含停用条目：all_contacts 模式下充当黑名单
        self._ratio = match_ratio
        self.match_all = match_all
        self.mode = mode if mode in ("list", "all_contacts") else "list"
        if match_all:
            logger.warning("[测试模式] whitelist.match_all=true：所有会话（含群聊）"
                           "视为白名单，仅供干跑测试，与 send_enabled 互斥")
        if self.mode == "all_contacts":
            logger.warning("[全联系人模式] 所有不带成员数后缀的 1对1 会话均可自动回复"
                           "（群聊自动排除；黑名单 = contacts 中 enabled:false 的条目）")

    def excluded(self, top_bar_name: str) -> bool:
        """all_contacts 模式的黑名单判定：顶栏名与任一停用条目互相包含。"""
        for name, conf in self._all_contacts.items():
            if not conf.get("enabled", True) and name:
                if name == top_bar_name or name in top_bar_name or top_bar_name in name:
                    return True
        return False

    def match_name(self, top_bar_name: str) -> MatchResult | None:
        """点击后顶栏干净名字的匹配（闭环验证用）。所有分支必须留审计日志。"""
        best: MatchResult | None = None
        for name in self._entries:
            if top_bar_name == name:
                best = self._better(best, MatchResult(name, 1.0, "exact"))
                continue
            if name in top_bar_name:
                best = self._better(best, MatchResult(name, 1.0, "substring"))
                continue
            ratio = difflib.SequenceMatcher(None, top_bar_name, name).ratio()
            if ratio >= self._ratio:
                best = self._better(best, MatchResult(name, ratio, "fuzzy"))
        if best:
            logger.info("白名单匹配（顶栏名）: %r -> %r (%s, %.2f)",
                        top_bar_name, best.name, best.how, best.ratio)
            return best
        if self.mode == "all_contacts" and top_bar_name.strip():
            if self.excluded(top_bar_name):
                logger.info("[全联系人模式] %r 命中黑名单（enabled:false），忽略", top_bar_name)
                return None
            result = MatchResult(name=top_bar_name.strip(), ratio=1.0, how="all_contacts")
            logger.info("白名单匹配（顶栏名）: %r -> 未在名单，全联系人模式身份=%r",
                        top_bar_name, result.name)
            return result
        logger.info("白名单匹配（顶栏名）: %r -> 无匹配", top_bar_name)
        return None

    def match_row(self, row_text: str) -> MatchResult | None:
        """点击前会话行文本的预判。行文本 = 名字+时间+预览拼接，
        采用子串精确或行首模糊（行首即名字，其余为时间/预览干扰）。"""
        if self.match_all:
            logger.info("[测试模式 match_all] 跳过白名单预判：所有会话可点击识别 %r",
                        row_text[:28])
            return MatchResult(name="*", ratio=1.0, how="match_all")
        if self.mode == "all_contacts":
            logger.info("[全联系人模式] 所有会话可点击识别 %r", row_text[:28])
            return MatchResult(name="*", ratio=1.0, how="all_contacts")
        best: MatchResult | None = None
        for name in self._entries:
            if name in row_text:
                best = self._better(best, MatchResult(name, 1.0, "substring"))
                continue
            head = row_text[: len(name) + 4]  # 容忍名字行混入时间等尾部噪声
            ratio = difflib.SequenceMatcher(None, head, name).ratio()
            if ratio >= self._ratio:
                best = self._better(best, MatchResult(name, ratio, "fuzzy"))
        if best:
            logger.info("白名单匹配（行文本）: %r -> %r (%s, %.2f)",
                        row_text[:28], best.name, best.how, best.ratio)
        else:
            logger.info("白名单匹配（行文本）: %r -> 无匹配，忽略", row_text[:28])
        return best

    @staticmethod
    def _better(a: MatchResult | None, b: MatchResult) -> MatchResult:
        if a is None or (b.ratio, len(b.name)) > (a.ratio, len(a.name)):
            return b
        return a
