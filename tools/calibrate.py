"""M1 校准工具（DESIGN.md §10-M1 / AGENTS.md 命令约定）。

实测 PrintWindow(PW_RENDERFULLCONTENT) 对微信 4.1 是否有效、定位微信窗口、
红点检测。只输出日志与截图，不点击、不输入、不联网（铁律 4/5）。

通过标准（DESIGN.md §10）：微信被其他窗口完全遮挡时，仍能稳定报告"XX 会话有新消息"。
（遮挡对比与联系人识别属 M2，本工具先验证捕获与红点两个前提环节。）
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vision import capture, reddot  # noqa: E402

EXPECTED_VERSION = "4.1.13.65"  # AGENTS.md 环境事实；不符仅告警不阻断（版本自检项）
CAPTURE_DIR = ROOT / "captures"
LOG_DIR = ROOT / "logs"

log = logging.getLogger("calibrate")
_verdicts: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str, *, warn_only: bool = False) -> None:
    status = "WARN" if warn_only and ok is False else ("PASS" if ok else "FAIL")
    _verdicts.append((name, status))
    log.info("[%s] %s: %s", status, name, detail)


def setup_logging() -> Path:
    CAPTURE_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"calibrate_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def save(image: np.ndarray, name: str) -> Path:
    path = CAPTURE_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.png"
    if not cv2.imwrite(str(path), image):
        log.error("截图保存失败: %s", path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="M1 校准工具：捕获实测 + 红点检测")
    parser.add_argument("--capture-only", action="store_true", help="只测捕获，不做红点检测")
    args = parser.parse_args()

    log_file = setup_logging()
    log.info("=== M1 校准开始，日志: %s ===", log_file)
    capture.set_dpi_aware()

    # ① 定位微信窗口（先进程后文件版本，注册表不采信）
    windows = capture.find_wechat_windows()
    if not windows:
        check("定位微信窗口", False, "未找到 Weixin.exe 的可见窗口——请启动并登录微信后重试")
        return _summary()
    for w in windows:
        log.info("候选窗口: hwnd=%s title=%r rect=%s", w.hwnd, w.title, w.rect)
    win = capture.pick_main_window(windows)
    check("定位微信窗口", True, f"hwnd={win.hwnd} title={win.title!r} exe={win.exe_path}")

    # ② 版本自检（模板坐标绑定界面版本）
    match = win.file_version == EXPECTED_VERSION
    check(
        "微信版本自检",
        match,
        f"exe 文件版本={win.file_version}，预期={EXPECTED_VERSION}"
        + ("" if match else "；版本变化后模板坐标须重新采样校准"),
        warn_only=True,
    )

    # ③ 最小化是硬边界（DESIGN.md §3.1）
    if capture.is_iconic(win.hwnd):
        check("窗口状态", False, "微信已最小化，无法捕获（硬边界）——请还原窗口后重试")
        return _summary()
    check("窗口状态", True, "非最小化，具备捕获条件")

    # ④ 捕获实测：PrintWindow + PW_RENDERFULLCONTENT
    result = capture.capture_window(win.hwnd)
    log.info(
        "PrintWindow 返回=%s 尺寸=%dx%d rect=%s",
        result.printwindow_ok, result.width, result.height, result.window_rect,
    )
    metrics = capture.black_image_metrics(result.image)
    black = not result.printwindow_ok or capture.is_black_image(metrics)
    check(
        "PrintWindow 捕获有效性",
        not black,
        f"黑图指标={metrics}（判定阈值 mean<2 且 std<2）",
    )
    if black:
        log.error("捕获无效 → M1 决策点：启用 WGC 兜底（DESIGN.md §3.1），本工具到此为止")
        return _summary()
    raw_path = save(result.image, "raw")

    # ⑤ 稳定性：间隔 0.8 秒二次捕获，像素级 + pHash 双对比（预验证 §3-② 变化检测链路）
    time.sleep(0.8)
    result2 = capture.capture_window(win.hwnd)
    pixel_diff = float(np.mean(cv2.absdiff(result.image, result2.image)))
    import imagehash  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    ph1 = imagehash.phash(Image.fromarray(cv2.cvtColor(result.image, cv2.COLOR_BGR2RGB)))
    ph2 = imagehash.phash(Image.fromarray(cv2.cvtColor(result2.image, cv2.COLOR_BGR2RGB)))
    stable = (ph1 - ph2) == 0
    check(
        "双捕获一致性",
        stable,
        f"pHash 距离={ph1 - ph2}，像素均差={pixel_diff:.2f}"
        + ("" if stable else "；画面在变化（可能正有消息/动画），属正常，重复运行观察"),
    )

    if args.capture_only:
        log.info("--capture-only：跳过红点检测")
        return _summary()

    # ⑥ 红点检测（只检测标注，不点击）
    dots, mask = reddot.detect_red_dots(result.image)
    save(mask, "reddot_mask")
    save(reddot.annotate(result.image, dots), "annotated")
    if dots:
        lines = ", ".join(
            f"#{i}(x={d.x},y={d.y},w={d.w},h={d.h},area={d.area})"
            for i, d in enumerate(dots)
        )
        check("红点检测", True, f"候选 {len(dots)} 个：{lines}")
    else:
        check("红点检测", True, "本次未检出红点候选（若微信确有未读消息则为漏检，需调 RedDotConfig）")
    check(
        "红点人工核对",
        False,
        f"请人工核对 {raw_path.name} / annotated.png / reddot_mask.png："
        "绿框是否恰好套住未读徽标，有无误检漏检——vision 层验收方式（AGENTS.md）",
        warn_only=True,
    )
    return _summary()


def _summary() -> int:
    fails = [n for n, s in _verdicts if s == "FAIL"]
    log.info("=== 校准结束：%d 项，FAIL %d 项 %s ===",
             len(_verdicts), len(fails), fails if fails else "")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
