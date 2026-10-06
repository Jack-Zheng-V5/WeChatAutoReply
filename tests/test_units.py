"""safety / state / llm 层单元测试（AGENTS.md 约定：pytest 只覆盖这三层）。

vision / controller 无法单测，验收方式 = 校准/干跑 + 人工核对（AGENTS.md）。
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config import DEFAULTS
from llm.prompt import DEFAULT_SYSTEM, apply_signature, build_messages
from safety import gate3, gate4, gate5
from safety.audit import AuditLog
from safety.whitelist import Whitelist, is_group_name, is_transient_top_bar
from state.store import StateStore


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.json")


# ── 闸门 3：高危词 ──

def test_gate3_hits_banned_word():
    words = DEFAULTS["safety"]["banned_words"]
    assert gate3.hit("我要退款，你们太差了", words) == "退款"
    assert gate3.hit("请拨打12315投诉", words) in ("12315", "投诉")


def test_gate3_passes_normal_text():
    assert gate3.hit("明天下午三点方便吗", DEFAULTS["safety"]["banned_words"]) is None


def test_gate3_empty_wordlist_never_hits():
    assert gate3.hit("退款", []) is None


def test_gate3_coerces_nonstring_words():
    # YAML 未加引号的数字词会解析成 int（2026-09-26 实测踩坑），必须 coerce
    assert gate3.hit("请拨打12315投诉", ["12315", 12315, None, ""]) == "12315"


# ── 闸门 4：频控 ──

def _cfg(**over):
    cfg = dict(DEFAULTS["safety"])
    cfg.update(over)
    return cfg


def test_gate4_passes_fresh_contact(store):
    assert gate4.evaluate(store, "张三", _cfg()) == []


def test_gate4_blocks_night(store):
    now = datetime(2026, 9, 26, 23, 30)
    reasons = gate4.evaluate(store, "张三", _cfg(), now)
    assert any("夜间免扰" in r for r in reasons)


def test_gate4_blocks_short_interval(store):
    now = datetime(2026, 9, 26, 12, 0, 0)
    store.record_reply("张三", (now - timedelta(seconds=10)).isoformat())
    reasons = gate4.evaluate(store, "张三", _cfg(reply_jitter=0), now)
    assert any("距上次回复" in r for r in reasons)


def test_gate4_passes_after_interval(store):
    now = datetime(2026, 9, 26, 12, 0, 0)
    store.record_reply("张三", (now - timedelta(seconds=60)).isoformat())
    assert gate4.evaluate(store, "张三", _cfg(reply_jitter=0), now) == []


def test_gate4_daily_cap(store):
    now = datetime(2026, 9, 26, 12, 0, 0)
    cfg = _cfg(daily_per_contact=2, daily_global=100, reply_jitter=0)
    for i in range(2):
        store.record_reply("张三", (now - timedelta(minutes=i + 1)).isoformat())
    reasons = gate4.evaluate(store, "张三", cfg, now)
    assert "联系人日上限" in reasons


# ── 白名单（闸门 1）──

def test_whitelist_substring_and_fuzzy():
    wl = Whitelist({"文件传输助手": {"enabled": True}, "香梅": {"enabled": True}})
    assert wl.match_name("文件传输助手、").name == "文件传输助手"
    assert wl.match_name("香梅").how == "exact"
    assert wl.match_row("香梅20:45啥时回来").name == "香梅"
    assert wl.match_row("东冠15号业主群 11:09 [43条]") is None


def test_whitelist_disabled_contact_ignored():
    wl = Whitelist({"香梅": {"enabled": False}})
    assert wl.match_name("香梅") is None


def test_whitelist_match_all_mode():
    wl = Whitelist({}, match_all=True)
    assert wl.match_row("任意群聊 12:00 内容").how == "match_all"


# ── 全联系人模式 + 群聊判定（承重墙）──

def test_is_group_name_on_real_group_names():
    # 全部来自 2026-09-20/22 实测日志的群聊顶栏名
    assert is_group_name("【湾区创富圈】会员10群(261)")
    assert is_group_name("东冠15号业主群禁止发广(227)")
    assert is_group_name("北京阳光100社区意客户群(300)")
    assert is_group_name("老李家菜园(6)")
    assert is_group_name("【北京市】郝继贤51群_滴滴...(118)")


def test_is_group_name_on_1on1_names():
    assert not is_group_name("文件传输助手")
    assert not is_group_name("香梅")
    assert not is_group_name("北京114预约挂号")
    assert not is_group_name("")


def test_is_group_name_fail_closed_on_paren_suffix():
    # 宽匹配：OCR 误读数字（261→26I）仍带括号后缀 → 仍判群聊（fail-closed）
    assert is_group_name("东冠15号业主群禁止发广(26I)")
    assert is_group_name("某群（２６１）")  # 全角数字


def test_is_group_name_incident_regression():
    # 2026-09-27 事故回归：全宽顶栏文本，括号段后即使有 OCR 噪声字符也必须判群
    assert is_group_name("北京阳光100社区意客户群(300)")
    assert is_group_name("北京阳光100社区意客户群(300)口角")
    assert is_group_name("北京阳光100社区意客户群(3OO)口")  # 数字误读+尾部噪声


def test_is_group_name_full_bar_1on1_still_pass():
    # 1对1 顶栏全宽文本（可能含图标 OCR 噪声）不得误判为群
    assert not is_group_name("文件传输助手、")
    assert not is_group_name("香梅")


# ── 顶栏瞬态状态文本（2026-09-27 "对方正在输入"假身份事故）──

def test_is_transient_top_bar_typing_indicator():
    # 对方打字时顶栏以状态文本代替会话名（2026-09-27 实测 OCR 读数）
    assert is_transient_top_bar("对方正在输入")
    assert is_transient_top_bar("对方正在输入...")
    assert is_transient_top_bar("对方正在输入。。。")


def test_is_transient_top_bar_real_names_pass():
    # 真实会话名不得误判为瞬态（fail-closed 的另一面：不误伤正常身份）
    assert not is_transient_top_bar("香梅")
    assert not is_transient_top_bar("微信ClawBot")
    assert not is_transient_top_bar("文件传输助手")
    assert not is_transient_top_bar("东冠15号业主群禁止发广(227)")  # 群名（走 is_group_name 分支）
    assert not is_transient_top_bar("")


def test_all_contacts_mode_typing_indicator_not_identity():
    # 事故回归：all_contacts 模式下瞬态文本不得被 match_name 收为身份——
    # 拦截责任在 main.py 调用 is_transient_top_bar 前置跳过，此处保证
    # 事故读数"对方正在输入"若漏拦，至少不出现在黑名单/白名单语义里
    wl = Whitelist({"香梅": {"enabled": True}}, mode="all_contacts")
    assert wl.match_name("香梅").name == "香梅"
    # 漏拦时 match_name 会放行该文本（已知设计），调用方必须前置拦截
    assert wl.match_name("对方正在输入") is not None
    assert not is_group_name("北京114预约挂号")


def test_all_contacts_mode_row_and_name():
    wl = Whitelist({"文件传输助手": {"enabled": True}}, mode="all_contacts")
    assert wl.match_row("陌生客户 14:00 你好").how == "all_contacts"
    m = wl.match_name("陌生客户")
    assert m is not None and m.name == "陌生客户" and m.how == "all_contacts"


def test_all_contacts_blocklist():
    wl = Whitelist({"亲小禾": {"enabled": False}}, mode="all_contacts")
    assert wl.match_name("亲小禾") is None
    assert wl.match_name("亲小禾官方号") is None  # 名单名包含于顶栏名也算黑名单


def test_list_mode_unknown_still_none():
    wl = Whitelist({"香梅": {"enabled": True}}, mode="list")
    assert wl.match_name("陌生客户") is None


# ── state 层 ──

def test_state_fingerprint_roundtrip(store):
    assert not store.seen("香梅", "r1", "c1")
    store.mark_processed("香梅", "r1", "c1")
    assert store.seen("香梅", "r1", "c1")
    assert not store.seen("香梅", "r1", "c2")  # 同区域不同内容不算重复


def test_state_history_window(store):
    for i in range(30):
        store.append_history("香梅", "user", f"m{i}", f"2026-09-26T12:{i:02d}:00")
    window = store.history_window("香梅", 20)
    assert len(window) == 20
    assert window[0]["text"] == "m10" and window[-1]["text"] == "m29"


def test_state_daily_rolls_by_date(store):
    store.record_reply("香梅", "2026-09-25T23:00:00")
    store.record_reply("香梅", "2026-09-26T08:00:00")
    c_count, g_count = store.daily_count("香梅", today="2026-09-26")
    assert (c_count, g_count) == (1, 1)  # 昨日计数已滚动


def test_state_persists_across_instances(tmp_path):
    p = tmp_path / "state.json"
    StateStore(p).mark_processed("香梅", "r", "c")
    assert StateStore(p).seen("香梅", "r", "c")


def test_state_legacy_schema_compatible(tmp_path):
    # M2 存量 state.json 没有 history/频控字段，加载后必须可写（2026-09-26 实测踩坑）
    p = tmp_path / "state.json"
    p.write_text(
        '{"contacts": {"文件传输助手": {"processed": [["a","b"]], "pending_reply": true}}}',
        encoding="utf-8",
    )
    store = StateStore(p)
    assert store.seen("文件传输助手", "a", "b")
    store.append_history("文件传输助手", "user", "你好", "2026-09-26T21:00:00")
    store.record_reply("文件传输助手", "2026-09-26T21:00:00")
    assert store.history_window("文件传输助手", 20)[-1]["text"] == "你好"
    assert store.daily_count("文件传输助手", today="2026-09-26") == (1, 1)


# ── llm 提示词构建 ──

def test_prompt_build_basic():
    msgs = build_messages("", None, [], "你好")
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == DEFAULT_SYSTEM
    assert msgs[-1] == {"role": "user", "content": "你好"}


def test_prompt_contact_override():
    msgs = build_messages("", "你是客服小香，负责口腔预约", [], "你好")
    assert "口腔预约" in msgs[0]["content"]
    assert msgs[0]["content"].startswith(DEFAULT_SYSTEM.split("\n")[0])


def test_prompt_includes_history_and_roles():
    history = [
        {"role": "user", "text": "在吗"},
        {"role": "assistant", "text": "在的"},
        {"role": "junk", "text": "应被过滤"},
        {"role": "user", "text": ""},
    ]
    msgs = build_messages("", None, history, "好的")
    assert [(m["role"], m["content"]) for m in msgs[1:]] == [
        ("user", "在吗"), ("assistant", "在的"), ("user", "好的"),
    ]


def test_prompt_custom_system_replaces_default():
    msgs = build_messages("自定义人设", None, [], "hi")
    assert msgs[0]["content"] == "自定义人设"


# ── AI 回复标志（apply_signature）──

def test_signature_appended():
    assert apply_signature("好的，收到", "🤖") == "好的，收到🤖"


def test_signature_not_duplicated():
    assert apply_signature("好的🤖", "🤖") == "好的🤖"


def test_signature_replaces_trailing_llm_emoji():
    # LLM 自带结尾 emoji（如 😊）被剥掉再换上标志
    assert apply_signature("我会记得的。😊", "🤖") == "我会记得的。🤖"


def test_signature_empty_disabled():
    assert apply_signature("好的 😊", "") == "好的 😊"


def test_signature_keeps_inner_emoji():
    # 只剥结尾 emoji，句中的保留
    assert apply_signature("好😀呀", "🤖") == "好😀呀🤖"


# ── 闸门 5：输入框闭环验证 ──

def test_gate5_pasted_ok_with_ocr_noise():
    # emoji 无法被 OCR（M2 已知限制），相似度仍应达标
    assert gate5.verify_pasted("好的，我会记得的。🤖", "好的，我会记得的。")


def test_gate5_pasted_fails_on_wrong_text():
    assert not gate5.verify_pasted("明天下午三点见", "完全不同的内容")


def test_gate5_cleared_after_enter():
    assert gate5.verify_cleared("好的🤖", "")          # 输入框空 = 已发送
    assert gate5.verify_cleared("好的🤖", "一些残留")   # 残留无关文本也算清空
    assert not gate5.verify_cleared("好的，收到啦🤖", "好的，收到啦🤖")  # 未发送


# ── 审计日志 ──

def test_audit_roundtrip(tmp_path):
    audit = AuditLog(tmp_path / "audit.db")
    audit.log("2026-09-27T09:00:00", "香梅", "在吗", "在的🤖",
              sent=True, block_reason="", model="glm-4-flash", tokens=100,
              elapsed_ms=800)
    audit.log("2026-09-27T09:01:00", "香梅", "退款", "",
              sent=False, block_reason="闸门3:退款")
    rows = audit.query("SELECT contact, sent, block_reason FROM reply_audit ORDER BY id")
    assert rows == [("香梅", 1, ""), ("香梅", 0, "闸门3:退款")]


def test_audit_schema_persists(tmp_path):
    p = tmp_path / "audit.db"
    AuditLog(p).log("2026-09-27T09:00:00", "x", "i", "r", sent=False, block_reason="干跑")
    audit2 = AuditLog(p)  # 重复打开不重建表、不丢数据
    audit2.log("2026-09-27T09:05:00", "x", "i2", "r2", sent=True)
    assert len(audit2.query("SELECT * FROM reply_audit")) == 2
