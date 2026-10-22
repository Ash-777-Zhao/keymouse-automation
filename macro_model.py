"""鼠标宏的数据模型，以及 .json 宏文件的读写。

一个动作用绝对屏幕像素坐标记录，时间戳是相对录制开始的毫秒数。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any, Iterable

SCHEMA_VERSION = 1

KIND_MOVE = "move"
KIND_DOWN = "down"
KIND_UP = "up"
KIND_SCROLL = "scroll"
KIND_TYPE = "type"  # 键盘输入一段文字，由剧本步骤展开而来

VALID_KINDS = (KIND_MOVE, KIND_DOWN, KIND_UP, KIND_SCROLL, KIND_TYPE)

# 文件里区分两种内容：录制出来的一串动作，还是手写的剧本步骤
KIND_RECORDING = "recording"
KIND_SCRIPT = "script"

# 键盘逐字输入时每个字符占用的毫秒数。
# 剧本展开时间轴和实际回放都要用同一个值，否则两者会对不上，所以放在这里共用。
TYPE_CHAR_MS = 25
VALID_BUTTONS = ("left", "right", "middle", "x1", "x2")

_BUTTON_CN = {"left": "左键", "right": "右键", "middle": "中键", "x1": "侧键1", "x2": "侧键2"}


class MacroFormatError(ValueError):
    """宏文件不是本工具认识的结构。"""


@dataclass(slots=True)
class Action:
    kind: str
    x: int = 0
    y: int = 0
    t: float = 0.0  # 相对录制开始的毫秒数
    button: str = ""
    dx: int = 0
    dy: int = 0
    text: str = ""  # 仅 KIND_TYPE 用

    def label(self) -> str:
        """列表里显示的中文动作名。"""
        if self.kind == KIND_MOVE:
            return "移动"
        if self.kind == KIND_TYPE:
            shown = self.text if len(self.text) <= 12 else self.text[:12] + "…"
            return f"输入 {shown}"
        if self.kind == KIND_DOWN:
            return f"{_BUTTON_CN.get(self.button, self.button)}按下"
        if self.kind == KIND_UP:
            return f"{_BUTTON_CN.get(self.button, self.button)}抬起"
        if self.kind == KIND_SCROLL:
            if self.dy > 0:
                return "滚轮 上"
            if self.dy < 0:
                return "滚轮 下"
            return "滚轮 左" if self.dx < 0 else "滚轮 右"
        return self.kind

    def is_click_boundary(self) -> bool:
        return self.kind in (KIND_DOWN, KIND_UP)


def remove_actions(actions: list[Action], indices: Iterable[int]) -> list[Action]:
    """删掉指定下标的动作，并把后面的时间轴收紧。

    每个动作都带一段"距上一个动作的等待"。删掉一个动作时，它前面那段等待
    也一并消失，这样剩下的动作回放起来就像被删的那个从来没发生过——
    否则列表里删掉一个误点，回放时那个位置会莫名其妙停一下。
    """
    drop = set(indices)
    result: list[Action] = []
    previous_t = 0.0
    new_t = 0.0
    for index, action in enumerate(actions):
        wait = 0.0 if index == 0 else action.t - previous_t
        previous_t = action.t
        if index in drop:
            continue
        new_t += max(0.0, wait)
        result.append(replace(action, t=new_t))
    return result


def _action_to_dict(a: Action) -> dict[str, Any]:
    d: dict[str, Any] = {"kind": a.kind, "x": a.x, "y": a.y, "t": round(a.t, 3)}
    if a.button:
        d["button"] = a.button
    if a.kind == KIND_SCROLL:
        d["dx"] = a.dx
        d["dy"] = a.dy
    if a.kind == KIND_TYPE:
        d["text"] = a.text
    return d


def clamp_int(value: Any, low: int, high: int, default: int = 0) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _parse_action(raw: Any, index: int) -> Action:
    if not isinstance(raw, dict):
        raise MacroFormatError(f"第 {index + 1} 个动作不是对象。")
    kind = raw.get("kind")
    if kind not in VALID_KINDS:
        raise MacroFormatError(f"第 {index + 1} 个动作的 kind 无法识别：{kind!r}")

    button = raw.get("button", "")
    if button not in VALID_BUTTONS:
        button = "left" if kind in (KIND_DOWN, KIND_UP) else ""

    try:
        t = max(0.0, float(raw.get("t", 0.0)))
    except (TypeError, ValueError):
        t = 0.0

    return Action(
        kind=kind,
        x=clamp_int(raw.get("x"), -32768, 32767),
        y=clamp_int(raw.get("y"), -32768, 32767),
        t=t,
        button=button,
        dx=clamp_int(raw.get("dx"), -1000, 1000),
        dy=clamp_int(raw.get("dy"), -1000, 1000),
        text=str(raw.get("text") or "")[:2000] if kind == KIND_TYPE else "",
    )


def write_payload(path: str, payload: dict[str, Any]) -> None:
    """先写临时文件再替换，避免中途失败留下半个文件。"""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_payload(path: str) -> dict[str, Any]:
    """读文件并确认根节点是对象。JSON 语法错误会抛 ValueError。"""
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise MacroFormatError("文件根节点不是 JSON 对象。")
    return payload


def parse_actions(raw_actions: Any) -> list[Action]:
    if not isinstance(raw_actions, list):
        raise MacroFormatError("文件里没有 actions 列表。")
    actions = [_parse_action(raw, i) for i, raw in enumerate(raw_actions)]
    actions.sort(key=lambda a: a.t)
    return actions


def save_macro(path: str, actions: Iterable[Action], meta: dict[str, Any] | None = None) -> None:
    """保存录制得到的动作列表。"""
    write_payload(
        path,
        {
            "schema": SCHEMA_VERSION,
            "kind": KIND_RECORDING,
            "meta": meta or {},
            "actions": [_action_to_dict(a) for a in actions],
        },
    )


def load_macro(path: str) -> tuple[list[Action], dict[str, Any]]:
    """读录制文件，返回 (动作列表, meta)。坏数据会抛 MacroFormatError。"""
    payload = read_payload(path)
    actions = parse_actions(payload.get("actions"))
    meta = payload.get("meta")
    return actions, meta if isinstance(meta, dict) else {}