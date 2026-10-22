"""步骤编辑对话框。

一个步骤要么是鼠标动作，要么是键盘动作，两者各自独立。
键盘动作可以输入一段文字，也可以按一组按键（Ctrl+C、F5 之类）。
"""

from __future__ import annotations

import queue
import tkinter as tk
from tkinter import messagebox, ttk

from keyinput import COMMON_KEYS, build_spec, modifier_name, pretty_keys
from screen_picker import PointPicker
from script_model import (
    DEFAULT_DELAY_MS,
    KEY_COMBO,
    KEY_MODE_CN,
    KEY_MODES,
    KEY_TEXT,
    MOUSE_ACTION_CN,
    MOUSE_ACTIONS,
    MOUSE_LEFT,
    STEP_KEY,
    STEP_KIND_CN,
    STEP_MOUSE,
    Step,
)

_CANONICAL_MOUSE = {MOUSE_ACTION_CN[key]: key for key in MOUSE_ACTIONS}
_CANONICAL_KEY_MODE = {KEY_MODE_CN[key]: key for key in KEY_MODES}
_CANONICAL_KIND = {STEP_KIND_CN[key]: key for key in (STEP_MOUSE, STEP_KEY)}

_TEXT_HINT = "执行这一步时输入这段内容。字母、数字、中文都可以。"
_COMBO_HINT = "快捷键走模拟真实按键，能被任何程序识别；Ctrl+C 这类组合会按下修饰键再按下主键。"


