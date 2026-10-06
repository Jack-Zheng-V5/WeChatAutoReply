"""M2 首次点击验证（铁律 5：唯一目标 = 文件传输助手）。

流程：捕获扫描 → 锁定目标行（行文本必须匹配"文件传输助手"且唯一，
否则拒绝点击）→ §5.1 前台协议带起微信 → 真实鼠标单击该行 → 捕获 →
OCR 顶栏名闭环验证 → 还原前台 → 复扫红点检查副作用（徽标应消失）。

开发测试期点击目标写死为文件传输助手（白名单测试会话），不得改作他用。
"""
from __future__ import annotations

import argparse
import difflib
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from controller import foreground, mouse  # noqa: E402
from vision import capture, layout, ocr, session_list  # noqa: E402

EXPECTED_NAME = "文件传输助手"
NAME_MATCH_RATIO = 0.75
NAME_BAND = (35, 80)  # 顶栏联系人名 y 波段（与 ocr_eval 一致）
CAPTURE_DIR = ROOT / "captures"
LOG_DIR = ROOT / "logs"


def name_ratio(text: str) -> float:
    return difflib.SequenceMatcher(None, text, EXPECTED_NAME).ratio()


def setup_logging() -> Path:
    CAPTURE_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"click_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def main() -> int:
    parser = argparse.ArgumentParser(description="M2 首次点击验证（仅文件传输助手）")
    parser.add_argument("--dry-run", action="store_true", help="只定位目标行，不点击")
    args = parser.parse_args()

    log = logging.getLogger("click")
    log_file = setup_logging()
    log.info("=== M2 点击验证开始（dry_run=%s），日志: %s ===", args.dry_run, log_file)
    capture.set_dpi_aware()

    wins = capture.find_wechat_windows()
    win = capture.pick_main_window(wins)
    if win is None or capture.is_iconic(win.hwnd):
        log.error("微信窗口不可用（未运行或已最小化）")
        return 1

    # ① 点击前捕获与扫描
    before = capture.capture_window(win.hwnd)
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    cv2.imwrite(str(CAPTURE_DIR / f"{tag}_before.png"), before.image)
    split_x = layout.detect_chat_split(before.image)
    lines = ocr.ocr_image(before.image)
    red_rows, rows = session_list.scan_red_rows(before.image, split_x, lines)
    log.info("点击前：分栏 x=%d，会话 %d 行，红点行 %s",
             split_x, len(rows), [rr.row.index for rr in red_rows])

    # ② 锁定目标行：红点行优先，文本必须匹配且唯一，否则拒绝点击
    candidates = [rr.row for rr in red_rows] or rows
    contains = [r for r in candidates if EXPECTED_NAME in r.text]
    if not contains:
        best = max(candidates, key=lambda r: name_ratio(r.text))
        if name_ratio(best.text) < NAME_MATCH_RATIO:
            log.error("无行匹配目标 %r（最高相似 %.2f，%r）——拒绝点击（铁律 5）",
                      EXPECTED_NAME, name_ratio(best.text), best.text[:30])
            return 1
        contains = [best]
    if len(contains) > 1:
        log.error("目标匹配不唯一（%d 行含 %r）——拒绝点击（铁律 5）",
                  len(contains), EXPECTED_NAME)
        return 1
    target = contains[0]
    click_x = session_list.SIDEBAR_W + (split_x - session_list.SIDEBAR_W) // 2
    click_y = (target.y_top + target.y_bottom) // 2
    log.info("目标行锁定：行%d y=%d~%d 文本=%r", target.index, target.y_top,
             target.y_bottom, target.text[:30])

    if args.dry_run:
        log.info("--dry-run：不执行点击")
        return 0

    # ③ 前台协议 + 真实点击 + 帧内捕获
    rect = capture.get_window_rect(win.hwnd)  # 点击前重新获取，防窗口移动
    screen_x, screen_y = rect[0] + click_x, rect[1] + click_y
    log.info("点击 窗口相对(%d,%d) → 屏幕(%d,%d)", click_x, click_y, screen_x, screen_y)
    try:
        with foreground.ForegroundGuard(win.hwnd):
            mouse.left_click(screen_x, screen_y)
            time.sleep(0.8)  # 等微信响应选中
            after = capture.capture_window(win.hwnd)
    except OSError as exc:
        log.error("前台协议中断，未完成点击流程: %s", exc)
        return 1
    cv2.imwrite(str(CAPTURE_DIR / f"{tag}_after.png"), after.image)

    # ④ 闭环验证：顶栏联系人名应为目标名
    after_split = layout.detect_chat_split(after.image)
    name_lines = ocr.ocr_image(after.image[NAME_BAND[0]:NAME_BAND[1], after_split:],
                               upscale=2.0)
    name_text = ocr.merged_text(name_lines)
    verified = EXPECTED_NAME in name_text or name_ratio(name_text) >= NAME_MATCH_RATIO
    log.info("顶栏名闭环验证: %r (相似度 %.2f) → %s",
             name_text, name_ratio(name_text), "PASS" if verified else "FAIL")

    # ⑤ 副作用检查：目标行未读徽标应消失
    after_lines = ocr.ocr_image(after.image)
    red_after, _ = session_list.scan_red_rows(after.image, after_split, after_lines)
    still_red = [
        rr for rr in red_after
        if abs(rr.row.y_top - target.y_top) < 20
    ]
    log.info("点击后红点行: %s；目标行徽标%s", [rr.row.index for rr in red_after],
             "仍在（未清除）" if still_red else "已消失")

    ok = verified and not still_red
    log.info("=== 点击验证 %s ===", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
