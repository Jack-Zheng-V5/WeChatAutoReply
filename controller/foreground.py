"""前台动作协议（DESIGN.md §5.1）：记录 → 带起微信 → 动作 → 还原。

使用者体感：屏幕闪一下微信，一秒多就还回来。带起失败时必须中断动作
（绝不静默失败），还原失败时显式告警。
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.POINTER(wintypes.ULONG)]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

VK_MENU = 0x12          # ALT
KEYEVENTF_KEYUP = 0x0002

logger = logging.getLogger(__name__)


def bring_to_front(hwnd: int) -> bool:
    """把窗口带到前台。绕过前台锁的标准组合：AttachThreadInput + ALT 按放。"""
    if not hwnd:
        return False
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    tid_fore = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_cur = kernel32.GetCurrentThreadId()
    attached = bool(tid_fore) and user32.AttachThreadInput(tid_cur, tid_fore, True)
    try:
        user32.keybd_event(VK_MENU, 0, 0, None)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)
        ok = bool(user32.SetForegroundWindow(hwnd))
        if not ok:
            time.sleep(0.05)
            ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(tid_cur, tid_fore, False)
    if not ok:
        logger.warning("SetForegroundWindow 失败 hwnd=%s", hwnd)
    return ok


class ForegroundGuard:
    """§5.1 协议的 with 封装。

    with ForegroundGuard(wechat_hwnd):
        ...  # 此块内的真实输入打到微信
    退出时自动还原原前台窗口；带起失败抛 OSError（动作不执行）。
    """

    def __init__(self, target_hwnd: int) -> None:
        self._target = target_hwnd
        self._saved: int = 0

    def __enter__(self) -> "ForegroundGuard":
        self._saved = user32.GetForegroundWindow() or 0
        if not bring_to_front(self._target):
            raise OSError("无法把微信带到前台（§5.1 协议中断，未执行任何动作）")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._saved and self._saved != self._target:
            if not bring_to_front(self._saved):
                logger.warning("前台窗口还原失败（§5.1），需人工确认焦点状态")
        return False
