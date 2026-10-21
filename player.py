"""宏回放：在后台线程里按录制下来的时间轴重放动作。

两个实现细节值得留意：

* 移动用 ``Controller.position`` 也就是 SetCursorPos 来定位，而不是相对位移。
  绝对定位累积误差为零，点位不会一路飘。
* 播放中被打断时，任何还按着的键都会在收尾时释放，不会留下卡住的左键。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Sequence

from pynput.mouse import Button, Controller

from macro_model import KIND_DOWN, KIND_MOVE, KIND_SCROLL, KIND_UP, Action

MIN_SPEED = 0.05
MAX_SPEED = 20.0

_BUTTONS: dict[str, Button] = {
    "left": Button.left,
    "right": Button.right,
    "middle": Button.middle,
}
for _name in ("x1", "x2"):
    _button = getattr(Button, _name, None)
    if _button is not None:
        _BUTTONS[_name] = _button

# 进度回调不是每个动作都发一次，否则几千个动作会把界面刷爆
_PROGRESS_EVERY = 16

ProgressFn = Callable[[int, int, int, int], None]
"""进度回调：(当前第几轮, 总轮数(0=无限), 本轮第几个, 本轮总数)"""

DoneFn = Callable[[BaseException | None, int], None]
"""完成回调：(异常或 None, 实际完成的轮数)"""


class Player:
    """把动作列表按原始节奏回放。start() 立即返回，播放跑在后台线程。"""

    def __init__(self) -> None:
        self._controller = Controller()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        actions: Sequence[Action],
        *,
        repeat: int = 1,
        speed: float = 1.0,
        gap_ms: int = 0,
        use_moves: bool = True,
        on_progress: ProgressFn | None = None,
        on_done: DoneFn | None = None,
    ) -> None:
        """repeat=0 表示无限循环；speed 是倍速（2.0 = 快一倍）。"""
        if self.running:
            raise RuntimeError("回放已在进行中。")

        steps = [a for a in actions if use_moves or a.kind != KIND_MOVE]
        if not steps:
            raise ValueError("没有可以回放的动作。")

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(steps, max(1, int(repeat)), _clamp_speed(speed), max(0, int(gap_ms)),
                  on_progress, on_done),
            name="macro-player",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """请求停止。正在播放的线程会在下一个检查点退出。"""
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # --- 播放线程 ---

    def _run(
        self,
        steps: list[Action],
        repeat: int,
        speed: float,
        gap_ms: int,
        on_progress: ProgressFn | None,
        on_done: DoneFn | None,
    ) -> None:
        held: list[Button] = []
        error: BaseException | None = None
        completed = 0
        total = len(steps)
        infinite = repeat == 0

        try:
            iteration = 0
            while not self._stop.is_set():
                if not infinite and iteration >= repeat:
                    break
                iteration += 1

                if iteration > 1 and gap_ms > 0:
                    if not self._sleep(gap_ms / 1000.0):
                        break

                previous_t = steps[0].t
                aborted = False
                for index, action in enumerate(steps):
                    if self._stop.is_set():
                        aborted = True
                        break
                    if index > 0:
                        wait = (action.t - previous_t) / 1000.0 / speed
                        if wait > 0 and not self._sleep(wait):
                            aborted = True
                            break
                        previous_t = action.t
                    self._dispatch(action, held)
                    if on_progress and (index % _PROGRESS_EVERY == 0 or index == total - 1):
                        on_progress(iteration, repeat, index + 1, total)

                if aborted:
                    break
                completed = iteration
        except BaseException as exc:  # noqa: BLE001 - 回传给界面统一报错
            error = exc
        finally:
            for button in held:
                try:
                    self._controller.release(button)
                except Exception:
                    pass
            if on_done:
                try:
                    on_done(error, completed)
                except Exception:
                    pass

    def _dispatch(self, action: Action, held: list[Button]) -> None:
        if action.kind == KIND_MOVE:
            self._controller.position = (action.x, action.y)
        elif action.kind == KIND_DOWN:
            button = _BUTTONS.get(action.button, Button.left)
            self._controller.position = (action.x, action.y)
            self._controller.press(button)
            held.append(button)
        elif action.kind == KIND_UP:
            button = _BUTTONS.get(action.button, Button.left)
            self._controller.position = (action.x, action.y)
            self._controller.release(button)
            if button in held:
                held.remove(button)
        elif action.kind == KIND_SCROLL:
            self._controller.position = (action.x, action.y)
            self._controller.scroll(action.dx, action.dy)

    def _sleep(self, seconds: float) -> bool:
        """分片睡眠，以便随时响应停止。返回 False 表示被打断。"""
        deadline = time.perf_counter() + seconds
        while True:
            if self._stop.is_set():
                return False
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.05))


def _clamp_speed(speed: float) -> float:
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    return max(MIN_SPEED, min(MAX_SPEED, value))