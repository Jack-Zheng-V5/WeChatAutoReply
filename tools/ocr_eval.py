"""M2 气泡 OCR 评估工具（零点击，DESIGN.md §10-M2）。

流程：捕获微信窗口 → 全窗 OCR（对照）→ 绿色气泡分割 → 逐气泡 3x 放大 OCR →
顶栏联系人名识别。只输出日志与截图，不点击、不输入（铁律 4/5）。

用法：python tools/ocr_eval.py [--chat-x 388]
chat-x = 会话列表与聊天区的分栏 x（随窗口宽度变化；分栏自动检测是 M2 待办，
当前人工查看 captures/ 最新 raw 图后指定）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vision import bubble, capture, layout, ocr  # noqa: E402

CAPTURE_DIR = ROOT / "captures"
LOG_DIR = ROOT / "logs"
NAME_BAND = (35, 80)  # 顶栏联系人名 y 波段（历史保留，现用 layout.chat_name_region）


def setup_logging() -> Path:
    CAPTURE_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"ocr_eval_{datetime.now():%Y%m%d_%H%M%S}.log"
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
    parser = argparse.ArgumentParser(description="M2 气泡 OCR 评估（零点击）")
    parser.add_argument("--chat-x", type=int, default=0,
                        help="聊天区左边界 x；0 或缺省 = 自动检测（分栏检测失败时才需手工指定）")
    args = parser.parse_args()

    log_file = setup_logging()
    log = logging.getLogger("ocr_eval")
    log.info("=== M2 OCR 评估开始，日志: %s ===", log_file)
    capture.set_dpi_aware()

    wins = capture.find_wechat_windows()
    win = capture.pick_main_window(wins)
    if win is None or capture.is_iconic(win.hwnd):
        log.error("微信窗口不可用（未运行或已最小化）")
        return 1
    cap = capture.capture_window(win.hwnd)
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = CAPTURE_DIR / f"{tag}_raw.png"
    cv2.imwrite(str(raw_path), cap.image)
    log.info("捕获 %dx%d → %s", cap.width, cap.height, raw_path.name)
    chat_x = args.chat_x or layout.detect_chat_split(cap.image)
    log.info("聊天区左边界 x = %d%s", chat_x,
             "（自动检测）" if not args.chat_x else "（手工指定）")

    # ① 全窗 OCR 对照
    t0 = datetime.now()
    full_lines = ocr.ocr_image(cap.image)
    log.info("全窗 OCR：%d 行（chat_x=%d 过滤后看聊天区）", len(full_lines), chat_x)
    for ln in full_lines:
        if ln.x >= chat_x - 10:
            log.info("  全窗 (%4d,%4d) %.3f  %s", ln.x, ln.y, ln.score, ln.text)

    # ② 顶栏联系人名（白名单匹配目标）
    name_lines = ocr.ocr_image(layout.chat_name_region(cap.image, chat_x), upscale=2.0)
    name_text = ocr.merged_text(name_lines)
    name_score = ocr.avg_score(name_lines)
    log.info("顶栏联系人名: %r (avg=%.3f)", name_text, name_score)

    # ③ 绿色气泡分割 + 逐气泡 3x OCR
    cfg = bubble.BubbleConfig(chat_x=chat_x)
    rects = bubble.find_green_bubbles(cap.image, cfg)
    log.info("绿色气泡: %d 个", len(rects))
    annotated = cap.image.copy()
    for i, rect in enumerate(rects):
        crop = bubble.crop_bubble(cap.image, rect)
        crop_path = CAPTURE_DIR / f"{tag}_bubble{i}.png"
        cv2.imwrite(str(crop_path), crop)
        lines = ocr.ocr_image(crop, upscale=3.0)
        text = ocr.merged_text(lines)
        score = ocr.avg_score(lines)
        log.info("  气泡#%d %s avg=%.3f 文本=%r", i, rect, score, text)
        x, y, w, h = rect
        cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(annotated, str(i), (x - 6, y - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    ann_path = CAPTURE_DIR / f"{tag}_annotated.png"
    cv2.imwrite(str(ann_path), annotated)
    log.info("标注图 → %s", ann_path.name)
    log.info("=== 评估结束（耗时 %s） ===", datetime.now() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
