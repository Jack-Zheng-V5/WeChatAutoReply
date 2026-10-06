"""微信自动回复主循环（DESIGN.md §3 管线编排）。

M4：识别 + LLM 生成 + 五道闸门 + 审计日志。send_enabled=false 时为干跑
（回复只写日志，铁律 4）；=true 时真实发送（§5 链路，闸门 5 闭环验证）。
串行单队列（铁律 7）；白名单（铁律 5）。
"""
from __future__ import annotations

import argparse
import ctypes
import cv2
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import load as load_config  # noqa: E402
from controller import foreground, mouse  # noqa: E402
from controller import send as sendctl  # noqa: E402
from llm import client as llm_client  # noqa: E402
from llm import prompt as llm_prompt  # noqa: E402
from safety import gate3, gate4, gate5  # noqa: E402
from safety.audit import AuditLog  # noqa: E402
from safety.whitelist import Whitelist, is_group_name, is_transient_top_bar  # noqa: E402
from state.store import StateStore, content_hash, region_hash  # noqa: E402
from vision import bubble, capture, change, layout, ocr, session_list  # noqa: E402
LOG_DIR = ROOT / "logs"
CAPTURE_DIR = ROOT / "captures"

log = logging.getLogger("main")


def setup_logging() -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"main_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


class MainLoop:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.whitelist = Whitelist(
            cfg["whitelist"]["contacts"], cfg["whitelist"]["match_ratio"],
            match_all=bool(cfg["whitelist"].get("match_all")),
            mode=str(cfg["whitelist"].get("mode", "list")),
        )
        self.state = StateStore(ROOT / "data" / "state.json")
        self.audit = AuditLog(ROOT / "data" / "audit.db")
        self.win = None
        self._last_hash = None
        self._last_scan_time = 0.0
        self._last_openchat_check = 0.0
        self._last_occlusion_wake = 0.0
        self._scan_rect = None
        self._was_locked = False
        self._was_occluded = False
        self._dot_fails: dict[tuple[int, int], int] = {}  # 红点位置 → 连续闭环失败次数

    # 同一红点位置闭环连续失败达此次数后，抑制重试并转人工（防止死循环重试）
    DOT_FAIL_LIMIT = 3

    def run(self, once: bool) -> None:
        interval = float(self.cfg["scan"]["interval_seconds"])
        cycle = 0
        while True:
            cycle += 1
            try:
                self.run_once(cycle)
            except Exception:  # 绝不静默：告警 + 记日志后继续跑（铁律 2）
                log.exception("[告警] 周期 %d 异常，继续下一周期", cycle)
            if once:
                break
            time.sleep(interval)

    def run_once(self, cycle: int) -> None:
        # 锁屏是捕获盲区（渲染冻结，画面恒为过期帧）：锁屏告警并空转，解锁强制全量重扫
        locked = capture.is_desktop_locked()
        if locked and not self._was_locked:
            log.error("[告警] 系统已锁屏：窗口渲染冻结，期间新消息无法捕获（漏检风险）；"
                      "解锁后将自动全量重扫。运行期间请勿锁屏")
        if locked:
            self._was_locked = True
            return
        if self._was_locked:
            log.warning("[恢复] 已解锁：跳过画面比对，本周期强制全量重扫")
            self._was_locked = False
            self._last_hash = None

        wins = capture.find_wechat_windows()
        self.win = capture.pick_main_window(wins)
        if self.win is None:
            log.error("[告警] 未找到微信窗口（未运行或未登录）")
            return
        if capture.is_iconic(self.win.hwnd):
            # §3.1：最小化窗口没有渲染表面（硬边界），SW_SHOWNOACTIVATE 无焦点自动还原
            log.warning("[恢复] 微信已最小化 → SW_SHOWNOACTIVATE 自动还原（显示但不抢焦点）")
            ctypes.windll.user32.ShowWindow(self.win.hwnd, 4)  # SW_SHOWNOACTIVATE
            time.sleep(1.0)
            self.win = capture.pick_main_window(capture.find_wechat_windows())
            if self.win is None or capture.is_iconic(self.win.hwnd):
                log.error("[告警] 最小化自动还原失败，请人工检查微信窗口状态")
                return

        # 遮挡唤醒（2026-09-27 实测：微信被完全覆盖时 4.x 暂停渲染，画面冻结 → pHash 盲区，
        # 与锁屏同类；部分可见不冻结）。周期性把微信置顶（不抢焦点）恢复渲染后扫描。
        occluded = capture.is_fully_occluded(self.win.hwnd)
        if occluded and not self._was_occluded:
            log.warning("[遮挡] 微信被完全覆盖：渲染冻结，期间新消息不可见；"
                        "将每 %ds 唤醒一次（置顶不抢焦点）扫描新消息",
                        int(self.cfg["scan"].get("occlusion_wake_seconds", 30)))
        self._was_occluded = occluded
        wake_interval = float(self.cfg["scan"].get("occlusion_wake_seconds", 30))
        if occluded and (time.time() - self._last_occlusion_wake) < wake_interval:
            return  # 静默等待下次唤醒（冻结帧不值得扫描）
        if occluded:
            self._last_occlusion_wake = time.time()
            # 实测（2026-09-27）：仅 SWP_NOACTIVATE 置顶可能不足以让 4.x 恢复渲染，
            # 需要短暂激活窗口（§5.1 同款闪一下）才可靠
            log.info("[遮挡唤醒] 激活微信窗口恢复渲染（焦点闪一下）")
            try:
                with foreground.ForegroundGuard(self.win.hwnd):
                    time.sleep(0.7)  # 激活后等渲染
            except OSError as exc:
                log.error("[告警] 遮挡唤醒激活失败: %s", exc)
                return

        cap = capture.capture_window(self.win.hwnd)
        if not cap.printwindow_ok or capture.is_black_image(
            capture.black_image_metrics(cap.image)
        ):
            log.error("[告警] 捕获无效（PrintWindow 失败或黑图）——WGC 兜底未实现，需人工介入")
            return

        h = change.phash(cap.image)
        force_rescan = int(self.cfg["scan"].get("force_rescan_seconds", 600))
        changed = self._last_hash is None or change.is_changed(
            self._last_hash, h, int(self.cfg["scan"]["phash_threshold"])
        )
        timed_out = (time.time() - self._last_scan_time) > force_rescan
        if not changed and not timed_out:
            return  # 画面无变化，零成本轮
        self._last_hash = h
        self._last_scan_time = time.time()
        log.info("周期 %d：%s，扫描会话列表", cycle,
                 "画面有变化" if changed else f"定期强制重扫（≥{force_rescan}s）")
        self._scan_rect = cap.window_rect  # 供点击前一致性比对（防窗口被移动/缩放后误点）

        split_x = layout.detect_chat_split(cap.image)
        lines = ocr.ocr_image(cap.image)
        red_rows, _rows = session_list.scan_red_rows(cap.image, split_x, lines)
        if not red_rows:
            # §4 补丁（2026-09-26 实测缺口）：消息进入"当前已打开的会话"时微信不出红点。
            # 无红点但画面有变化 → 检查打开会话（节流 5s，避免操作者打字触发高频 OCR）
            if time.time() - self._last_openchat_check >= 5.0:
                self._last_openchat_check = time.time()
                self._process_open_chat(split_x, cap.image)
            return
        # §3-④ 逐个处理本周期全部红点行（开发期非白名单仅忽略不点击）。
        # 点击按消息时间序不改变列表顺序，中途新消息导致漂移的风险由顶栏名闭环验证兜底。
        for rr in red_rows:
            self._process(rr, split_x)

    def _process(self, rr: session_list.RedRow, split_x: int) -> None:
        row = rr.row
        match = self.whitelist.match_row(row.text)
        if match is None:
            log.info("忽略非白名单会话：行%d %r", row.index, row.text[:30])
            return

        log.info("[白名单新消息] %s（行%d，红点 (%d,%d)）→ 点击识别",
                 match.name, row.index, rr.dot.x, rr.dot.y)

        # 防护 1：扫描与点击之间窗口被移动/缩放 → 坐标已失效，本条延后到下周期
        fresh_rect = capture.get_window_rect(self.win.hwnd)
        if fresh_rect != self._scan_rect:
            log.warning("[跳过] 窗口在扫描与点击之间被移动/缩放（%s → %s），本条下周期重扫",
                        self._scan_rect, fresh_rect)
            return
        # 防护 2：同一红点位置连续闭环失败 → 抑制重试、转人工
        dot_key = (rr.dot.x // 16, rr.dot.y // 16)
        if self._dot_fails.get(dot_key, 0) >= self.DOT_FAIL_LIMIT:
            log.error("[告警] 红点 (%d,%d) 已连续失败 %d 次，抑制重试、转人工",
                      rr.dot.x, rr.dot.y, self._dot_fails[dot_key])
            return

        rect = capture.get_window_rect(self.win.hwnd)
        click_x = session_list.SIDEBAR_W + (split_x - session_list.SIDEBAR_W) // 2
        click_y = (row.y_top + row.y_bottom) // 2
        try:
            with foreground.ForegroundGuard(self.win.hwnd):
                mouse.left_click(rect[0] + click_x, rect[1] + click_y)
                time.sleep(0.8)
                after = capture.capture_window(self.win.hwnd)
        except OSError as exc:
            log.error("[告警] 前台协议中断，本条转人工: %s", exc)
            return

        # 闭环验证：先做群聊硬排除（必须用全宽顶栏 OCR——250px 裁剪会截掉群名尾部
        # 成员数后缀，13:17 发进 300 人社区群的事故根因），再按模式取身份
        after_split = layout.detect_chat_split(after.image)
        bar_full = ocr.merged_text(ocr.ocr_image(
            layout.top_bar_full_region(after.image, after_split), upscale=2.0))
        if is_transient_top_bar(bar_full):
            log.warning("[跳过] 顶栏为瞬态状态 %r（对方正在输入代替了会话名），"
                        "身份不可知，本轮不处理、待重扫兜底", bar_full)
            return
        if is_group_name(bar_full):
            log.info("[群聊忽略] 顶栏含成员数后缀 %r，群聊永不回复", bar_full)
            return
        name_lines = ocr.ocr_image(layout.chat_name_region(after.image, after_split),
                                   upscale=2.0)
        top_name = ocr.merged_text(name_lines)
        if is_transient_top_bar(top_name):
            log.warning("[跳过] 顶栏为瞬态状态 %r（对方正在输入代替了会话名），"
                        "身份不可知，本轮不处理、待重扫兜底", top_name)
            return
        verified = self.whitelist.match_name(top_name)
        if self.whitelist.match_all:
            ok = bool(top_name.strip())
            identity = verified.name if verified else top_name.strip()
        elif self.whitelist.mode == "all_contacts":
            if verified is None:
                log.info("[全联系人模式] %r 不可回复（黑名单），忽略", top_name)
                return
            ok = True
            identity = verified.name
        else:
            ok = verified is not None and verified.name == match.name
            identity = match.name
        if not ok:
            self._dot_fails[dot_key] = self._dot_fails.get(dot_key, 0) + 1
            dbg = CAPTURE_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_verify_fail.png"
            cv2.imwrite(str(dbg), after.image)
            log.error("[告警] 点击后顶栏名闭环验证失败：预期 %s，实际 %r —— 转人工（第 %d 次）；截图 %s",
                      match.name, top_name, self._dot_fails[dot_key], dbg.name)
            return
        # 计数语义：只有"新消息成功记录"才清零；打开会话但无新内容（指纹跳过/无文本气泡）
        # 时累计——红点未消除说明它不是未读徽标（如头像红色碎片），达上限后抑制重试
        if self.whitelist.match_all:
            log.info("[测试模式] 会话身份 = %r（取自顶栏名）", identity)

        latest, side = self._find_latest_bubble(after.image, after_split)
        if latest is None:
            log.info("[新消息] %s：未检出文本气泡（图片/语音/视频等媒体消息，仅记录）",
                     identity)
            self._dot_fails[dot_key] = self._dot_fails.get(dot_key, 0) + 1
            self.state.set_pending(identity, True)
            return
        self._handle_reply(identity, side, latest, after.image, after_split, dot_key)

    def _find_latest_bubble(self, image_bgr, split_x: int):
        """绿色=我方文本，白色=对方文本；返回位置最下方的一个（最新）。"""
        green = bubble.find_green_bubbles(image_bgr, bubble.BubbleConfig(chat_x=split_x))
        white = bubble.find_white_bubbles(image_bgr, bubble.WhiteBubbleConfig(chat_x=split_x))
        all_bubbles = [(r, "我方") for r in green] + [(r, "对方") for r in white]
        if not all_bubbles:
            return None, None
        return max(all_bubbles, key=lambda t: t[0][1] + t[0][3])

    def _process_open_chat(self, split_x: int, image_bgr) -> None:
        """§4 红点机制盲区补丁：进入"当前已打开会话"的消息不出红点。

        身份直接取顶栏名（会话已打开，免点击），仅白名单会话进入处理链。
        我方（绿）气泡只对文件传输助手放行——其手机端来消息在 PC 显示为绿色
        （多端同步视角）；其余会话的绿气泡是操作者本人发出，不得触发回复。
        """
        name_lines = ocr.ocr_image(
            layout.chat_name_region(image_bgr, split_x), upscale=2.0
        )
        top_name = ocr.merged_text(name_lines)
        if not top_name.strip():
            return  # 列表态（无打开的会话）
        if is_transient_top_bar(top_name):
            log.warning("[跳过] 打开会话顶栏为瞬态状态 %r（对方正在输入代替了会话名），"
                        "身份不可知，本轮不处理、待重扫兜底", top_name)
            return
        bar_full = ocr.merged_text(ocr.ocr_image(
            layout.top_bar_full_region(image_bgr, split_x), upscale=2.0))
        if is_transient_top_bar(bar_full):
            log.warning("[跳过] 打开会话顶栏为瞬态状态 %r，身份不可知，"
                        "本轮不处理、待重扫兜底", bar_full)
            return
        if is_group_name(bar_full):
            log.info("[群聊忽略] 顶栏含成员数后缀 %r，群聊永不回复", bar_full)
            return  # 群聊永不回复（含打开着的群）
        match = self.whitelist.match_name(top_name)
        if match is None:
            return  # 打开的会话不在白名单
        latest, side = self._find_latest_bubble(image_bgr, split_x)
        if latest is None:
            return
        if side == "我方" and match.name != "文件传输助手":
            return
        self._handle_reply(match.name, side, latest, image_bgr, split_x, None)

    def _handle_reply(self, identity: str, side: str, latest, image_bgr,
                      split_x: int, dot_key) -> None:
        """气泡 OCR → 闸门 2/3/4 → LLM 生成 → 状态落盘（两条入口路径共用）。"""
        if side == "对方":
            # 群聊第二道防线：4.x 群消息的白气泡上方有发送者昵称，1对1 没有此结构
            bx, by, bw, _bh = latest
            band = image_bgr[max(0, by - 20):by, bx:bx + bw]
            above = ocr.merged_text(ocr.ocr_image(band, upscale=2.0))
            if len(above.strip()) >= 2:
                log.info("[群聊特征] 最新对方气泡上方有文字 %r（疑似发送者昵称）→ 按群聊忽略",
                         above)
                return
        blines = ocr.ocr_image(bubble.crop_bubble(image_bgr, latest), upscale=3.0)
        if not blines:
            # OCR 零检出：图片/表情包等媒体气泡被色掩码误收（其缩略图常为浅色），
            # 按 DESIGN.md §1"非文字只记日志不回复"处理；仍计入红点计数防循环
            if dot_key is not None:
                self._dot_fails[dot_key] = self._dot_fails.get(dot_key, 0) + 1
                log.info("[媒体消息-仅记录] %s：最新气泡无文本检出（疑似图片/表情包，计数 %d）",
                         identity, self._dot_fails[dot_key])
            else:
                log.info("[媒体消息-仅记录] %s：最新气泡无文本检出（疑似图片/表情包）", identity)
            return
        text = ocr.merged_text(blines)
        score = ocr.avg_score(blines)
        # 闸门 2：按文本长度分级（短文本单行均值系统性偏低，见 Progress.md 2026-09-26）
        short_len = int(self.cfg["ocr"].get("short_text_max_len", 6))
        threshold = (
            float(self.cfg["ocr"].get("bubble_min_avg_score_short", 0.55))
            if len(text.strip()) <= short_len
            else float(self.cfg["ocr"]["bubble_min_avg_score"])
        )
        if score < threshold:
            log.error("[告警] 气泡 OCR 置信度 %.3f < %.2f（长度 %d），文本=%r —— 转人工",
                      score, threshold, len(text.strip()), text)
            return

        rhash, chash = region_hash(latest), content_hash(text)
        if self.state.seen(identity, rhash, chash):
            if dot_key is not None:
                self._dot_fails[dot_key] = self._dot_fails.get(dot_key, 0) + 1
                log.info("[跳过] %s：已处理指纹（区域 %s / 内容 %s，红点计数 %d）",
                         identity, rhash, chash, self._dot_fails[dot_key])
            else:
                log.info("[跳过] %s：已处理指纹（区域 %s / 内容 %s）",
                         identity, rhash, chash)
            return

        # 闸门 3：来信命中高危词 → 转人工，绝不生成回复（DESIGN.md §8）
        banned = gate3.hit(text, self.cfg["safety"]["banned_words"])
        if banned:
            self.state.mark_processed(identity, rhash, chash)
            self.state.set_pending(identity, True)
            if dot_key is not None:
                self._dot_fails.pop(dot_key, None)
            log.error("[闸门3-高危词] %s 来信命中 %r，转人工、不生成回复: %r",
                      identity, banned, text)
            return

        # 闸门 4：频控判定
        now = datetime.now()
        block_reasons = gate4.evaluate(self.state, identity, self.cfg["safety"], now)
        send_enabled = bool(self.cfg.get("send_enabled"))

        if send_enabled and block_reasons:
            # M4：闸门 4 拦截 = 不生成、不发送、只记录（§8 夜间/频控）
            ts = now.isoformat(timespec="seconds")
            self.state.mark_processed(identity, rhash, chash)
            self.state.set_pending(identity, True)
            if dot_key is not None:
                self._dot_fails.pop(dot_key, None)
            blocks = "；".join(block_reasons)
            self.audit.log(ts, identity, text, "", sent=False,
                           block_reason=f"闸门4:{blocks}")
            log.error("[闸门4-拦截] %s（%s），不生成不发送: %r", identity, blocks, text)
            return

        llm_cfg = self.cfg["llm"]
        if not llm_cfg.get("api_key"):
            self.state.mark_processed(identity, rhash, chash)
            self.state.set_pending(identity, True)
            if dot_key is not None:
                self._dot_fails.pop(dot_key, None)
            log.info("[新消息-已记录待回复] %s（%s气泡，置信度 %.3f）: %r"
                     " —— LLM 未配置（llm.api_key 为空），仅记录",
                     identity, side, score, text)
            return

        t0 = time.perf_counter()
        try:
            history = self.state.history_window(
                identity, int(llm_cfg.get("ctx_max_messages", 20)))
            contact_prompt = self.cfg["whitelist"]["contacts"].get(
                identity, {}).get("prompt")
            messages = llm_prompt.build_messages(
                llm_cfg.get("system_prompt", ""), contact_prompt, history, text)
            reply, tokens = llm_client.chat(
                llm_cfg["base_url"], llm_cfg["api_key"], llm_cfg["model"],
                messages, float(llm_cfg.get("temperature", 0.7)),
            )
            reply = llm_prompt.apply_signature(
                reply, str(llm_cfg.get("reply_signature", "")))
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            self.audit.log(now.isoformat(timespec="seconds"), identity, text, "",
                           sent=False, block_reason=f"LLM失败:{exc}"[:200],
                           model=llm_cfg["model"], elapsed_ms=elapsed_ms)
            log.error("[告警] LLM 生成失败（%s），本条转人工、未入上下文", exc)
            return
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if not send_enabled:
            # ── M3 干跑路径（保持既有行为）+ 审计预演 ──
            ts = now.isoformat(timespec="seconds")
            self.state.mark_processed(identity, rhash, chash)
            self.state.append_history(identity, "user", text, ts)
            self.state.append_history(identity, "assistant", reply, ts)
            self.state.record_reply(identity, ts)  # 干跑模拟发送时间，作闸门 4 数据源
            self.state.set_pending(identity, True)
            if dot_key is not None:
                self._dot_fails.pop(dot_key, None)
            blocks = "；".join(block_reasons) if block_reasons else "通过"
            self.audit.log(ts, identity, text, reply, sent=False, block_reason="干跑",
                           model=llm_cfg["model"], tokens=tokens, elapsed_ms=elapsed_ms)
            log.info("[干跑-回复] %s（%s气泡，置信度 %.3f）", identity, side, score)
            log.info("  来信: %r", text)
            log.info("  回复: %r", reply)
            log.info("  闸门4频控: %s —— 干跑不发送", blocks)
            return

        # ── M4 发送路径：闸门 5 闭环（§5）+ 重试 + 审计 ──
        ok, detail = self._send_reply(reply)
        ts = now.isoformat(timespec="seconds")
        self.state.mark_processed(identity, rhash, chash)
        self.state.append_history(identity, "user", text, ts)
        self.state.append_history(identity, "assistant", reply, ts)
        self.state.record_reply(identity, ts)
        self.state.set_pending(identity, not ok)  # 发送成功 = 不再待回复；失败转人工
        if dot_key is not None:
            self._dot_fails.pop(dot_key, None)
        self.audit.log(ts, identity, text, reply, sent=ok,
                       block_reason=detail if ok else detail, model=llm_cfg["model"],
                       tokens=tokens, elapsed_ms=elapsed_ms)
        if ok:
            log.info("[已发送] %s: %r（%s，%d ms）", identity, reply, detail, elapsed_ms)
        else:
            log.error("[告警] 发送失败（%s），本条转人工；回复内容已入日志与上下文", detail)

    def _send_reply(self, reply: str) -> tuple[bool, str]:
        """M4 发送链路（§5）：聚焦输入框 → 清空 → 剪贴板粘贴 → 闸门 5 粘贴验证
        → Enter → 清空验证 → 剪贴板恢复（铁律 6）。含重试。返回 (ok, detail)。"""
        attempts = 1 + int(self.cfg["send"].get("retry", 2))
        saved: str | None = None
        detail = ""
        paste_delay = 0.4
        try:
            for attempt in range(1, attempts + 1):
                try:
                    with foreground.ForegroundGuard(self.win.hwnd):
                        cap = capture.capture_window(self.win.hwnd)
                        split = layout.detect_chat_split(cap.image)
                        rx, ry, rw, rh = layout.input_box_region(cap.image.shape, split)
                        # 点击坐标 = 窗口相对 + 窗口屏幕偏移；且必须落在窗口矩形内
                        # （2026-09-27 实测教训：漏加偏移导致点击打到窗外其他窗口）
                        rect = capture.get_window_rect(self.win.hwnd)
                        cx, cy = rect[0] + rx + rw // 2, rect[1] + ry + rh // 2
                        if not (rect[0] <= cx < rect[2] and rect[1] <= cy < rect[3]):
                            raise OSError(f"输入框点击坐标 ({cx},{cy}) 不在微信窗口 {rect} 内，拒绝点击")
                        mouse.left_click(cx, cy)  # 聚焦输入框
                        sendctl.clear_input()
                        saved = sendctl.save_clipboard()
                        sendctl.set_clipboard_text(reply)
                        sendctl.paste()
                        time.sleep(paste_delay)
                        cap2 = capture.capture_window(self.win.hwnd)
                        crop = cap2.image[ry:ry + rh, rx:rx + rw]
                        pasted = ocr.merged_text(ocr.ocr_image(crop, upscale=2.0))
                        if not gate5.verify_pasted(reply, pasted):
                            detail = f"闸门5粘贴验证失败（第 {attempt} 次）: 输入框 OCR={pasted[:40]!r}"
                            log.error("[闸门5-失败] %s", detail)
                            continue  # 不按 Enter，未发送
                        sendctl.enter()
                        time.sleep(0.6)
                        cap3 = capture.capture_window(self.win.hwnd)
                        crop3 = cap3.image[ry:ry + rh, rx:rx + rw]
                        left = ocr.merged_text(ocr.ocr_image(crop3, upscale=2.0))
                        if not gate5.verify_cleared(reply, left):
                            detail = f"闸门5清空验证失败（第 {attempt} 次）: 回车后输入框仍有 {left[:40]!r}"
                            log.error("[闸门5-失败] %s", detail)
                            continue
                        return True, "五道闸门全部通过"
                except OSError as exc:
                    detail = f"前台/剪贴板异常（第 {attempt} 次）: {exc}"
                    log.error("[告警] %s", detail)
                    time.sleep(0.5)
            # 全部重试失败：清掉输入框残留（可见的手工状态不留半截文本）
            try:
                with foreground.ForegroundGuard(self.win.hwnd):
                    cap = capture.capture_window(self.win.hwnd)
                    split = layout.detect_chat_split(cap.image)
                    rx, ry, rw, rh = layout.input_box_region(cap.image.shape, split)
                    rect = capture.get_window_rect(self.win.hwnd)
                    mouse.left_click(rect[0] + rx + rw // 2, rect[1] + ry + rh // 2)
                    sendctl.clear_input()
            except OSError as exc:
                log.warning("输入框残留清理失败（不影响判定）: %s", exc)
            return False, detail
        finally:
            try:
                sendctl.restore_clipboard(saved)
            except OSError as exc:
                log.error("[告警] 剪贴板恢复失败（铁律 6）: %s —— 请人工确认剪贴板内容", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="微信自动回复主循环（M4）")
    parser.add_argument("--once", action="store_true", help="只跑一个周期")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--interval", type=float, default=0,
                        help="覆盖配置中的扫描间隔（秒）")
    args = parser.parse_args()

    log_file = setup_logging()
    cfg = load_config(Path(args.config))
    if args.interval > 0:
        cfg["scan"]["interval_seconds"] = args.interval

    # 安全互锁（优先于一切横幅）：全匹配（含群聊）与发送不得同时开启
    if cfg["send_enabled"] and cfg["whitelist"].get("match_all"):
        log.error("安全互斥：whitelist.match_all（全匹配，含群聊）与 send_enabled（发送）"
                  "不得同时开启，否则会向所有会话回复")
        sys.exit(1)

    log.info("=== 主循环启动（M4：五道闸门 + 审计日志；发送开关=%s），日志: %s ===",
             "开" if cfg["send_enabled"] else "关（干跑）", log_file)
    if cfg["send_enabled"]:
        log.warning("=== [全自动模式] send_enabled=true：白名单回复将真实发送，"
                    "五道闸门 + 审计日志生效 ===")
    if cfg["whitelist"].get("mode") == "all_contacts":
        log.warning("=== [全联系人模式] 所有不带成员数后缀的 1对1 会话自动回复"
                    "（群聊自动排除；黑名单=contacts 中 enabled:false）===")
    if cfg["whitelist"].get("match_all"):
        log.warning("=== [测试模式] 白名单已暂时失效（match_all=true）：所有会话都会被"
                    "点击识别，与发送互斥；测试完请改回 false ===")
    log.info("白名单 %d 项，扫描间隔 %.1fs，OCR 置信度阈值 %.2f",
             len(cfg["whitelist"]["contacts"]),
             float(cfg["scan"]["interval_seconds"]),
             float(cfg["ocr"]["bubble_min_avg_score"]))
    MainLoop(cfg).run(once=args.once)


if __name__ == "__main__":
    main()
