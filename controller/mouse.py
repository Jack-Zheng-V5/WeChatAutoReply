"""真实鼠标输入（SendInput，ctypes 直调——pywin32 无 cp314 轮子，见 Progress.md）。

DESIGN.md §5.1：微信 4.x 自绘控件不响应合成的窗口消息点击，只能把真实鼠标
事件打到屏幕坐标。本模块是项目唯一合法的鼠标输入入口。
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

logger = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.SetCursorPos.argtypes = [wintypes.INT, wintypes.INT]
user32.SetCursorPos.restype = wintypes.BOOL
user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def _send_mouse(flags: int) -> None:
    inp = _INPUT(type=INPUT_MOUSE)
    inp.mi = _MOUSEINPUT(0, 0, 0, flags, 0, None)
    if not user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)):
        raise OSError(f"SendInput 失败: {ctypes.get_last_error()}")


def move_to(x: int, y: int) -> None:
    if not user32.SetCursorPos(int(x), int(y)):
        raise OSError(f"SetCursorPos 失败 ({x},{y})")


def left_click(x: int, y: int, *, move_delay: float = 0.08, press_delay: float = 0.06) -> None:
    """移动到绝对屏幕坐标并左键单击（真实事件）。"""
    logger.info("真实鼠标单击屏幕坐标 (%d,%d)", x, y)
    move_to(x, y)
    time.sleep(move_delay)
    _send_mouse(MOUSEEVENTF_LEFTDOWN)
    time.sleep(press_delay)
    _send_mouse(MOUSEEVENTF_LEFTUP)
