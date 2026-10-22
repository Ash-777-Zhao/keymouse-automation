"""鼠标宏：录制屏幕上的鼠标动作，再按设定次数重复回放。

    python mouse_macro.py

热键：F9 开始/停止录制，F10 回放，F11 停止。
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

RECORD_DELAY_MS = 1200  # 点“开始录制”后留出的缓冲，让手指离开鼠标
TICK_MS = 100
MAX_ROWS = 3000  # 列表最多渲染这么多行，再多只是拖慢界面

SETTINGS_FILE = "settings.json"

# pynput 的热键写法：'<f9>' 表示 F9，单个字符直接写 'a'
DEFAULT_HOTKEYS = {"record": "<f9>", "play": "<f10>", "stop": "<f11>", "pick": "<f8>"}
HOTKEY_ORDER = ("record", "play", "stop", "pick")
HOTKEY_NAMES = {"record": "录制", "play": "回放", "stop": "停止", "pick": "取点"}


def _pretty_hotkey(spec: str) -> str:
    """把 pynput 的热键写法转成界面上好读的样子。"""
    if not spec:
        return "未设置"
    if spec.startswith("<") and spec.endswith(">"):
        return spec[1:-1].upper()
    return spec.upper()


def _key_to_spec(key) -> str | None:
    """把 pynput 回调收到的按键转成热键写法。"""
    if isinstance(key, keyboard.Key):
        return f"<{key.name}>"
    char = getattr(key, "char", None)
    if char:
        return char.lower()
    vk = getattr(key, "vk", None)
    return f"<{vk}>" if vk else None


def _enable_dpi_awareness() -> None:
    """必须在创建窗口之前调用。

    进程若不是 DPI 感知的，Windows 会把屏幕坐标按缩放比例虚拟化，
    录到的点位和真实鼠标位置就对不上了（125% 缩放时尤其明显）。
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()

try:
    from pynput import keyboard

    from macro_model import (
        KIND_SCRIPT,
        MacroFormatError,
        remove_actions,
        save_macro,
    )
    from player import Player
    from recorder import Recorder
    from screen_picker import PointPicker, cursor_position
    from script_model import (
        DEFAULT_DELAY_MS,
        MOUSE_LEFT,
        Step,
        load_any,
        remove_steps,
        save_script,
        steps_to_actions,
    )
    from step_dialog import StepDialog
except ImportError as exc:  # pragma: no cover - 只在不满足依赖时走到
    import tkinter.messagebox as _mb

    _mb.showerror(
        "缺少依赖",
        "没能导入 pynput，请先在命令行里执行：\n\n"
        "    python -m pip install pynput\n\n"
        f"详细信息：{exc}",
    )
    raise SystemExit(1) from exc


def _apply_scaling(root: tk.Tk) -> None:
    """DPI 感知之后 tkinter 不会自动放大控件，这里手动对齐一次。"""
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    if dpi:
        root.tk.call("tk", "scaling", dpi / 72.0)


class MacroApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.recorder = Recorder()
        self.player = Player()
        self.actions: list = []
        self.steps: list[Step] = []
        self.macro_path: str | None = None
        self._picker: PointPicker | None = None

        self._ui_queue: queue.Queue = queue.Queue()
        self._hotkeys = None
        self._record_after: str | None = None
        self._stop_requested = False
        self._last_stats: tuple | None = None
        self._capture_listener = None
        self._capturing: str | None = None
        self._hotkey_buttons: dict[str, ttk.Button] = {}

        self.hotkeys: dict[str, str] = dict(DEFAULT_HOTKEYS)
        self._load_settings()

        self.var_repeat = tk.IntVar(value=1)
        self.var_infinite = tk.BooleanVar(value=False)
        self.var_speed = tk.DoubleVar(value=1.0)
        self.var_gap = tk.IntVar(value=500)
        self.var_moves = tk.BooleanVar(value=True)
        self.var_native_typing = tk.BooleanVar(value=False)
        # 默认关掉窗口过滤：开启时它会静默吞掉落在本窗口范围内的动作，
        # 万一目标区域和窗口重叠就会表现得像"录不到"，不容易察觉。需要时手动勾。
        self.var_skip_self = tk.BooleanVar(value=False)
        self.var_skip_injected = tk.BooleanVar(value=True)
        self.var_status = tk.StringVar(value="就绪。按 F9 开始录制。")
        self.var_capture_info = tk.StringVar(value="")
        self.var_selection = tk.StringVar(value="")
        self.var_step_count = tk.StringVar(value="共 0 步")
        self.var_step_selection = tk.StringVar(value="")

        self._build_ui()
        error = self._install_hotkeys()
        if error:
            self.var_status.set(f"全局热键注册失败，请用界面按钮：{error}")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(TICK_MS, self._tick)

    # ------------------------------------------------------------------ 界面

    def _build_ui(self) -> None:
        root = self.root
        root.title("鼠标宏 · 录制 / 回放")
        root.geometry("1060x690")
        root.minsize(1020, 650)

        toolbar = ttk.Frame(root, padding=(10, 10, 10, 4))
        toolbar.pack(fill="x")

        self.btn_record = ttk.Button(
            toolbar, text="● 开始录制  F9", width=18, command=self.toggle_record
        )
        self.btn_play = ttk.Button(toolbar, text="▶ 回放  F10", width=14, command=self.start_play)
        self.btn_stop = ttk.Button(toolbar, text="■ 停止  F11", width=14, command=self.stop_all)
        for button in (self.btn_record, self.btn_play, self.btn_stop):
            button.pack(side="left", padx=(0, 6))

        self.btn_load = ttk.Button(toolbar, text="加载…", width=8, command=self.load_macro)
        self.btn_save = ttk.Button(toolbar, text="保存…", width=8, command=self.save_macro)
        self.btn_clear = ttk.Button(toolbar, text="清空", width=8, command=self.clear_actions)
        for button in (self.btn_load, self.btn_save, self.btn_clear):
            button.pack(side="right", padx=(6, 0))

        body = ttk.Frame(root, padding=(10, 4, 10, 4))
        body.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(body)
        self.notebook.pack(side="left", fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        record_tab = ttk.Frame(self.notebook, padding=(0, 6, 0, 0))
        self.notebook.add(record_tab, text="  录制  ")
        script_tab = ttk.Frame(self.notebook, padding=(0, 6, 0, 0))
        self.notebook.add(script_tab, text="  剧本  ")

        left = record_tab

        list_bar = ttk.Frame(left)
        list_bar.pack(fill="x", pady=(0, 4))
        self.btn_delete = ttk.Button(
            list_bar, text="删除选中", width=10, command=self.delete_selected
        )
        self.btn_delete.pack(side="left")
        self.btn_play_sel = ttk.Button(
            list_bar, text="从选中处回放", width=13, command=self.play_from_selection
        )
        self.btn_play_sel.pack(side="left", padx=(6, 0))
        ttk.Button(list_bar, text="全选", width=7, command=self.select_all).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(
            list_bar, textvariable=self.var_selection, foreground="#666666"
        ).pack(side="left", padx=(10, 0))

        list_area = ttk.Frame(left)
        list_area.pack(fill="both", expand=True)

        columns = ("idx", "action", "pos", "gap")
        self.tree = ttk.Treeview(
            list_area, columns=columns, show="headings", selectmode="extended"
        )
        self.tree.heading("idx", text="#")
        self.tree.heading("action", text="动作")
        self.tree.heading("pos", text="坐标 (屏幕像素)")
        self.tree.heading("gap", text="距上一步")
        self.tree.column("idx", width=60, anchor="e", stretch=False)
        self.tree.column("action", width=110, anchor="w", stretch=False)
        self.tree.column("pos", width=180, anchor="w")
        self.tree.column("gap", width=100, anchor="e", stretch=False)

        scrollbar = ttk.Scrollbar(list_area, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Delete>", self._on_delete_key)
        self.tree.bind("<Control-a>", self._on_select_all_key)
        self.tree.bind("<<TreeviewSelect>>", self._on_selection_change)

        self._build_script_tab(script_tab)
        self._build_settings(body)

        statusbar = ttk.Frame(root, padding=(10, 2, 10, 10))
        statusbar.pack(fill="x")

        top_row = ttk.Frame(statusbar)
        top_row.pack(fill="x")
        ttk.Label(top_row, textvariable=self.var_status).pack(side="left")
        self.progress = ttk.Progressbar(top_row, mode="determinate", length=240)
        self.progress.pack(side="right")

        # 录制时这一行会实时显示"钩子收到多少、过滤掉多少"，
        # 录不到动作时靠它就能看出是没收到事件、还是被过滤掉了。
        ttk.Label(
            statusbar, textvariable=self.var_capture_info, foreground="#666666"
        ).pack(side="left", pady=(2, 0))

    def _build_script_tab(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 4))
        self._step_bar = bar
        ttk.Button(bar, text="屏幕上取点添加", width=15, command=self.pick_point_for_step).pack(
            side="left"
        )
        ttk.Button(bar, text="手动添加", width=10, command=self.add_step).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(bar, text="编辑", width=7, command=self.edit_step).pack(
            side="left", padx=(6, 0)
        )
        self.btn_step_delete = ttk.Button(
            bar, text="删除", width=7, command=self.delete_steps
        )
        self.btn_step_delete.pack(side="left", padx=(6, 0))
        self.btn_step_up = ttk.Button(
            bar, text="↑", width=3, command=lambda: self.move_step(-1)
        )
        self.btn_step_up.pack(side="left", padx=(6, 0))
        self.btn_step_down = ttk.Button(
            bar, text="↓", width=3, command=lambda: self.move_step(1)
        )
        self.btn_step_down.pack(side="left", padx=(2, 0))
        ttk.Button(bar, text="清空", width=7, command=self.clear_steps).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(bar, textvariable=self.var_step_selection, foreground="#666666").pack(
            side="left", padx=(10, 0)
        )
        ttk.Label(bar, textvariable=self.var_step_count, foreground="#666666").pack(side="right")

        area = ttk.Frame(parent)
        area.pack(fill="both", expand=True)

        columns = ("idx", "pos", "mouse", "text", "delay")
        self.step_tree = ttk.Treeview(
            area, columns=columns, show="headings", selectmode="extended"
        )
        self.step_tree.heading("idx", text="#")
        self.step_tree.heading("pos", text="坐标")
        self.step_tree.heading("mouse", text="鼠标动作")
        self.step_tree.heading("text", text="输入文字")
        self.step_tree.heading("delay", text="执行后等待")
        self.step_tree.column("idx", width=44, anchor="e", stretch=False)
        self.step_tree.column("pos", width=130, anchor="w", stretch=False)
        self.step_tree.column("mouse", width=90, anchor="w", stretch=False)
        self.step_tree.column("text", width=140, anchor="w")
        self.step_tree.column("delay", width=90, anchor="e", stretch=False)

        scrollbar = ttk.Scrollbar(area, orient="vertical", command=self.step_tree.yview)
        self.step_tree.configure(yscrollcommand=scrollbar.set)
        self.step_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.step_tree.bind("<Double-1>", lambda _e: self.edit_step())
        self.step_tree.bind("<Delete>", self._on_step_delete_key)
        self.step_tree.bind("<Control-a>", self._on_step_select_all_key)
        self.step_tree.bind("<<TreeviewSelect>>", self._on_step_selection_change)

    # ------------------------------------------------------------------ 剧本

    def _on_tab_changed(self, _event=None) -> None:
        """切页签时把按钮和提示文字换成当前这套的。"""
        self._refresh_hotkey_labels()
        if not self.recorder.active:
            self._render_actions(announce=False)
            self._render_steps()
            if self._on_script_tab():
                total = sum(step.delay_ms for step in self.steps)
                hint = "，点「屏幕上取点添加」开始" if not self.steps else f"，预计耗时约 {total / 1000:.1f} 秒"
                self.var_status.set(f"剧本共 {len(self.steps)} 步{hint}。")
            else:
                self.var_status.set(f"录制模式，当前有 {len(self.actions)} 个动作。")

    def _on_script_tab(self) -> bool:
        try:
            return self.notebook.index(self.notebook.select()) == 1
        except tk.TclError:
            return False

    def _select_script_tab(self) -> None:
        self._select_tab(1)

    def _busy(self) -> bool:
        return self.recorder.active or self.player.running or self._record_after is not None

    def _render_steps(self) -> None:
        self.step_tree.delete(*self.step_tree.get_children())
        self._on_step_selection_change()
        for index, step in enumerate(self.steps[:MAX_ROWS]):
            shown = step.text if len(step.text) <= 20 else step.text[:20] + "…"
            self.step_tree.insert(
                "",
                "end",
                values=(
                    index + 1,
                    f"{step.x}, {step.y}",
                    step.mouse_label(),
                    shown or "—",
                    f"{step.delay_ms} ms",
                ),
            )
        self.var_step_count.set(f"共 {len(self.steps)} 步")

    def _on_step_selection_change(self, _event=None) -> None:
        count = len(self.step_tree.selection())
        self.var_step_selection.set("" if count == 0 else f"已选 {count} 步")

    def _on_step_delete_key(self, _event=None) -> str:
        self.delete_steps()
        return "break"

    def _on_step_select_all_key(self, _event=None) -> str:
        self.select_all_steps()
        return "break"

    def _selected_step_indices(self) -> list[int]:
        return sorted(self.step_tree.index(item) for item in self.step_tree.selection())

    def _edit_step_dialog(self, step: Step, index: int | None = None) -> Step | None:
        dialog = StepDialog(self.root, step, index)
        self.root.wait_window(dialog)
        return dialog.result

    def _add_step_at(self, x: int, y: int) -> None:
        """取点回调：拿到坐标后立刻弹对话框设动作和输入。"""
        self._picker = None
        result = self._edit_step_dialog(Step(x=x, y=y, mouse=MOUSE_LEFT, delay_ms=DEFAULT_DELAY_MS))
        if result is None:
            self.var_status.set(f"已取到 ({x}, {y})，但没有添加。")
            return
        self.steps.append(result)
        self._render_steps()
        self._select_script_tab()
        self.var_status.set(f"已添加第 {len(self.steps)} 步：{result.summary()}")

    def _on_pick_cancelled(self) -> None:
        self._picker = None
        self.var_status.set("已取消取点。")

    def pick_point_for_step(self) -> None:
        if self._busy():
            self._warn("正在录制或回放，先停下来再取点。")
            return
        if self._picker is not None:
            return
        self.var_status.set("把鼠标移到目标位置，点击选取坐标（Esc 取消）。")
        self._picker = PointPicker(
            self.root, on_picked=self._add_step_at, on_cancel=self._on_pick_cancelled
        )

    def quick_pick_step(self) -> None:
        """热键取点：直接取当前鼠标位置，按默认设置添加一步，不弹对话框。"""
        if self._busy():
            self._warn("正在录制或回放，先停下来再取点。")
            return
        x, y = cursor_position()
        self.steps.append(Step(x=x, y=y, mouse=MOUSE_LEFT, delay_ms=DEFAULT_DELAY_MS))
        self._render_steps()
        self._select_script_tab()
        self.var_status.set(f"已按当前位置添加第 {len(self.steps)} 步：(x, y)=({x}, {y})")

    def add_step(self) -> None:
        if self._busy():
            return
        result = self._edit_step_dialog(Step(mouse=MOUSE_LEFT, delay_ms=DEFAULT_DELAY_MS))
        if result is None:
            return
        self.steps.append(result)
        self._render_steps()
        self.var_status.set(f"已添加第 {len(self.steps)} 步：{result.summary()}")

    def edit_step(self) -> None:
        if self._busy():
            return
        indices = self._selected_step_indices()
        if len(indices) != 1:
            self._warn("选中一个步骤再点编辑。")
            return
        index = indices[0]
        result = self._edit_step_dialog(self.steps[index], index)
        if result is None:
            return
        self.steps[index] = result
        self._render_steps()
        self.step_tree.selection_set(self.step_tree.get_children()[index])
        self.var_status.set(f"第 {index + 1} 步已更新：{result.summary()}")

    def delete_steps(self) -> None:
        if self._busy():
            return
        indices = self._selected_step_indices()
        if not indices:
            self._warn("先在列表里选中要删除的步骤（Delete 键也可以）。")
            return
        removed = len(indices)
        self.steps = remove_steps(self.steps, indices)
        self._render_steps()
        self.var_status.set(f"已删除 {removed} 个步骤，剩下 {len(self.steps)} 步。")

    def select_all_steps(self) -> None:
        if self._busy():
            return
        items = self.step_tree.get_children()
        if items:
            self.step_tree.selection_set(items)
            self._on_step_selection_change()

    def move_step(self, delta: int) -> None:
        if self._busy():
            return
        indices = self._selected_step_indices()
        if len(indices) != 1:
            self._warn("选中一个步骤再用 ↑ ↓ 调整顺序。")
            return
        index = indices[0]
        target = index + delta
        if not 0 <= target < len(self.steps):
            return
        self.steps[index], self.steps[target] = self.steps[target], self.steps[index]
        self._render_steps()
        self.step_tree.selection_set(self.step_tree.get_children()[target])

    def clear_steps(self) -> None:
        if self._busy():
            return
        if not self.steps:
            return
        self.steps = []
        self._render_steps()
        self.var_status.set("剧本已清空。")

    def _build_settings(self, parent: ttk.Frame) -> None:
        column = ttk.Frame(parent, width=300)
        column.pack(side="right", fill="y", padx=(12, 0))
        column.pack_propagate(False)
        self._settings_column = column

        self._build_playback_panel(column)
        self._build_hotkey_panel(column)

    def _build_playback_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text=" 回放设置 ", padding=(12, 10))
        panel.pack(fill="x")
        panel.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(panel, text="重复次数").grid(row=row, column=0, sticky="w", pady=(0, 4))
        ttk.Spinbox(panel, from_=1, to=999999, width=9, textvariable=self.var_repeat).grid(
            row=row, column=1, sticky="e", pady=(0, 4)
        )
        ttk.Label(panel, text="次").grid(row=row, column=2, sticky="w", padx=(6, 0), pady=(0, 4))
        row += 1

        ttk.Checkbutton(panel, text="无限循环", variable=self.var_infinite).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        row += 1

        ttk.Label(panel, text="播放速度").grid(row=row, column=0, sticky="w", pady=(0, 4))
        ttk.Spinbox(
            panel, from_=0.25, to=8.0, increment=0.25, width=9, textvariable=self.var_speed
        ).grid(row=row, column=1, sticky="e", pady=(0, 4))
        ttk.Label(panel, text="倍").grid(row=row, column=2, sticky="w", padx=(6, 0), pady=(0, 4))
        row += 1

        ttk.Label(panel, text="循环间隔").grid(row=row, column=0, sticky="w", pady=(0, 4))
        ttk.Spinbox(
            panel, from_=0, to=600000, increment=100, width=9, textvariable=self.var_gap
        ).grid(row=row, column=1, sticky="e", pady=(0, 4))
        ttk.Label(panel, text="毫秒").grid(row=row, column=2, sticky="w", padx=(6, 0), pady=(0, 4))
        row += 1

        ttk.Checkbutton(panel, text="还原移动轨迹", variable=self.var_moves).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        row += 1

        ttk.Checkbutton(
            panel, text="用模拟按键方式输入", variable=self.var_native_typing
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
        row += 1

        ttk.Separator(panel, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(0, 4)
        )
        row += 1

        ttk.Checkbutton(
            panel, text="忽略本窗口内的操作", variable=self.var_skip_self
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 5))
        row += 1

        ttk.Checkbutton(
            panel, text="忽略其他程序注入的输入", variable=self.var_skip_injected
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
        row += 1

        ttk.Label(
            panel,
            foreground="#666666",
            justify="left",
            wraplength=250,
            text="排查录不到：先取消上面两项",
        ).grid(row=row, column=0, columnspan=3, sticky="w")

    def _build_hotkey_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text=" 热键 ", padding=(12, 10))
        panel.pack(fill="x", pady=(10, 0))
        panel.columnconfigure(0, weight=1)

        for index, which in enumerate(HOTKEY_ORDER):
            ttk.Label(panel, text=f"{HOTKEY_NAMES[which]}：").grid(
                row=index, column=0, sticky="w", pady=(0, 5)
            )
            button = ttk.Button(
                panel,
                width=12,
                command=lambda w=which: self._start_capture(w),
            )
            button.grid(row=index, column=1, sticky="e", pady=(0, 5))
            self._hotkey_buttons[which] = button

        ttk.Button(panel, text="恢复默认", command=self._reset_hotkeys).grid(
            row=len(HOTKEY_ORDER), column=1, sticky="e", pady=(2, 4)
        )
        ttk.Label(
            panel,
            foreground="#666666",
            justify="left",
            wraplength=250,
            text="点按钮后按新键，Esc 取消",
        ).grid(row=len(HOTKEY_ORDER) + 1, column=0, columnspan=2, sticky="w")

        self._refresh_hotkey_labels()

    # ------------------------------------------------------------ 线程间通信

    def _post(self, fn, *args) -> None:
        """从后台线程把界面更新排进队列，由主线程的 _tick 消费。

        直接在别的线程里碰 tkinter 控件迟早会崩，统一走队列。
        """
        self._ui_queue.put((fn, args))

    def _drain(self) -> None:
        while True:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except tk.TclError:
                return

    def _tick(self) -> None:
        try:
            self._drain()
            self._refresh_ignore_rect()
            if self.recorder.active:
                self._update_capture_info()
            elif self._last_stats is not None:
                self._last_stats = None
                self.var_capture_info.set("")
        except tk.TclError:
            pass
        finally:
            # 无论如何都要排上下一次，否则一次异常就会让界面永久停止刷新
            try:
                self.root.after(TICK_MS, self._tick)
            except tk.TclError:
                pass

    def _update_capture_info(self) -> None:
        """把采集器的计数显示出来，录制没反应时一眼能看出卡在哪一步。"""
        stats = self.recorder.stats()
        key = (stats["recorded"], stats["seen"], stats["injected"], stats["window"])
        if key == self._last_stats:
            return
        self._last_stats = key

        hotkey = _pretty_hotkey(self.hotkeys["record"])
        if stats["seen"] == 0:
            self.var_status.set(f"● 录制中…… 尚未收到任何鼠标事件（{hotkey} 停止）")
            self.var_capture_info.set(
                "钩子收到 0 个事件 —— 如果此刻你正在移动鼠标，说明系统钩子没抓到输入。"
                "常见原因：目标程序以管理员身份运行而本工具没有提权。"
            )
            return

        self.var_status.set(f"● 录制中…… 已记录 {stats['recorded']} 个动作（{hotkey} 停止）")
        parts = [f"钩子收到 {stats['seen']} 个事件", f"已记录 {stats['recorded']}"]
        if stats["injected"]:
            parts.append(f"忽略注入 {stats['injected']}")
        if stats["window"]:
            parts.append(f"忽略本窗口 {stats['window']}")
        if stats["coalesced"]:
            parts.append(f"轨迹合并 {stats['coalesced']}")
        if stats["last"]:
            parts.append(f"最近 {stats['last']}")
        self.var_capture_info.set("  ·  ".join(parts))

    def _refresh_ignore_rect(self) -> None:
        """把当前窗口矩形和录制开关同步给采集线程（只能从主线程做）。"""
        self.recorder.ignore_injected = bool(self.var_skip_injected.get())
        if not self.var_skip_self.get() or self.root.state() != "normal":
            self.recorder.ignore_rect = None
            return
        try:
            x = self.root.winfo_rootx()
            y = self.root.winfo_rooty()
            width = self.root.winfo_width()
            height = self.root.winfo_height()
        except tk.TclError:
            self.recorder.ignore_rect = None
            return
        self.recorder.ignore_rect = (x, y, width, height) if width > 1 and height > 1 else None

    def _install_hotkeys(self) -> str | None:
        """按当前配置注册全局热键，失败时返回错误信息（界面按钮照常可用）。"""
        self._stop_hotkeys()
        try:
            self._hotkeys = keyboard.GlobalHotKeys(
                {
                    self.hotkeys["record"]: lambda: self._post(self.toggle_record),
                    self.hotkeys["play"]: lambda: self._post(self.start_play),
                    self.hotkeys["stop"]: lambda: self._post(self.stop_all),
                    self.hotkeys["pick"]: lambda: self._post(self.quick_pick_step),
                }
            )
            self._hotkeys.start()
            return None
        except Exception as exc:  # 热键被别的程序占用时依然能用界面按钮
            self._hotkeys = None
            return str(exc)

    def _stop_hotkeys(self) -> None:
        listener, self._hotkeys = self._hotkeys, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    def _refresh_hotkey_labels(self) -> None:
        for which, button in self._hotkey_buttons.items():
            button.configure(text=_pretty_hotkey(self.hotkeys[which]))
        record_key = _pretty_hotkey(self.hotkeys["record"])
        if self.recorder.active:
            self.btn_record.configure(text=f"● 录制中…  {record_key} 停止")
        else:
            self.btn_record.configure(text=f"● 开始录制  {record_key}")
        self.btn_play.configure(text=f"▶ 回放  {_pretty_hotkey(self.hotkeys['play'])}")
        self.btn_stop.configure(text=f"■ 停止  {_pretty_hotkey(self.hotkeys['stop'])}")

    def _start_capture(self, which: str) -> None:
        """进入改键状态：抓取用户按下的下一个键。"""
        if self._capturing is not None:
            self._cancel_capture()
        if self.recorder.active or self.player.running:
            self._warn("正在录制或回放，先停下来再改热键。")
            return

        self._capturing = which
        self._hotkey_buttons[which].configure(text="按下按键…")
        self._stop_hotkeys()  # 否则按到旧热键会直接触发动作
        self._capture_listener = keyboard.Listener(on_press=self._on_capture_press)
        self._capture_listener.start()
        self.var_status.set(f"请按下要用于「{HOTKEY_NAMES[which]}」的键，Esc 取消。")

    def _cancel_capture(self) -> None:
        listener, self._capture_listener = self._capture_listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        self._capturing = None
        self._refresh_hotkey_labels()
        error = self._install_hotkeys()
        if error:
            self.var_status.set(f"全局热键注册失败，请用界面按钮：{error}")

    def _on_capture_press(self, key) -> None:
        """在采集线程里收到按键，交给主线程处理。"""
        self._post(self._apply_capture, key)

    def _apply_capture(self, key) -> None:
        which = self._capturing
        if which is None:
            return

        if key == keyboard.Key.esc:
            self._cancel_capture()
            self.var_status.set("已取消改键。")
            return

        spec = _key_to_spec(key)
        if not spec:
            self._cancel_capture()
            self.var_status.set("这个键不支持，换一个（建议 F1~F12）。")
            return

        for other, existing in self.hotkeys.items():
            if other != which and existing == spec:
                self._cancel_capture()
                self._warn(f"{_pretty_hotkey(spec)} 已经是「{HOTKEY_NAMES[other]}」的热键了。")
                return

        self.hotkeys[which] = spec
        self._cancel_capture()  # 里面会重新注册并刷新按钮文字
        self._save_settings()
        self.var_status.set(f"「{HOTKEY_NAMES[which]}」的热键已改为 {_pretty_hotkey(spec)}。")

    def _reset_hotkeys(self) -> None:
        if self._capturing is not None:
            self._cancel_capture()
        self.hotkeys = dict(DEFAULT_HOTKEYS)
        self._refresh_hotkey_labels()
        self._save_settings()
        error = self._install_hotkeys()
        if error:
            self.var_status.set(f"全局热键注册失败，请用界面按钮：{error}")
        else:
            self.var_status.set("热键已恢复默认（F9 / F10 / F11）。")

    # ---------------------------------------------------------- 配置的存取

    def _settings_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), SETTINGS_FILE)

    def _load_settings(self) -> None:
        try:
            with open(self._settings_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        saved = data.get("hotkeys") if isinstance(data, dict) else None
        if not isinstance(saved, dict):
            return
        for which in HOTKEY_ORDER:
            value = saved.get(which)
            if isinstance(value, str) and value:
                self.hotkeys[which] = value

    def _save_settings(self) -> None:
        payload = {"hotkeys": self.hotkeys}
        try:
            with open(self._settings_path(), "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
        except OSError as exc:
            self.var_status.set(f"配置保存失败：{exc}")

    # ------------------------------------------------------------------ 录制

    def toggle_record(self) -> None:
        if self.recorder.active or self._record_after is not None:
            self.stop_record()
        else:
            self.start_record()

    def start_record(self) -> None:
        if self.player.running:
            self._warn("正在回放，请先停止回放再录制。")
            return
        if self.recorder.active or self._record_after is not None:
            return

        self._refresh_hotkey_labels()
        self.progress.configure(value=0)
        self.var_status.set("准备录制…… 把鼠标移到目标位置。")
        self._record_after = self.root.after(RECORD_DELAY_MS, self._begin_record)

    def _begin_record(self) -> None:
        self._record_after = None
        if self.recorder.active:
            return
        # 到这一步才丢弃上一次的宏，用户中途取消不会丢东西
        self.actions = []
        self._render_actions()
        self._last_stats = None
        self.var_capture_info.set("正在等待鼠标事件……")
        self.recorder.start()
        self._refresh_ignore_rect()
        self.var_status.set(f"● 录制中…… 按 {_pretty_hotkey(self.hotkeys['record'])} 停止")

    def stop_record(self) -> None:
        if self._record_after is not None:
            self.root.after_cancel(self._record_after)
            self._record_after = None
        was_active = self.recorder.active
        if was_active:
            self.actions = self.recorder.stop()
        self._refresh_hotkey_labels()
        self._render_actions()
        if was_active:
            self.var_status.set(
                f"录制完成，共 {len(self.actions)} 个动作。"
                f"按 {_pretty_hotkey(self.hotkeys['play'])} 回放。"
            )
        else:
            self.var_status.set("已取消录制。")

    # ------------------------------------------------------------------ 回放

    def start_play(self) -> None:
        self._play(0)

    def play_from_selection(self) -> None:
        tree = self.step_tree if self._on_script_tab() else self.tree
        selection = tree.selection()
        if not selection:
            self._warn("先在列表里选中一项，作为回放的起点。")
            return
        self._play(min(tree.index(item) for item in selection))

    def _play(self, start: int) -> None:
        if self.recorder.active or self._record_after is not None:
            self._warn("正在录制，请先停止录制再回放。")
            return
        if self.player.running:
            return

        # 当前在哪个页签就回放哪一套内容
        if self._on_script_tab():
            if not self.steps:
                self._warn("剧本还是空的，先添加步骤。")
                return
            source = steps_to_actions(self.steps[start:])
            unit = "步"
        else:
            if not self.actions:
                self._warn("还没有录制任何动作。")
                return
            source = self.actions[start:]
            unit = "个动作"

        if not source:
            self._warn("起点之后已经没有内容了。")
            return

        settings = self._read_settings()
        if settings is None:
            return

        self._stop_requested = False
        self.progress.configure(value=0)
        self._set_playing_ui(True)
        if start:
            self.var_status.set(f"从第 {start + 1} {unit}开始回放……")
        try:
            self.player.start(
                source,
                repeat=settings["repeat"],
                speed=settings["speed"],
                gap_ms=settings["gap_ms"],
                use_moves=settings["use_moves"],
                native_typing=settings["native_typing"],
                on_progress=lambda *a: self._post(self._update_progress, *a),
                on_done=lambda *a: self._post(self._on_play_done, *a),
            )
        except (RuntimeError, ValueError) as exc:
            self._set_playing_ui(False)
            self._warn(str(exc))

    def _read_settings(self) -> dict | None:
        try:
            speed = float(self.var_speed.get())
        except (tk.TclError, ValueError):
            self._warn("播放速度要填数字，例如 1 或 2.5。")
            return None
        if not 0.25 <= speed <= 8.0:
            self._warn("播放速度请设在 0.25 ~ 8 倍之间。")
            return None

        try:
            gap_ms = int(float(self.var_gap.get()))
        except (tk.TclError, ValueError):
            self._warn("循环间隔要填毫秒数，例如 500。")
            return None
        gap_ms = max(0, min(600000, gap_ms))

        if self.var_infinite.get():
            repeat = 0
        else:
            try:
                repeat = int(float(self.var_repeat.get()))
            except (tk.TclError, ValueError):
                self._warn("重复次数要填整数。")
                return None
            if repeat < 1:
                self._warn("重复次数至少是 1，想一直跑就勾上“无限循环”。")
                return None
            repeat = min(repeat, 1000000)

        return {
            "repeat": repeat,
            "speed": speed,
            "gap_ms": gap_ms,
            "use_moves": bool(self.var_moves.get()),
            "native_typing": bool(self.var_native_typing.get()),
        }

    def _update_progress(self, iteration: int, repeat: int, index: int, total: int) -> None:
        if repeat > 0:
            self.progress.configure(
                mode="determinate", maximum=repeat * total, value=(iteration - 1) * total + index
            )
            self.var_status.set(f"回放中…… 第 {iteration}/{repeat} 轮，第 {index}/{total} 个动作")
        else:
            self.progress.configure(mode="determinate", maximum=total, value=index)
            self.var_status.set(f"回放中…… 第 {iteration} 轮（无限循环），第 {index}/{total} 个动作")

    def _on_play_done(self, error, completed: int) -> None:
        self._set_playing_ui(False)
        self.progress.configure(value=0)
        if error is not None:
            self.var_status.set(f"回放出错：{error}")
            messagebox.showerror("回放出错", str(error), parent=self.root)
        elif self._stop_requested:
            self.var_status.set(f"回放已停止，完成了 {completed} 轮。")
        else:
            self.var_status.set(f"回放完成，共 {completed} 轮。")

    def _set_playing_ui(self, playing: bool) -> None:
        state = ["disabled"] if playing else ["!disabled"]
        buttons = [
            self.btn_play,
            self.btn_record,
            self.btn_clear,
            self.btn_load,
            self.btn_save,
            self.btn_delete,
            self.btn_play_sel,
            self.btn_step_delete,
            self.btn_step_up,
            self.btn_step_down,
            *self._hotkey_buttons.values(),
        ]
        for button in buttons:
            button.state(state)
        self.btn_stop.state(["!disabled"])

    def stop_all(self) -> None:
        if self.recorder.active or self._record_after is not None:
            self.stop_record()
            return
        if self.player.running:
            self._stop_requested = True
            self.player.stop()
            self.var_status.set("正在停止……")

    # ------------------------------------------------------------------ 列表

    def _render_actions(self, announce: bool = True) -> None:
        self.tree.delete(*self.tree.get_children())
        self._on_selection_change()
        shown = self.actions[:MAX_ROWS]
        previous_t = 0.0
        for index, action in enumerate(shown):
            gap = "—" if index == 0 else f"{action.t - previous_t:.0f} ms"
            previous_t = action.t
            self.tree.insert(
                "", "end", values=(index + 1, action.label(), f"{action.x}, {action.y}", gap)
            )
        if announce and not self.recorder.active:
            text = f"共 {len(self.actions)} 个动作"
            if len(self.actions) > MAX_ROWS:
                text += f"（列表只显示前 {MAX_ROWS} 条）"
            if self.actions:
                text += "，按 F10 回放。"
            self.var_status.set(text)

    def _on_selection_change(self, _event=None) -> None:
        count = len(self.tree.selection())
        self.var_selection.set("" if count == 0 else f"已选 {count} 项")

    def _on_delete_key(self, _event=None) -> str:
        self.delete_selected()
        return "break"  # 别让 Treeview 自己再处理一次

    def _on_select_all_key(self, _event=None) -> str:
        self.select_all()
        return "break"

    def select_all(self) -> None:
        if self.recorder.active:
            return
        items = self.tree.get_children()
        if items:
            self.tree.selection_set(items)
            self._on_selection_change()

    def delete_selected(self) -> None:
        if self.recorder.active or self.player.running:
            return
        selection = self.tree.selection()
        if not selection:
            self._warn("先在列表里选中要删除的动作（Delete 键也可以）。")
            return
        if not self.actions:
            return

        indices = sorted(self.tree.index(item) for item in selection)
        removed = len(indices)
        before = len(self.actions)
        self.actions = remove_actions(self.actions, indices)
        self._render_actions()
        self.var_status.set(
            f"已删除 {removed} 个动作，剩下 {before - removed} 个"
            f"（后面动作的时间轴已收紧）。"
        )

    def clear_actions(self) -> None:
        if self.recorder.active:
            return
        self.actions = []
        self.macro_path = None
        self._render_actions()
        self.var_status.set("已清空。")

    # ------------------------------------------------------------ 存取宏文件

    def _macro_dir(self) -> str:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macros")
        os.makedirs(path, exist_ok=True)
        return path

    def save_macro(self) -> None:
        """保存当前页签的内容：录制页存动作，剧本页存步骤。"""
        on_script = self._on_script_tab()
        if on_script and not self.steps:
            self._warn("剧本还是空的，没有可保存的内容。")
            return
        if not on_script and not self.actions:
            self._warn("没有可保存的动作。")
            return

        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="保存剧本" if on_script else "保存鼠标宏",
            defaultextension=".json",
            initialdir=self._macro_dir(),
            initialfile="script.json" if on_script else "macro.json",
            filetypes=[("鼠标宏 / 剧本", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return

        meta = {
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": "mouse_macro",
        }
        try:
            if on_script:
                meta["steps"] = len(self.steps)
                save_script(path, self.steps, meta)
            else:
                meta["actions"] = len(self.actions)
                save_macro(path, self.actions, meta)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return

        self.macro_path = path
        count = len(self.steps) if on_script else len(self.actions)
        self.var_status.set(f"已保存 {count} {'步剧本' if on_script else '个动作'}到 {path}")

    def load_macro(self) -> None:
        if self.recorder.active or self.player.running:
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="加载鼠标宏 / 剧本",
            initialdir=self._macro_dir(),
            filetypes=[("鼠标宏 / 剧本", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            kind, data, _meta = load_any(path)
        except (OSError, MacroFormatError, ValueError) as exc:
            messagebox.showerror("加载失败", str(exc), parent=self.root)
            return

        self.macro_path = path
        name = os.path.basename(path)
        if kind == KIND_SCRIPT:
            self.steps = data
            self._render_steps()
            self._select_tab(1)
            self.var_status.set(f"已加载剧本 {len(data)} 步：{name}")
        else:
            self.actions = data
            self._render_actions()
            self._select_tab(0)
            self.var_status.set(f"已加载 {len(data)} 个动作：{name}")

    def _select_tab(self, index: int) -> None:
        try:
            self.notebook.select(index)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ 杂项

    def _warn(self, message: str) -> None:
        messagebox.showwarning("提示", message, parent=self.root)

    def _on_close(self) -> None:
        if self._record_after is not None:
            self.root.after_cancel(self._record_after)
            self._record_after = None
        if self.recorder.active:
            self.recorder.stop()
        if self._capture_listener is not None:
            try:
                self._capture_listener.stop()
            except Exception:
                pass
        self._stop_hotkeys()
        if self.player.running:
            self.player.stop()
            self.player.join(1.5)
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    _apply_scaling(root)
    MacroApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())