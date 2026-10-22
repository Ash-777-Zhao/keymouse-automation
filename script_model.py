"""剧本：手写的步骤列表，以及把它展开成动作序列。

一个步骤 = 一个屏幕坐标 + 一个鼠标动作 + 可选的键盘输入 + 执行完等多久。
展开之后就是标准的 Action 列表，回放走的是和录制完全同一条路径，
所以剧本不需要单独的回放器。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from macro_model import (
    KIND_DOWN,
    KIND_MOVE,
    KIND_RECORDING,
    KIND_SCRIPT,
    KIND_SCROLL,
    KIND_TYPE,
    KIND_UP,
    SCHEMA_VERSION,
    TYPE_CHAR_MS,
    Action,
    MacroFormatError,
    clamp_int,
    parse_actions,
    read_payload,
    write_payload,
)

# --- 鼠标动作 -------------------------------------------------------------

MOUSE_NONE = "none"
MOUSE_LEFT = "left"
MOUSE_RIGHT = "right"
MOUSE_MIDDLE = "middle"
MOUSE_DOUBLE = "double"
MOUSE_WHEEL_UP = "wheelup"
MOUSE_WHEEL_DOWN = "wheeldown"

MOUSE_ACTIONS = (
    MOUSE_LEFT,
    MOUSE_RIGHT,
    MOUSE_MIDDLE,
    MOUSE_DOUBLE,
    MOUSE_WHEEL_UP,
    MOUSE_WHEEL_DOWN,
    MOUSE_NONE,
)

MOUSE_ACTION_CN: dict[str, str] = {
    MOUSE_LEFT: "左键单击",
    MOUSE_RIGHT: "右键单击",
    MOUSE_MIDDLE: "中键单击",
    MOUSE_DOUBLE: "双击",
    MOUSE_WHEEL_UP: "滚轮上",
    MOUSE_WHEEL_DOWN: "滚轮下",
    MOUSE_NONE: "仅移动",
}

# 单击类动作用哪个键
_SINGLE_BUTTONS = {MOUSE_LEFT: "left", MOUSE_RIGHT: "right", MOUSE_MIDDLE: "middle"}

# --- 时间参数（毫秒） -----------------------------------------------------

CLICK_HOLD_MS = 10  # 按下到抬起
DOUBLE_CLICK_GAP_MS = 80  # 双击两下之间
SCROLL_NOTCHES = 1  # 一次滚几格
DEFAULT_DELAY_MS = 500  # 步骤默认延时


@dataclass(slots=True)
class Step:
    x: int = 0
    y: int = 0
    mouse: str = MOUSE_LEFT
    text: str = ""
    delay_ms: int = DEFAULT_DELAY_MS

    def mouse_label(self) -> str:
        return MOUSE_ACTION_CN.get(self.mouse, self.mouse)

    def summary(self) -> str:
        """一行文字概括，用在状态栏和提示里。"""
        parts = [f"({self.x}, {self.y})", self.mouse_label()]
        if self.text:
            shown = self.text if len(self.text) <= 12 else self.text[:12] + "…"
            parts.append(f"输入 {shown}")
        parts.append(f"{self.delay_ms}ms")
        return " · ".join(parts)


def steps_to_actions(steps: Iterable[Step]) -> list[Action]:
    """把剧本展开成动作序列。

    时间轴在这里算好：输入文字本身也要占时间，所以后面那个动作的等待
    是从"上一个动作真正做完"开始算的，而不是从它开始算的。
    """
    actions: list[Action] = []
    t = 0.0

    for step in steps:
        x, y = int(step.x), int(step.y)
        actions.append(Action(KIND_MOVE, x, y, t))

        mouse = step.mouse
        if mouse in _SINGLE_BUTTONS:
            button = _SINGLE_BUTTONS[mouse]
            actions.append(Action(KIND_DOWN, x, y, t, button=button))
            t += CLICK_HOLD_MS
            actions.append(Action(KIND_UP, x, y, t, button=button))
        elif mouse == MOUSE_DOUBLE:
            for index in range(2):
                actions.append(Action(KIND_DOWN, x, y, t, button="left"))
                t += CLICK_HOLD_MS
                actions.append(Action(KIND_UP, x, y, t, button="left"))
                if index == 0:
                    t += DOUBLE_CLICK_GAP_MS
        elif mouse == MOUSE_WHEEL_UP:
            actions.append(Action(KIND_SCROLL, x, y, t, dy=SCROLL_NOTCHES))
        elif mouse == MOUSE_WHEEL_DOWN:
            actions.append(Action(KIND_SCROLL, x, y, t, dy=-SCROLL_NOTCHES))
        # MOUSE_NONE 只留下前面那个移动动作

        if step.text:
            actions.append(Action(KIND_TYPE, x, y, t, text=step.text))
            t += TYPE_CHAR_MS * len(step.text)

        t += max(0, int(step.delay_ms))

    return actions


def remove_steps(steps: list[Step], indices: Iterable[int]) -> list[Step]:
    """删除步骤。步骤的延时是自带的，所以不需要像动作那样重算时间轴。"""
    drop = set(indices)
    return [step for index, step in enumerate(steps) if index not in drop]


# --- 存取 -----------------------------------------------------------------


def _step_to_dict(step: Step) -> dict[str, Any]:
    data: dict[str, Any] = {
        "x": step.x,
        "y": step.y,
        "mouse": step.mouse,
        "delay_ms": step.delay_ms,
    }
    if step.text:
        data["text"] = step.text
    return data


def parse_steps(raw_steps: Any) -> list[Step]:
    if not isinstance(raw_steps, list):
        raise MacroFormatError("文件里没有 steps 列表。")

    steps: list[Step] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise MacroFormatError(f"第 {index + 1} 个步骤不是对象。")
        mouse = raw.get("mouse", MOUSE_LEFT)
        if mouse not in MOUSE_ACTIONS:
            mouse = MOUSE_LEFT
        steps.append(
            Step(
                x=clamp_int(raw.get("x"), -32768, 32767),
                y=clamp_int(raw.get("y"), -32768, 32767),
                mouse=mouse,
                text=str(raw.get("text") or "")[:2000],
                delay_ms=clamp_int(raw.get("delay_ms"), 0, 600000, DEFAULT_DELAY_MS),
            )
        )
    return steps


def save_script(path: str, steps: Iterable[Step], meta: dict[str, Any] | None = None) -> None:
    write_payload(
        path,
        {
            "schema": SCHEMA_VERSION,
            "kind": KIND_SCRIPT,
            "meta": meta or {},
            "steps": [_step_to_dict(s) for s in steps],
        },
    )


def load_script(path: str) -> tuple[list[Step], dict[str, Any]]:
    payload = read_payload(path)
    meta = payload.get("meta")
    return parse_steps(payload.get("steps")), meta if isinstance(meta, dict) else {}


def load_any(path: str) -> tuple[str, list, dict[str, Any]]:
    """自动识别文件是录制还是剧本。

    返回 (KIND_RECORDING 或 KIND_SCRIPT, 动作列表或步骤列表, meta)。
    """
    payload = read_payload(path)
    meta = payload.get("meta")
    meta = meta if isinstance(meta, dict) else {}

    kind = payload.get("kind")
    if kind not in (KIND_RECORDING, KIND_SCRIPT):
        # 早期版本的文件没有 kind 字段，靠有没有 actions 来判断
        if "actions" in payload:
            kind = KIND_RECORDING
        elif "steps" in payload:
            kind = KIND_SCRIPT
        else:
            raise MacroFormatError("文件里既没有 actions 也没有 steps。")

    if kind == KIND_SCRIPT:
        return KIND_SCRIPT, parse_steps(payload.get("steps")), meta
    return KIND_RECORDING, parse_actions(payload.get("actions")), meta