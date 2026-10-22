"""键盘输入：把一段文字打出去。

pynput 的 type() 是靠 VkKeyScan 把字符映射成虚拟键的，中文这类非 ASCII 字符
映射不了（VkKeyScan 返回 -1），所以这里分两条路：

* ASCII 走 pynput，产生的是和真实按键一样的按下/抬起事件，游戏之类的程序认；
* 其余字符走 SendInput 的 KEYEVENTF_UNICODE，直接把 UTF-16 码元发给系统，
  不经过键盘布局，中文全角标点都能打。

注意 KEYEVENTF_UNICODE 是"只产生字符、不产生真实按键"的路径，
少数只读扫描码的程序（部分游戏）可能收不到，这是系统层面的限制。
"""

from __future__ import annotations

import ctypes
import time
from typing import Callable

# --- SendInput 的结构体 ---------------------------------------------------
# 这些结构体必须和 Windows 的 ABI 完全一致，SendInput 会校验 cbSize，
# 对不上一律失败，所以下面会实测一次大小并在不符时关掉这条路径。

_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_INPUT_KEYBOARD = 1


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    # padding 撑到和 MOUSEINPUT 一样大（x64 上是 32 字节），
    # 否则 INPUT 的总大小会比系统预期的小
    _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_EXPECTED_INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
SENDINPUT_AVAILABLE = ctypes.sizeof(_INPUT) == _EXPECTED_INPUT_SIZE

_user32 = ctypes.windll.user32

# 这两行必须显式写。ctypes.windll.user32 的函数对象是进程级共享的，
# pynput 内部会把 VkKeyScanW.argtypes 设成 (WCHAR,)，一旦它被导入就会影响我们，
# 传 ord() 得到的整数会被拒。自己定死才不受别人影响。
_user32.VkKeyScanW.argtypes = (ctypes.c_wchar,)
_user32.VkKeyScanW.restype = ctypes.c_short  # 默认 c_int 会把 -1 读成 65535


def send_unicode_char(char: str) -> bool:
    """用 KEYEVENTF_UNICODE 发一个字符。返回是否成功。"""
    if not SENDINPUT_AVAILABLE:
        return False

    code = ord(char)
    if code > 0xFFFF:
        # 基本多文种平面之外的字符（emoji 等）要拆成代理对
        offset = code - 0x10000
        units = [0xD800 + (offset >> 10), 0xDC00 + (offset & 0x3FF)]
    else:
        units = [code]

    for unit in units:
        for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            item = _INPUT(type=_INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(0, unit, flags, 0, None)))
            if _user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT)) != 1:
                return False
    return True


def can_type_natively(char: str) -> bool:
    """这个字符能否走 pynput 的原生按键路径。"""
    return len(char) == 1 and _user32.VkKeyScanW(char) != -1


class Typist:
    """按字符发键盘输入。持有 pynput 的键盘控制器。"""

    def __init__(self) -> None:
        from pynput.keyboard import Controller

        self._controller = Controller()

    def type_text(
        self,
        text: str,
        char_delay_ms: float = 25.0,
        should_stop: Callable[[], bool] | None = None,
        native: bool = False,
    ) -> int:
        """打出整段文字，返回实际打出的字符数。

        should_stop 用来支持回放中途停止：每打一个字符检查一次。
        native=True 时优先走模拟真实按键的路径，见 type_char 的说明。
        """
        typed = 0
        delay = max(0.0, char_delay_ms) / 1000.0
        for char in text:
            if should_stop is not None and should_stop():
                break
            self.type_char(char, native=native)
            typed += 1
            if delay:
                time.sleep(delay)
        return typed

    def type_char(self, char: str, native: bool = False) -> None:
        """发一个字符。

        默认走 SendInput 的 KEYEVENTF_UNICODE，直接告诉系统"要输入这个字符"，
        不经过键盘布局，也不经过输入法。

        这一点很关键：如果系统开着中文输入法，走模拟按键那条路的话，
        发出去的 abc 会被输入法当成拼音，打出"饿了咯"这种中文。
        代价是 KEYEVENTF_UNICODE 只产生字符、不产生真实扫描码，
        少数只读扫描码的程序（部分游戏）收不到，那种场景才需要 native=True。
        """
        if native and self._type_native(char):
            return
        if send_unicode_char(char):
            return
        # Unicode 通道不可用时退回模拟按键
        if self._type_native(char):
            return
        raise RuntimeError(
            f"这个字符打不出来：{char!r}。"
            "SendInput 的 Unicode 通道不可用（结构体大小与系统不匹配），"
            "而且它也无法通过模拟按键输入。"
        )

    def _type_native(self, char: str) -> bool:
        if not can_type_natively(char):
            return False
        try:
            self._controller.press(char)
            self._controller.release(char)
            return True
        except Exception:
            return False