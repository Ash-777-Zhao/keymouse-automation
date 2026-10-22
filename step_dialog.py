"""步骤编辑对话框：设坐标、鼠标动作、可选输入文字、延时。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from screen_picker import PointPicker
from script_model import (
    DEFAULT_DELAY_MS,
    MOUSE_ACTION_CN,
    MOUSE_ACTIONS,
    MOUSE_LEFT,
    Step,
)

_CANONICAL_MOUSE = {MOUSE_ACTION_CN[key]: key for key in MOUSE_ACTIONS}


class StepDialog(tk.Toplevel):
    """模态对话框。关掉之后读 result，None 表示取消。"""

    def __init__(self, parent: tk.Misc, step: Step | None = None, index: int | None = None) -> None:
        super().__init__(parent)
        self.result: Step | None = None
        self._picker: PointPicker | None = None

        editing = step is not None
        source = step if editing else Step(mouse=MOUSE_LEFT, delay_ms=DEFAULT_DELAY_MS)

        self.title("编辑步骤" if editing else "添加步骤")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())

        self.var_x = tk.StringVar(value=str(source.x))
        self.var_y = tk.StringVar(value=str(source.y))
        self.var_mouse = tk.StringVar(value=MOUSE_ACTION_CN.get(source.mouse, MOUSE_ACTION_CN[MOUSE_LEFT]))
        self.var_text = tk.StringVar(value=source.text)
        self.var_delay = tk.StringVar(value=str(source.delay_ms))

        self._build(index)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._confirm())

        self.grab_set()
        self.update_idletasks()
        self._center(parent)
        self._x_entry.focus_set()

    # --- 界面 ---

    def _build(self, index: int | None) -> None:
        body = ttk.Frame(self, padding=(16, 14))
        body.pack(fill="both", expand=True)

        row = 0
        if index is not None:
            ttk.Label(body, text=f"第 {index + 1} 步", foreground="#666666").grid(
                row=row, column=0, columnspan=4, sticky="w", pady=(0, 10)
            )
            row += 1

        # 坐标
        ttk.Label(body, text="坐标").grid(row=row, column=0, sticky="w", pady=(0, 10))
        coords = ttk.Frame(body)
        coords.grid(row=row, column=1, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Label(coords, text="X").pack(side="left")
        self._x_entry = ttk.Entry(coords, textvariable=self.var_x, width=8)
        self._x_entry.pack(side="left", padx=(4, 10))
        ttk.Label(coords, text="Y").pack(side="left")
        ttk.Entry(coords, textvariable=self.var_y, width=8).pack(side="left", padx=(4, 10))
        ttk.Button(coords, text="屏幕上取点…", command=self._pick).pack(side="left")
        row += 1

        # 鼠标动作
        ttk.Label(body, text="鼠标动作").grid(row=row, column=0, sticky="w", pady=(0, 10))
        combo = ttk.Combobox(
            body,
            textvariable=self.var_mouse,
            values=[MOUSE_ACTION_CN[key] for key in MOUSE_ACTIONS],
            state="readonly",
            width=14,
        )
        combo.grid(row=row, column=1, columnspan=3, sticky="w", pady=(0, 10))
        row += 1

        # 输入文字
        ttk.Label(body, text="输入文字").grid(row=row, column=0, sticky="w", pady=(0, 4))
        entry = ttk.Entry(body, textvariable=self.var_text, width=32)
        entry.grid(row=row, column=1, columnspan=3, sticky="ew", pady=(0, 4))
        row += 1
        ttk.Label(
            body,
            text="执行完上面的鼠标动作后输入这段内容，留空表示不输入。字母数字和中文都可以。",
            foreground="#666666",
            wraplength=330,
            justify="left",
        ).grid(row=row, column=0, columnspan=4, sticky="w", pady=(0, 10))
        row += 1

        # 延时
        ttk.Label(body, text="执行后等待").grid(row=row, column=0, sticky="w", pady=(0, 10))
        delay = ttk.Frame(body)
        delay.grid(row=row, column=1, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Spinbox(delay, from_=0, to=600000, increment=100, width=9, textvariable=self.var_delay).pack(side="left")
        ttk.Label(delay, text="毫秒，然后执行下一步",
                  foreground="#666666").pack(side="left", padx=(8, 0))
        row += 1

        ttk.Separator(body, orient="horizontal").grid(
            row=row, column=0, columnspan=4, sticky="ew", pady=(2, 12)
        )
        row += 1

        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=4, sticky="e")
        ttk.Button(buttons, text="取消", width=10, command=self._cancel).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="确定", width=10, command=self._confirm).pack(side="left")

        body.columnconfigure(1, weight=1)

    def _center(self, parent: tk.Misc) -> None:
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    # --- 交互 ---

    def _pick(self) -> None:
        self.withdraw()  # 别让对话框挡住要点的位置
        self.update_idletasks()

        def picked(x: int, y: int) -> None:
            self.var_x.set(str(x))
            self.var_y.set(str(y))
            self.deiconify()
            self.lift()

        def cancelled() -> None:
            self.deiconify()
            self.lift()

        self._picker = PointPicker(self, on_picked=picked, on_cancel=cancelled)

    def _parse_coord(self, raw: str, name: str) -> int | None:
        try:
            value = int(float(raw.strip()))
        except (ValueError, AttributeError):
            messagebox.showwarning("坐标不对", f"{name} 要填整数，例如 1024。", parent=self)
            return None
        if not -32768 <= value <= 32767:
            messagebox.showwarning("坐标不对", f"{name} 超出屏幕坐标范围。", parent=self)
            return None
        return value

    def _parse_delay(self) -> int | None:
        try:
            value = int(float(self.var_delay.get().strip()))
        except (ValueError, AttributeError):
            messagebox.showwarning("延时不对", "延时要填毫秒数，例如 500。", parent=self)
            return None
        if value < 0:
            messagebox.showwarning("延时不对", "延时不能是负数。", parent=self)
            return None
        return min(value, 600000)

    def _confirm(self) -> None:
        x = self._parse_coord(self.var_x.get(), "X")
        if x is None:
            return
        y = self._parse_coord(self.var_y.get(), "Y")
        if y is None:
            return
        delay = self._parse_delay()
        if delay is None:
            return

        mouse = _CANONICAL_MOUSE.get(self.var_mouse.get(), MOUSE_LEFT)
        self.result = Step(
            x=x,
            y=y,
            mouse=mouse,
            text=self.var_text.get()[:2000],
            delay_ms=delay,
        )
        self._close()

    def _cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        if self._picker is not None:
            self._picker.close()
            self._picker = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()