"""剧本：手写的步骤列表，以及把它展开成动作序列。

一个步骤要么是**鼠标动作**（坐标 + 左键/右键/滚轮/仅移动），
要么是**键盘动作**（输入一段文字，或者按一组按键比如 Ctrl+C、F5）。

两种步骤各自独立，键盘动作不再挂在鼠标动作后面。
展开之后就是标准的 Action 列表，回放走的是和录制完全同一条路径，
所以剧本不需要单独的回放器。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from keyinput import pretty_keys
from macro_model import (
    KIND_DOWN,
    KIND_KEYS,
    KIND_MOVE,
    KIND_RECORDING,
    KIND_SCROLL,
    KIND_SCRIPT,
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

# --- 步骤类型 -------------------------------------------------------------

STEP_MOUSE = "mouse"
STEP_KEY = "key"
STEP_KINDS = (STEP_MOUSE, STEP_KEY)

STEP_KIND_CN = {STEP_MOUSE: "鼠标", STEP_KEY: "键盘"}

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

_SINGLE_BUTTONS = {MOUSE_LEFT: "left", MOUSE_RIGHT: "right", MOUSE_MIDDLE: "middle"}

# --- 键盘动作 -------------------------------------------------------------

KEY_TEXT = "text"  # 输入一段文字
KEY_COMBO = "keys"  # 按一组按键

KEY_MODES = (KEY_TEXT, KEY_COMBO)
KEY_MODE_CN = {KEY_TEXT: "输入文字", KEY_COMBO: "按快捷键 / 功能键"}

# --- 时间参数（毫秒） -----------------------------------------------------

CLICK_HOLD_MS = 10  # 按下到抬起
DOUBLE_CLICK_GAP_MS = 80  # 双击两下之间
SCROLL_NOTCHES = 1  # 一次滚几格
DEFAULT_DELAY_MS = 500  # 步骤默认延时


@dataclass(slots=True)
class Step:
    kind: str = STEP_MOUSE

    # 鼠标步骤用
    x: int = 0
    y: int = 0
    mouse: str = MOUSE_LEFT

    # 键盘步骤用
    key_mode: str = KEY_TEXT
    text: str = ""
    keys: str = ""

    # 公共
    delay_ms: int = DEFAULT_DELAY_MS

    def mouse_label(self) -> str:
        return MOUSE_ACTION_CN.get(self.mouse, self.mouse)

    def kind_label(self) -> str:
        return STEP_KIND_CN.get(self.kind, self.kind)

    def action_label(self) -> str:
        """列表「动作」列显示的内容。"""
        if self.kind == STEP_KEY:
            if self.key_mode == KEY_COMBO:
                return f"按键 {pretty_keys(self.keys)}" if self.keys else "按键（未设置）"
            shown = self.text if len(self.text) <= 20 else self.text[:20] + "…"
            return f"输入 {shown}" if shown else "输入（空）"
        return self.mouse_label()

    def position_label(self) -> str:
        """列表「坐标」列。键盘步骤不涉及坐标。"""
        return f"{self.x}, {self.y}" if self.kind == STEP_MOUSE else "—"

    def is_ready(self) -> bool:
        """这一步有没有实质内容。空步骤播放时等于纯等待。"""
        if self.kind == STEP_KEY:
            return bool(self.text) if self.key_mode == KEY_TEXT else bool(self.keys)
        return True  # 鼠标步骤哪怕只是移过去也算有事做

    def summary(self) -> str:
        """一行文字概括，用在状态栏和提示里。"""
        parts = [self.kind_label()]
        if self.kind == STEP_MOUSE:
            parts.append(f"({self.x}, {self.y})")
            parts.append(self.mouse_label())
        else:
            parts.append(self.action_label())
        parts.append(f"等待 {self.delay_ms}ms")
        return " · ".join(parts)


def steps_to_actions(steps: Iterable[Step]) -> list[Action]:
    """把剧本展开成动作序列。

    时间轴在这里算好：输入文字本身也要占时间，所以后面那个动作的等待
    是从"上一个动作真正做完"开始算的，而不是从它开始算的。
    """
    actions: list[Action] = []
    t = 0.0

    for step in steps:
        if step.kind == STEP_KEY:
            if step.key_mode == KEY_COMBO:
                if step.keys:
                    actions.append(Action(KIND_KEYS, t=t, text=step.keys))
            elif step.text:
                actions.append(Action(KIND_TYPE, t=t, text=step.text))
                t += TYPE_CHAR_MS * len(step.text)
            t += max(0, int(step.delay_ms))
            continue

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

        t += max(0, int(step.delay_ms))

    return actions


def remove_steps(steps: list[Step], indices: Iterable[int]) -> list[Step]:
    """删除步骤。步骤的延时是自带的，所以不需要像动作那样重算时间轴。"""
    drop = set(indices)
    return [step for index, step in enumerate(steps) if index not in drop]


# --- 存取 -----------------------------------------------------------------


def _step_to_dict(step: Step) -> dict[str, Any]:
    data: dict[str, Any] = {"kind": step.kind, "delay_ms": step.delay_ms}
    if step.kind == STEP_KEY:
        data["key_mode"] = step.key_mode
        if step.key_mode == KEY_COMBO:
            data["keys"] = step.keys
        else:
            data["text"] = step.text
    else:
        data["x"] = step.x
        data["y"] = step.y
        data["mouse"] = step.mouse
    return data


def _parse_mouse_step(raw: dict) -> Step:
    mouse = raw.get("mouse", MOUSE_LEFT)
    if mouse not in MOUSE_ACTIONS:
        mouse = MOUSE_LEFT
    return Step(
        kind=STEP_MOUSE,
        x=clamp_int(raw.get("x"), -32768, 32767),
        y=clamp_int(raw.get("y"), -32768, 32767),
        mouse=mouse,
        delay_ms=clamp_int(raw.get("delay_ms"), 0, 600000, DEFAULT_DELAY_MS),
    )


def _parse_key_step(raw: dict) -> Step:
    mode = raw.get("key_mode", KEY_TEXT)
    if mode not in KEY_MODES:
        mode = KEY_TEXT
    return Step(
        kind=STEP_KEY,
        key_mode=mode,
        text=str(raw.get("text") or "")[:2000],
        keys=str(raw.get("keys") or "")[:200],
        delay_ms=clamp_int(raw.get("delay_ms"), 0, 600000, DEFAULT_DELAY_MS),
    )


def parse_steps(raw_steps: Any) -> list[Step]:
    if not isinstance(raw_steps, list):
        raise MacroFormatError("文件里没有 steps 列表。")

    steps: list[Step] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise MacroFormatError(f"第 {index + 1} 个步骤不是对象。")

        if "kind" not in raw:
            # 早期格式：「输入文字」是挂在鼠标步骤上的。
            # 现在键盘动作独立成一步，所以拆成两步，顺序不变：
            # 鼠标步骤先做（延时挪走），键盘步骤随后（承原延时）。
            text = str(raw.get("text") or "")[:2000]
            if text:
                mouse_step = _parse_mouse_step({**raw, "delay_ms": 0})
                key_step = Step(
                    kind=STEP_KEY,
                    key_mode=KEY_TEXT,
                    text=text,
                    delay_ms=clamp_int(raw.get("delay_ms"), 0, 600000, DEFAULT_DELAY_MS),
                )
                steps.append(mouse_step)
                steps.append(key_step)
            else:
                steps.append(_parse_mouse_step(raw))
            continue

        kind = raw.get("kind")
        if kind == STEP_KEY:
            steps.append(_parse_key_step(raw))
        elif kind == STEP_MOUSE:
            steps.append(_parse_mouse_step(raw))
        else:
            raise MacroFormatError(f"第 {index + 1} 个步骤的 kind 无法识别：{kind!r}")

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