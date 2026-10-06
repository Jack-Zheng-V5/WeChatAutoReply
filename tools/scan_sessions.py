"""M2 会话行扫描工具（零点击，DESIGN.md §3-③/§4）。

捕获微信窗口 → 分栏自动检测 → 行结构识别 → 红点行扫描。
只输出日志与标注图，不点击、不输入（铁律 4/5）。

用法：
  python tools/scan_sessions.py                 # 实时捕获
  python tools/scan_sessions.py --image captures/xxx_raw.png   # 离线分析已保存截图
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

from vision import capture, layout, ocr, session_list  # noqa: E402

CAPTURE_DIR = ROOT / "captures"
LOG_DIR = ROOT / "logs"


def setup_logging() -> Path:
    CAPTURE_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"scan_{datetime.now():%Y%m%d_%H%M%S}.log"
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
    parser = argparse.ArgumentParser(description="M2 会话行扫描（零点击）")
    parser.add_argument("--image", type=str, default="", help="离线分析指定截图而非实时捕获")
    args = parser.parse_args()

    log_file = setup_logging()
    log = logging.getLogger("scan")
    log.info("=== 会话行扫描开始，日志: %s ===", log_file)
    capture.set_dpi_aware()

    if args.image:
        image = cv2.imread(args.image)
        if image is None:
            log.error("无法读取截图: %s", args.image)
            return 1
        tag = f"offline_{Path(args.image).stem}"
        log.info("离线模式: %s", args.image)
    else:
        wins = capture.find_wechat_windows()
        win = capture.pick_main_window(wins)
        if win is None or capture.is_iconic(win.hwnd):
            log.error("微信窗口不可用（未运行或已最小化）")
            return 1
        cap = capture.capture_window(win.hwnd)
        image = cap.image
        tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        cv2.imwrite(str(CAPTURE_DIR / f"{tag}_raw.png"), image)
        log.info("捕获 %dx%d", cap.width, cap.height)

    # ① 分栏 + 一次全窗 OCR 供行结构与展示复用
    split_x = layout.detect_chat_split(image)
    log.info("分栏 x = %d", split_x)
    lines = ocr.ocr_image(image)

    # ② 行结构 + 红点行
    red_rows, rows = session_list.scan_red_rows(image, split_x, lines)
    log.info("会话行共 %d 行：", len(rows))
    for row in rows:
        log.info("  行%-2d y=%4d~%-4d %s", row.index, row.y_top, row.y_bottom,
                 row.text[:28])
    if red_rows:
        log.info("有未读红点的行：")
        for rr in red_rows:
            warn = "（最近行兜底）" if rr.by_nearest else ""
            log.info("  行%d %s dot=(%d,%d) 文本=%r%s",
                     rr.row.index, rr.dot, rr.dot.x, rr.dot.y, rr.row.text[:28], warn)
    else:
        log.info("当前无红点行")

    # ③ 标注图：蓝线=分栏，灰线=行边界，红框=红点
    annotated = image.copy()
    h, w = image.shape[:2]
    cv2.line(annotated, (split_x, 0), (split_x, h), (255, 128, 0), 1)
    for row in rows:
        cv2.line(annotated, (session_list.SIDEBAR_W, row.y_top - 2),
                 (split_x, row.y_top - 2), (160, 160, 160), 1)
        cv2.putText(annotated, str(row.index), (68, row.y_top + 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (128, 128, 128), 1, cv2.LINE_AA)
    for rr in red_rows:
        d = rr.dot
        cv2.rectangle(annotated, (d.x, d.y), (d.x + d.w, d.y + d.h), (0, 0, 255), 2)
    out_path = CAPTURE_DIR / f"{tag}_scan.png"
    cv2.imwrite(str(out_path), annotated)
    log.info("标注图 → %s", out_path.name)
    log.info("=== 扫描结束 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
