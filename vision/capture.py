"""窗口定位与捕获（DESIGN.md §3.1）。

只读窗口像素；本模块不得包含任何点击、键盘或前台切换逻辑。
- 定位：先查运行进程（Weixin.exe），不依赖窗口类名与注册表（AGENTS.md 环境事实）；
- 捕获：PrintWindow + PW_RENDERFULLCONTENT，微信 4.x 为 GPU 合成自绘界面，
  必须带此标志否则拿到黑图/残影；黑图判定见 black_image_metrics；
- 最小化是硬边界：IsIconic 时无法捕获，调用方须先检查；
- 实现说明：直接 ctypes 调 Win32——pywin32 尚无 Python 3.14 轮子（M1 实测结论），
  本层仅用十来个 API，stdlib 足够，顺带消掉最大的依赖兼容风险。
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

PW_RENDERFULLCONTENT = 0x00000002
DWMWA_EXTENDED_FRAME_BOUNDS = 9
WECHAT_PROCESS_NAME = "weixin.exe"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
BI_RGB = 0
DIB_RGB_COLORS = 0

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
shcore = ctypes.WinDLL("shcore")
_version = ctypes.WinDLL("version")


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG), ("top", wintypes.LONG),
        ("right", wintypes.LONG), ("bottom", wintypes.LONG),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


class VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wintypes.DWORD), ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD), ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD), ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD), ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD), ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD), ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# 64 位下句柄是指针宽度，必须显式声明 restype/argtypes，防止截断
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
_version.GetFileVersionInfoW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
_version.GetFileVersionInfoW.restype = wintypes.BOOL
_version.VerQueryValueW.argtypes = [
    ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT),
]
_version.VerQueryValueW.restype = wintypes.BOOL


@dataclass
class WechatWindow:
    hwnd: int
    title: str
    pid: int
    exe_path: str
    file_version: str
    rect: tuple[int, int, int, int]  # GetWindowRect，含不可见缩放边框
    content_bounds: tuple[int, int, int, int]  # DWM 可见内容边界（屏幕坐标）


@dataclass
class CaptureResult:
    image: np.ndarray  # BGR
    width: int
    height: int
    printwindow_ok: bool
    window_rect: tuple[int, int, int, int]


def set_dpi_aware() -> None:
    """进程级 per-monitor DPI 感知，保证坐标为物理像素（启动自检项，DESIGN.md §2）。"""
    try:
        shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            logger.warning("DPI 感知设置失败，坐标可能被系统虚拟化")


def _query_process_image_name(pid: int) -> str | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def _file_version(path: str) -> str:
    size = _version.GetFileVersionInfoSizeW(path, None)
    if not size:
        raise OSError("GetFileVersionInfoSizeW 失败")
    data = ctypes.create_string_buffer(size)
    if not _version.GetFileVersionInfoW(path, 0, size, data):
        raise OSError("GetFileVersionInfoW 失败")
    pval = ctypes.c_void_p()
    vlen = wintypes.UINT(0)
    if not _version.VerQueryValueW(data, "\\", ctypes.byref(pval), ctypes.byref(vlen)):
        raise OSError("VerQueryValueW 失败")
    info = ctypes.cast(pval, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
    return f"{ms >> 16 & 0xFFFF}.{ms & 0xFFFF}.{ls >> 16 & 0xFFFF}.{ls & 0xFFFF}"


def _get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("GetWindowRect 失败")
    return rect.left, rect.top, rect.right, rect.bottom


def _dwm_content_bounds(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = RECT()
    result = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if result != 0:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def find_wechat_windows() -> list[WechatWindow]:
    """枚举进程名为 Weixin.exe 的可见顶层窗口，附 exe 文件版本（先进程后文件，注册表不采信）。"""
    visible: list[tuple[int, str]] = []

    def _on_window(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                visible.append((hwnd, buf.value))
        return True

    enum_proc = WNDENUMPROC(_on_window)  # 回调对象须在 EnumWindows 调用期间存活
    user32.EnumWindows(enum_proc, 0)

    candidates: list[WechatWindow] = []
    for hwnd, title in visible:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _query_process_image_name(pid.value)
        if not exe or Path(exe).name.lower() != WECHAT_PROCESS_NAME:
            continue
        try:
            version = _file_version(exe)
        except OSError:
            version = "未知"
            logger.warning("读取 exe 文件版本失败: %s", exe)
        rect = _get_window_rect(hwnd)
        candidates.append(
            WechatWindow(
                hwnd=hwnd, title=title, pid=pid.value, exe_path=exe,
                file_version=version, rect=rect,
                content_bounds=_dwm_content_bounds(hwnd) or rect,
            )
        )
    return candidates


def pick_main_window(windows: list[WechatWindow]) -> WechatWindow | None:
    """多窗口（含托盘隐藏辅助窗）时取面积最大的为主窗口。"""
    if not windows:
        return None
    return max(windows, key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]))


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """窗口当前屏幕矩形。点击前必须重新获取（捕获与点击之间窗口可能被移动）。"""
    return _get_window_rect(hwnd)


def is_desktop_locked() -> bool:
    """检测系统是否处于锁屏（安全桌面）。

    锁屏期间窗口渲染冻结，PrintWindow 只能拿到过期画面——实测（2026-09-25，
    全天挂机漏检）证明锁屏是捕获盲区，静默空闲与真静止不可区分，必须显式检测。
    判别：OpenInputDesktop 打开接收输入的桌面；锁屏时输入桌面是 Winlogon
    安全桌面，调用将失败。
    """
    DESKTOP_READOBJECTS = 0x0001
    handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if handle:
        user32.CloseDesktop(handle)
        return False
    return True


def is_fully_occluded(hwnd: int, threshold: float = 0.98) -> bool:
    """近似判定窗口是否被其他可见窗口完全覆盖（覆盖率 ≥ threshold）。

    实测依据（2026-09-27 遮挡实验）：微信 4.x 被完全覆盖时暂停渲染，画面冻结
    （66 秒 pHash 零变化），与锁屏同类的检测盲区——部分可见时不冻结。
    实现：EnumWindows 按 Z 序自顶向下枚举，收集排在 hwnd 之前的可见窗口矩形
    （跳过最小化与 cloaked 挂起窗口）；对微信矩形做 64x64 网格中心点采样，
    任一遮挡矩形覆盖该点即记被盖。近似无像素级裁剪，但足以区分"全覆盖"与"部分可见"。
    """
    left, top, right, bottom = _get_window_rect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return False
    occluders: list[tuple[int, int, int, int]] = []
    found_self = False

    def _on_window(h: int, _lparam: int) -> bool:
        nonlocal found_self
        if h == hwnd:
            found_self = True
            return False  # 之后的窗口在微信 Z 序之下，不构成遮挡
        if user32.IsWindowVisible(h) and not user32.IsIconic(h):
            cloaked = wintypes.DWORD(0)
            dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(h), wintypes.DWORD(14),  # DWMWA_CLOAKED
                ctypes.byref(cloaked), ctypes.sizeof(cloaked),
            )
            if not cloaked.value:
                r = RECT()
                if user32.GetWindowRect(h, ctypes.byref(r)):
                    if r.right - r.left > 0 and r.bottom - r.top > 0:
                        occluders.append((r.left, r.top, r.right, r.bottom))
        return True

    proc = WNDENUMPROC(_on_window)
    user32.EnumWindows(proc, 0)
    if not found_self or not occluders:
        return False

    grid = 64
    covered = 0
    for iy in range(grid):
        py = top + int((iy + 0.5) * height / grid)
        for ix in range(grid):
            px = left + int((ix + 0.5) * width / grid)
            if any(l <= px < r and t <= py < b for l, t, r, b in occluders):
                covered += 1
    return covered / (grid * grid) >= threshold


def raise_window_noactivate(hwnd: int) -> None:
    """把窗口提到 Z 序顶部但不抢焦点（SWP_NOACTIVATE）：让被覆盖的微信恢复渲染。

    用于遮挡唤醒；用户在别的窗口打字不受影响（焦点不变）。
    """
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
    HWND_TOP = 0
    user32.SetWindowPos(
        wintypes.HWND(hwnd), wintypes.HWND(HWND_TOP), 0, 0, 0, 0,
        wintypes.UINT(SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE),
    )


def is_iconic(hwnd: int) -> bool:
    """最小化窗口没有渲染表面，PrintWindow/WGC 都拿不到内容（硬边界，DESIGN.md §3.1）。"""
    return bool(user32.IsIconic(hwnd))


def capture_window(hwnd: int) -> CaptureResult:
    """PrintWindow + PW_RENDERFULLCONTENT 截取整个窗口（含自绘标题栏），返回 BGR 图像。"""
    left, top, right, bottom = _get_window_rect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError(f"窗口尺寸异常: {width}x{height}")

    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        raise OSError("GetWindowDC 失败")
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old_obj = gdi32.SelectObject(mem_dc, bitmap)
    try:
        ok = bool(user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT))
        if not ok:
            logger.error("PrintWindow 调用返回失败，结果走黑图判定，必要时启用 WGC 兜底")
        buf = (ctypes.c_ubyte * (width * height * 4))()
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # 负值 = 行序自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        if not gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(bmi), DIB_RGB_COLORS):
            raise OSError("GetDIBits 失败")
    finally:
        gdi32.SelectObject(mem_dc, old_obj)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)

    bgra = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4)
    image = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
    return CaptureResult(
        image=image, width=width, height=height, printwindow_ok=ok,
        window_rect=(left, top, right, bottom),
    )


def black_image_metrics(image: np.ndarray) -> dict:
    """黑图判定指标：微信 4.x GPU 自绘窗口在 PrintWindow 失效时输出近似全黑位图。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return {
        "mean": round(float(gray.mean()), 2),
        "std": round(float(gray.std()), 2),
        "nonblack_ratio": round(float((gray > 10).mean()), 4),
    }


def is_black_image(metrics: dict) -> bool:
    return metrics["mean"] < 2.0 and metrics["std"] < 2.0
