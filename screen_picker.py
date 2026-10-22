"""全屏取点：屏幕变暗出十字准星，点哪儿取哪儿。

两个实现细节：

* 覆盖层要铺满**整个虚拟桌面**（多显示器时可能横跨负坐标），
  所以位置用 Win32 的 SetWindowPos 来设，而不是 Tk 的 geometry 字符串——
  Tk 的几何语法里负坐标得写成 "+-1920" 这种别扭形式，容易出错。
* 取到的坐标是屏幕绝对像素，和录制、回放用的是同一套坐标系。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import tkinter as tk
from typing import Callable

# SetWindowPos
_HWND_TOPMOST = -1
_SWP_SHOWWINDOW = 0x0040
_SWP_NOACTIVATE = 0x0010

_user32 = ctypes.windll.user32


def virtual_screen() -> tuple[int, int, int, int]:
    """整个虚拟桌面的 (x, y, 宽, 高)。多显示器时 x/y 可能是负数。"""
    g = _user32.GetSystemMetrics
    return g(76), g(77), g(78), g(79)  # SM_X/YVIRTUALSCREEN, SM_CX/CYVIRTUALSCREEN


def cursor_position() -> tuple[int, int]:
    """当前鼠标的屏幕绝对坐标。"""
    point = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


class PointPicker:
    """打开一层全屏覆盖层让用户点一个坐标。

    on_picked(x, y) 在用户点击后回调；on_cancel() 在按 Esc / 右键时回调。
    """

    def __init__(
        self,
        parent: tk.Misc,
        on_picked: Callable[[int, int], None],
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        self._on_picked = on_picked
        self._on_cancel = on_cancel
        self._closed = False

        vx, vy, vw, vh = virtual_screen()
        self._origin = (vx, vy)

        self.top = tk.Toplevel(parent)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", 0.35)
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(
            self.top, bg="#102030", highlightthickness=0, cursor="crosshair"
        )
        self.canvas.pack(fill="both", expand=True)

        self._hint = self.canvas.create_text(
            vw // 2, 40, text="", fill="#ffffff",
            font=("Microsoft YaHei UI", 14), justify="center",
        )
        self._hline = self.canvas.create_line(0, 0, 0, 0, fill="#00e5ff", width=1)
        self._vline = self.canvas.create_line(0, 0, 0, 0, fill="#00e5ff", width=1)
        self._readout = self.canvas.create_text(
            0, 0, text="", fill="#ffffff", font=("Consolas", 13), anchor="nw"
        )
        self._readout_bg = self.canvas.create_rectangle(
            0, 0, 0, 0, fill="#000000", outline="#00e5ff", width=1
        )
        self.canvas.tag_raise(self._readout)
        self.canvas.tag_raise(self._readout_bg)
        self.canvas.tag_lower(self._readout_bg, self._readout)

        self._place(vx, vy, vw, vh)

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Button-3>", lambda _e: self._cancel())
        self.top.bind("<Escape>", lambda _e: self._cancel())

        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()
        try:
            self.top.grab_set()
        except tk.TclError:
            pass

        self.canvas.configure(bg="#102030")
        self.top.after(10, self._first_paint)

    # --- 内部 ---

    def _place(self, x: int, y: int, width: int, height: int) -> None:
        """把覆盖层摆到指定的屏幕矩形上。

        优先用 SetWindowPos（原生接受负坐标）；如果拿到的句柄不对，
        退回 Tk 自己的几何设置。
        """
        try:
            hwnd = self.top.winfo_id()
            _user32.SetWindowPos(
                hwnd, _HWND_TOPMOST, x, y, width, height,
                _SWP_SHOWWINDOW | _SWP_NOACTIVATE,
            )
        except Exception:
            pass

        self.top.update_idletasks()
        if (
            self.top.winfo_rootx() != x
            or self.top.winfo_rooty() != y
            or self.top.winfo_width() != width
            or self.top.winfo_height() != height
        ):
            # 退回 Tk 几何：负坐标要写成 "+-1920" 这种形式
            self.top.geometry(f"{width}x{height}+{x}+{y}")
            self.top.update_idletasks()

    def _first_paint(self) -> None:
        if self._closed:
            return
        x, y = cursor_position()
        self._paint(x, y)
        self.canvas.itemconfigure(
            self._hint,
            text="点击选取这个坐标　·　Esc 或右键取消",
        )

    def _paint(self, screen_x: int, screen_y: int) -> None:
        """按屏幕坐标画面上的十字线和读数。"""
        ox, oy = self._origin
        local_x, local_y = screen_x - ox, screen_y - oy
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()

        self.canvas.coords(self._hline, 0, local_y, width, local_y)
        self.canvas.coords(self._vline, local_x, 0, local_x, height)

        text = f"{screen_x}, {screen_y}"
        self.canvas.itemconfigure(self._readout, text=text)
        # 读数框跟着光标走，靠右边缘时翻到左侧，免得跑出屏幕
        box_x = local_x + 18
        box_y = local_y + 18
        self.canvas.coords(self._readout, box_x + 6, box_y + 4)
        self.canvas.coords(
            self._readout_bg, box_x, box_y, box_x + 130, box_y + 28
        )

    def _on_motion(self, event: tk.Event) -> None:
        ox, oy = self._origin
        self._paint(ox + event.x, oy + event.y)

    def _on_click(self, event: tk.Event) -> None:
        ox, oy = self._origin
        x, y = ox + event.x, oy + event.y
        self.close()
        self._on_picked(x, y)

    def _cancel(self) -> None:
        self.close()
        if self._on_cancel is not None:
            self._on_cancel()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        try:
            self.top.destroy()
        except tk.TclError:
            pass