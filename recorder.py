"""鼠标动作录制：用 pynput 的全局钩子采集移动 / 按键 / 滚轮事件。

鼠标钩子的回调频率可以达到每秒几百次，所以移动点会做抽稀：
距离和间隔都很小的点直接丢掉，只保留有信息量的采样点。

采集器同时维护一组计数（收到多少、过滤掉多少、为什么过滤），
界面会把它们显示出来——录制"没反应"时，靠这些数字就能定位原因。
"""

from __future__ import annotations

import threading
import time

from pynput import mouse

from macro_model import KIND_DOWN, KIND_MOVE, KIND_SCROLL, KIND_UP, Action

# 抽稀阈值：与上一个已记录点相比，位移小于 MOVE_MIN_DIST 像素
# 且间隔小于 MOVE_MIN_DT 秒的移动点会被丢弃。
MOVE_MIN_DIST = 3
MOVE_MIN_DT = 0.020

# 过滤原因，界面上直接显示这些词
FILTER_INJECTED = "注入"
FILTER_WINDOW = "本窗口"

_BUTTON_NAMES: dict[object, str] = {
    mouse.Button.left: "left",
    mouse.Button.right: "right",
    mouse.Button.middle: "middle",
}
for _name in ("x1", "x2"):
    _button = getattr(mouse.Button, _name, None)
    if _button is not None:
        _BUTTON_NAMES[_button] = _name


class Recorder:
    """采集鼠标动作。start() 开始，stop() 结束并取回动作列表。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._actions: list[Action] = []
        self._listener: mouse.Listener | None = None
        self._t0 = 0.0
        self._last_move_t = 0.0
        self._last_pos: tuple[int, int] | None = None
        self._active = False

        # 统计
        self.seen = 0          # 钩子送过来的事件总数
        self.skipped_injected = 0
        self.skipped_window = 0
        self.coalesced = 0     # 被抽稀掉的移动点
        self.last_event = ""

        # 主线程每隔一小段时间刷新一次；(x, y, w, h)，命中该矩形的鼠标事件会被忽略。
        # 用来避免把点击本工具窗口的操作录进宏里。元组赋值在 CPython 里是原子的。
        self.ignore_rect: tuple[int, int, int, int] | None = None

        # 忽略 SendInput 注入的事件（带 LLMHF_INJECTED 标志）。
        # 回放走的就是 SendInput，勾上这个可以彻底避免把自己的回放又录一遍，
        # 同时也滤掉其他宏工具产生的假输入。
        self.ignore_injected = True

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        with self._lock:
            if self._active:
                return
            self._actions = []
            self._last_pos = None
            self._last_move_t = 0.0
            self._t0 = time.perf_counter()
            self._active = True
            self.seen = 0
            self.skipped_injected = 0
            self.skipped_window = 0
            self.coalesced = 0
            self.last_event = ""
        self._listener = mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        self._listener.start()

    def stop(self) -> list[Action]:
        with self._lock:
            self._active = False
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.stop()
        with self._lock:
            return list(self._actions)

    def count(self) -> int:
        with self._lock:
            return len(self._actions)

    def stats(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "recorded": len(self._actions),
                "seen": self.seen,
                "injected": self.skipped_injected,
                "window": self.skipped_window,
                "coalesced": self.coalesced,
                "last": self.last_event,
            }

    # --- 内部 ---

    def _ms(self, now: float) -> float:
        return (now - self._t0) * 1000.0

    def _ignored(self, x: int, y: int) -> bool:
        rect = self.ignore_rect
        if rect is None:
            return False
        rx, ry, rw, rh = rect
        return rx <= x < rx + rw and ry <= y < ry + rh

    def _ensure_position(self, x: int, y: int, now: float) -> None:
        """点击/滚轮之前如果指针不在该坐标，补一个移动动作。

        这样回放时点位的落点是精确的，不依赖上一次移动采样点的精度。
        """
        if self._last_pos == (x, y):
            return
        self._actions.append(Action(KIND_MOVE, x, y, self._ms(now)))
        self._last_pos = (x, y)
        self._last_move_t = now

    def _record(self, kind: str, x: int, y: int, injected: bool, **extra) -> None:
        """所有回调的统一入口：过滤 -> 抽稀 -> 落盘计数。"""
        now = time.perf_counter()
        x, y = int(x), int(y)
        with self._lock:
            if not self._active:
                return
            self.seen += 1

            if injected and self.ignore_injected:
                self.skipped_injected += 1
                return
            if self._ignored(x, y):
                self.skipped_window += 1
                return

            if kind == KIND_MOVE:
                if self._last_pos == (x, y):
                    return  # 位置没动，不算过滤，也不值得记
                if self._last_pos is not None:
                    px, py = self._last_pos
                    moved = max(abs(x - px), abs(y - py))
                    if moved < MOVE_MIN_DIST and (now - self._last_move_t) < MOVE_MIN_DT:
                        self.coalesced += 1
                        return
                self._actions.append(Action(KIND_MOVE, x, y, self._ms(now)))
                self._last_pos = (x, y)
                self._last_move_t = now
            else:
                self._ensure_position(x, y, now)
                self._actions.append(Action(kind, x, y, self._ms(now), **extra))

            action = self._actions[-1]
            self.last_event = f"{action.label()} @ {action.x}, {action.y}"

    def _on_move(self, x, y, injected=False, **kwargs) -> None:
        if self._active:
            self._record(KIND_MOVE, x, y, injected)

    def _on_click(self, x, y, button, pressed, injected=False, **kwargs) -> None:
        if self._active:
            self._record(
                KIND_DOWN if pressed else KIND_UP,
                x,
                y,
                injected,
                button=_BUTTON_NAMES.get(button, "left"),
            )

    def _on_scroll(self, x, y, dx, dy, injected=False, **kwargs) -> None:
        if self._active:
            self._record(KIND_SCROLL, x, y, injected, dx=int(dx), dy=int(dy))