class StepDialog(tk.Toplevel):
    """模态对话框。关掉之后读 result，None 表示取消。"""

    def __init__(self, parent: tk.Misc, step: Step | None = None, index: int | None = None) -> None:
        super().__init__(parent)
        self.result: Step | None = None
        self._picker: PointPicker | None = None

        self._capture_listener = None
        self._capture_queue: queue.Queue = queue.Queue()
        self._capturing = False
        self._held_modifiers: set[str] = set()

        editing = step is not None
        source = step if editing else Step(kind=STEP_MOUSE, mouse=MOUSE_LEFT, delay_ms=DEFAULT_DELAY_MS)

        self.title("编辑步骤" if editing else "添加步骤")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())

        self.var_kind = tk.StringVar(value=STEP_KIND_CN[source.kind])
        self.var_x = tk.StringVar(value=str(source.x))
        self.var_y = tk.StringVar(value=str(source.y))
        self.var_mouse = tk.StringVar(
            value=MOUSE_ACTION_CN.get(source.mouse, MOUSE_ACTION_CN[MOUSE_LEFT])
        )
        self.var_key_mode = tk.StringVar(value=KEY_MODE_CN.get(source.key_mode, KEY_MODE_CN[KEY_TEXT]))
        self.var_text = tk.StringVar(value=source.text)
        self.var_keys = tk.StringVar(value=source.keys)
        self.var_delay = tk.StringVar(value=str(source.delay_ms))

        self._build(index)
        self._apply_kind_visibility()
        self._apply_key_mode_visibility()

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
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)

        row = 0
        if index is not None:
            ttk.Label(body, text=f"第 {index + 1} 步", foreground="#666666").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(0, 8)
            )
            row += 1

        kind_row = ttk.Frame(body)
        kind_row.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(kind_row, text="动作类型").pack(side="left", padx=(0, 12))
        for key in (STEP_MOUSE, STEP_KEY):
            ttk.Radiobutton(
                kind_row,
                text=f"{STEP_KIND_CN[key]}动作",
                value=STEP_KIND_CN[key],
                variable=self.var_kind,
                command=self._on_kind_changed,
            ).pack(side="left", padx=(0, 12))
        row += 1

        ttk.Separator(body, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(0, 12)
        )
        row += 1

        # 鼠标区和键盘区都摆在同一个格子里，靠 grid_remove 切换
        self._mouse_frame = ttk.Frame(body)
        self._mouse_frame.grid(row=row, column=0, columnspan=2, sticky="ew")
        self._key_frame = ttk.Frame(body)
        self._key_frame.grid(row=row, column=0, columnspan=2, sticky="ew")

        self._build_mouse_area(self._mouse_frame)
        self._build_key_area(self._key_frame)
        row += 1

        ttk.Separator(body, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(12, 12)
        )
        row += 1

        delay_row = ttk.Frame(body)
        delay_row.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(delay_row, text="执行后等待").pack(side="left", padx=(0, 12))
        ttk.Spinbox(
            delay_row, from_=0, to=600000, increment=100, width=9, textvariable=self.var_delay
        ).pack(side="left")
        ttk.Label(delay_row, text="毫秒，然后执行下一步", foreground="#666666").pack(
            side="left", padx=(8, 0)
        )
        row += 1

        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="取消", width=10, command=self._cancel).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="确定", width=10, command=self._confirm).pack(side="left")

    def _build_mouse_area(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="坐标").grid(row=0, column=0, sticky="w", pady=(0, 10))
        coords = ttk.Frame(parent)
        coords.grid(row=0, column=1, sticky="w", pady=(0, 10))
        ttk.Label(coords, text="X").pack(side="left")
        self._x_entry = ttk.Entry(coords, textvariable=self.var_x, width=8)
        self._x_entry.pack(side="left", padx=(4, 10))
        ttk.Label(coords, text="Y").pack(side="left")
        ttk.Entry(coords, textvariable=self.var_y, width=8).pack(side="left", padx=(4, 10))
        ttk.Button(coords, text="屏幕上取点…", command=self._pick).pack(side="left")

        ttk.Label(parent, text="鼠标动作").grid(row=1, column=0, sticky="w", pady=(0, 4))
        ttk.Combobox(
            parent,
            textvariable=self.var_mouse,
            values=[MOUSE_ACTION_CN[key] for key in MOUSE_ACTIONS],
            state="readonly",
            width=16,
        ).grid(row=1, column=1, sticky="w", pady=(0, 4))

    def _build_key_area(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="键盘动作").grid(row=0, column=0, sticky="w", pady=(0, 10))
        ttk.Combobox(
            parent,
            textvariable=self.var_key_mode,
            values=[KEY_MODE_CN[key] for key in KEY_MODES],
            state="readonly",
            width=16,
        ).grid(row=0, column=1, sticky="w", pady=(0, 10))

        # 下面两块也只显示其中一块
        self._text_frame = ttk.Frame(parent)
        self._text_frame.grid(row=1, column=0, columnspan=2, sticky="ew")
        self._combo_frame = ttk.Frame(parent)
        self._combo_frame.grid(row=1, column=0, columnspan=2, sticky="ew")

        self.var_key_mode.trace_add("write", lambda *_: self._apply_key_mode_visibility())

        # 输入文字
        self._text_frame.columnconfigure(1, weight=1)
        ttk.Label(self._text_frame, text="输入内容").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Entry(self._text_frame, textvariable=self.var_text, width=30).grid(
            row=0, column=1, sticky="ew", pady=(0, 4)
        )
        ttk.Label(
            self._text_frame, text=_TEXT_HINT, foreground="#666666", wraplength=330, justify="left"
        ).grid(row=1, column=0, columnspan=2, sticky="w")

        # 快捷键 / 功能键
        self._combo_frame.columnconfigure(1, weight=1)
        ttk.Label(self._combo_frame, text="按键组合").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self._capture_btn = ttk.Button(
            self._combo_frame, width=22, command=self._start_key_capture
        )
        self._capture_btn.grid(row=0, column=1, sticky="w", pady=(0, 6))
        self._refresh_capture_button()

        ttk.Label(self._combo_frame, text="常用").grid(row=1, column=0, sticky="w", pady=(0, 6))
        picker = ttk.Combobox(
            self._combo_frame,
            values=[label for _spec, label in COMMON_KEYS],
            state="readonly",
            width=30,
        )
        picker.grid(row=1, column=1, sticky="w", pady=(0, 6))
        picker.bind("<<ComboboxSelected>>", lambda _e: self._insert_common(picker))

        ttk.Label(
            self._combo_frame, text=_COMBO_HINT, foreground="#666666", wraplength=330, justify="left"
        ).grid(row=2, column=0, columnspan=2, sticky="w")

    def _insert_common(self, picker: ttk.Combobox) -> None:
        index = picker.current()
        if 0 <= index < len(COMMON_KEYS):
            spec, _label = COMMON_KEYS[index]
            self.var_keys.set(spec)
            self._refresh_capture_button()
        picker.selection_clear()

    def _refresh_capture_button(self) -> None:
        spec = self.var_keys.get()
        self._capture_btn.configure(text=pretty_keys(spec) if spec else "点击后按下组合键")

    # --- 区域切换 ---

    def _current_kind(self) -> str:
        return _CANONICAL_KIND.get(self.var_kind.get(), STEP_MOUSE)

    def _current_key_mode(self) -> str:
        return _CANONICAL_KEY_MODE.get(self.var_key_mode.get(), KEY_TEXT)

    def _on_kind_changed(self) -> None:
        if self._capturing:
            self._stop_key_capture()
        self._apply_kind_visibility()

    def _apply_kind_visibility(self) -> None:
        if self._current_kind() == STEP_MOUSE:
            self._key_frame.grid_remove()
            self._mouse_frame.grid()
            self.update_idletasks()
            self.geometry("")  # 让窗口缩回内容大小
        else:
            self._mouse_frame.grid_remove()
            self._key_frame.grid()
            self.update_idletasks()
            self.geometry("")

    def _apply_key_mode_visibility(self) -> None:
        if self._current_key_mode() == KEY_TEXT:
            self._combo_frame.grid_remove()
            self._text_frame.grid()
        else:
            self._text_frame.grid_remove()
            self._combo_frame.grid()
        self.update_idletasks()
        self.geometry("")

    # --- 组合键捕获 ---

    def _start_key_capture(self) -> None:
        if self._capturing:
            self._stop_key_capture()
            return
        from pynput import keyboard

        self._capturing = True
        self._held_modifiers = set()
        self._capture_btn.configure(text="请按下组合键…（Esc 取消）")

        self._capture_listener = keyboard.Listener(
            on_press=self._on_capture_press, on_release=self._on_capture_release
        )
        self._capture_listener.start()
        self.after(60, self._poll_capture)

    def _on_capture_press(self, key) -> None:
        """监听线程里跑：只往队列里塞，绝不碰界面。"""
        try:
            from pynput import keyboard

            if key == keyboard.Key.esc:
                self._capture_queue.put(None)
                return
            name = modifier_name(key)
            if name:
                self._held_modifiers.add(name)
                return
            self._capture_queue.put(build_spec(self._held_modifiers, key))
        except Exception:
            self._capture_queue.put(None)

    def _on_capture_release(self, key) -> None:
        name = modifier_name(key)
        if name:
            self._held_modifiers.discard(name)

    def _poll_capture(self) -> None:
        """主线程里跑，取监听线程放进队列的结果。"""
        if not self._capturing:
            return
        try:
            spec = self._capture_queue.get_nowait()
        except queue.Empty:
            self.after(60, self._poll_capture)
            return

        self._stop_key_capture()
        if spec:
            self.var_keys.set(spec)
        self._refresh_capture_button()

    def _stop_key_capture(self) -> None:
        self._capturing = False
        listener, self._capture_listener = self._capture_listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        self._held_modifiers = set()

    # --- 取点 ---

    def _center(self, parent: tk.Misc) -> None:
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

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

    # --- 校验与收尾 ---

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
        if self._capturing:
            self._stop_key_capture()
            self._refresh_capture_button()

        delay = self._parse_delay()
        if delay is None:
            return

        if self._current_kind() == STEP_KEY:
            mode = self._current_key_mode()
            if mode == KEY_COMBO:
                spec = self.var_keys.get().strip()
                if not spec:
                    messagebox.showwarning(
                        "还没设按键", "点一下按钮，再按下要用的组合键。", parent=self
                    )
                    return
                from keyinput import parse_spec

                if not parse_spec(spec):
                    messagebox.showwarning(
                        "按键写法不对", f"看不懂这组按键：{spec}", parent=self
                    )
                    return
                self.result = Step(
                    kind=STEP_KEY, key_mode=KEY_COMBO, keys=spec, delay_ms=delay
                )
            else:
                self.result = Step(
                    kind=STEP_KEY, key_mode=KEY_TEXT, text=self.var_text.get()[:2000], delay_ms=delay
                )
            self._close()
            return

        x = self._parse_coord(self.var_x.get(), "X")
        if x is None:
            return
        y = self._parse_coord(self.var_y.get(), "Y")
        if y is None:
            return
        self.result = Step(
            kind=STEP_MOUSE,
            x=x,
            y=y,
            mouse=_CANONICAL_MOUSE.get(self.var_mouse.get(), MOUSE_LEFT),
            delay_ms=delay,
        )
        self._close()

    def _cancel(self) -> None:
        if self._capturing:
            # 捕获组合键时按 Esc 只取消捕获，不关掉整个对话框
            self._stop_key_capture()
            self._refresh_capture_button()
            return
        self.result = None
        self._close()

    def _close(self) -> None:
        self._stop_key_capture()
        if self._picker is not None:
            self._picker.close()
            self._picker = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()