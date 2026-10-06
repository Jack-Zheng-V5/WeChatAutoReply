"""发送链路动作（DESIGN.md §5）：剪贴板写入 → Ctrl+V → Enter，铁律 6 剪贴板保存恢复。

仅动作原语；闸门 5 的闭环判定逻辑在 safety/gate5.py，编排在 main.py。
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

logger = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_V = 0x56
VK_A = 0x41
VK_DELETE = 0x2E
KEYEVENTF_KEYUP = 0x0002

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.keybd_event.argtypes = [
    wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.POINTER(wintypes.ULONG)
]

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]


def _open_clipboard(retries: int = 10) -> None:
    """剪贴板可能被其他进程短暂锁定，重试打开（失败必须显式抛出）。"""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise OSError("OpenClipboard 失败（剪贴板被占用）")


def save_clipboard() -> str | None:
    """保存当前剪贴板文本（铁律 6）。当前是非文本内容时返回 None（无法原样恢复）。"""
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        logger.warning("剪贴板当前无文本内容，发送后将无法原样恢复（铁律 6 局限，见日志）")
        return None
    _open_clipboard()
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    _open_clipboard()
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError("GlobalAlloc 失败")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            raise OSError("GlobalLock 失败")
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        # 成功后剪贴板接管句柄；失败才需要释放
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise OSError("SetClipboardData 失败")
    finally:
        user32.CloseClipboard()


def restore_clipboard(saved: str | None) -> None:
    """恢复发送前保存的剪贴板内容（铁律 6）。None = 发送前无可恢复的文本内容。"""
    if saved is None:
        return
    set_clipboard_text(saved)


def _key(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, None)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, None)


def clear_input() -> None:
    """清空输入框（Ctrl+A + Delete）：防止重试时 Ctrl+V 叠加文本。"""
    user32.keybd_event(VK_CONTROL, 0, 0, None)
    user32.keybd_event(VK_A, 0, 0, None)
    user32.keybd_event(VK_A, 0, KEYEVENTF_KEYUP, None)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, None)
    _key(VK_DELETE)


def paste() -> None:
    user32.keybd_event(VK_CONTROL, 0, 0, None)
    user32.keybd_event(VK_V, 0, 0, None)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, None)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, None)


def enter() -> None:
    _key(VK_RETURN)
