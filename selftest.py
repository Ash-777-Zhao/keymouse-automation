"""自检脚本：验证数据模型、采集抽稀逻辑和回放时序。

回放部分用假的 Controller 顶替真实的 pynput，所以跑测试时鼠标不会真的动。

    python selftest.py
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import threading
import time

import macro_model
import player as player_mod
from macro_model import (
    KIND_DOWN,
    KIND_MOVE,
    KIND_RECORDING,
    KIND_SCROLL,
    KIND_SCRIPT,
    KIND_TYPE,
    KIND_UP,
    Action,
)
from recorder import Recorder

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  FAIL {name} {detail}")


# ------------------------------------------------------------------ 假控制器


class FakeController:
    """把回放发出的调用记下来，替代真实的鼠标操作。"""

    log: list[tuple] = []

    def __init__(self) -> None:
        self._pos = (0, 0)

    @property
    def position(self):
        return self._pos

    @position.setter
    def position(self, value):
        self._pos = (int(value[0]), int(value[1]))
        FakeController.log.append(("position", self._pos[0], self._pos[1]))

    def press(self, button):
        FakeController.log.append(("down", button.name))

    def release(self, button):
        FakeController.log.append(("up", button.name))

    def scroll(self, dx, dy):
        FakeController.log.append(("scroll", dx, dy))


def events(log):
    """只看按键和滚轮，忽略设置指针位置这种伴随动作。"""
    return [entry for entry in log if entry[0] != "position"]


def positions(log):
    return [entry[1:] for entry in log if entry[0] == "position"]


def kinds(log):
    return [entry[0] for entry in log]


# ------------------------------------------------------------------ 数据模型


def test_model() -> None:
    print("\n[数据模型]")

    actions = [
        Action(KIND_MOVE, 10, 20, 0.0),
        Action(KIND_DOWN, 10, 20, 5.0, button="left"),
        Action(KIND_UP, 10, 20, 30.0, button="left"),
        Action(KIND_SCROLL, 300, 400, 120.0, dx=0, dy=3),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "m.json")
        macro_model.save_macro(path, actions, {"note": "测试"})
        loaded, meta = macro_model.load_macro(path)

    check("存取往返后动作数量一致", len(loaded) == len(actions), f"{len(loaded)} != {len(actions)}")
    check(
        "字段逐个还原",
        all(
            (a.kind, a.x, a.y, a.t, a.button, a.dx, a.dy)
            == (b.kind, b.x, b.y, b.t, b.button, b.dx, b.dy)
            for a, b in zip(loaded, actions)
        ),
    )
    check("meta 被保留", meta.get("note") == "测试")

    check("滚轮动作显示为向上", actions[3].label() == "滚轮 上", actions[3].label())
    check("左键动作显示为左键按下", actions[1].label() == "左键按下", actions[1].label())

    # 坏数据
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write('{"actions": [{"kind": "wormhole", "x": 1}]}')
        try:
            macro_model.load_macro(bad)
            check("非法 kind 应当报错", False, "没有抛异常")
        except macro_model.MacroFormatError:
            check("非法 kind 会抛 MacroFormatError", True)

        not_json = os.path.join(tmp, "nope.json")
        with open(not_json, "w", encoding="utf-8") as fh:
            fh.write("这不是 json")
        try:
            macro_model.load_macro(not_json)
            check("非 JSON 应当报错", False, "没有抛异常")
        except ValueError:
            check("非 JSON 会抛 ValueError", True)

    # 时间戳乱序时应当排序
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "u.json")
        macro_model.save_macro(path, [Action(KIND_MOVE, 1, 1, 90.0), Action(KIND_MOVE, 2, 2, 10.0)])
        loaded, _ = macro_model.load_macro(path)
    check("载入时按时间排序", [a.t for a in loaded] == [10.0, 90.0], str([a.t for a in loaded]))

    # 越界坐标应当被夹紧而不是崩掉
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "c.json")
        raw = '{"actions": [{"kind": "move", "x": 999999, "y": "abc", "t": -5}]}'
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw)
        loaded, _ = macro_model.load_macro(path)
    check("越界 x 被夹紧", loaded[0].x == 32767, str(loaded[0].x))
    check("非法 y 退化为 0", loaded[0].y == 0, str(loaded[0].y))
    check("负时间戳归零", loaded[0].t == 0.0, str(loaded[0].t))


# ------------------------------------------------------------------ 动作编辑


def test_remove_actions() -> None:
    print("\n[删除动作]")

    def make(times):
        return [Action(KIND_MOVE, i, i, t) for i, t in enumerate(times)]

    # 每个动作都带一段"距上一个的等待"。删掉动作时，它前面那段等待要一起消失，
    # 否则回放时被删的位置会莫名其妙停一下。
    acts = make([0.0, 500.0, 520.0, 900.0])
    kept = macro_model.remove_actions(acts, [1])
    check("删中间一个后数量正确", len(kept) == 3, str(len(kept)))
    check(
        "删中间一个后时间轴收紧",
        [a.t for a in kept] == [0.0, 20.0, 400.0],
        str([a.t for a in kept]),
    )
    check(
        "删除不影响其余字段",
        [(a.kind, a.x, a.y) for a in kept] == [(KIND_MOVE, 0, 0), (KIND_MOVE, 2, 2), (KIND_MOVE, 3, 3)],
        str([(a.kind, a.x, a.y) for a in kept]),
    )

    kept = macro_model.remove_actions(make([0.0, 500.0, 520.0, 900.0]), [0])
    check(
        "删第一个后其余整体前移",
        [a.t for a in kept] == [500.0, 520.0, 900.0],
        str([a.t for a in kept]),
    )

    kept = macro_model.remove_actions(make([0.0, 500.0, 520.0, 900.0]), [3])
    check("删最后一个", [a.t for a in kept] == [0.0, 500.0, 520.0], str([a.t for a in kept]))

    kept = macro_model.remove_actions(make([0.0, 100.0, 200.0, 300.0]), [1, 2])
    check(
        "连续删多个",
        [a.t for a in kept] == [0.0, 100.0],
        str([a.t for a in kept]),
    )

    kept = macro_model.remove_actions(make([0.0, 100.0, 200.0]), [0, 1, 2])
    check("全部删掉后为空", kept == [], str(kept))

    original = make([0.0, 100.0])
    kept = macro_model.remove_actions(original, [])
    check("不删任何东西时原样返回", [a.t for a in kept] == [0.0, 100.0], str([a.t for a in kept]))

    kept = macro_model.remove_actions(make([0.0, 100.0]), [99])
    check("越界下标被忽略", len(kept) == 2, str(len(kept)))

    # 时间戳乱序时不能算出负的等待
    kept = macro_model.remove_actions(make([0.0, 300.0, 100.0]), [1])
    check("乱序输入不会产生负时间戳", all(a.t >= 0 for a in kept), str([a.t for a in kept]))

    # 按键、滚轮这些字段要完整保留
    mixed = [
        Action(KIND_MOVE, 0, 0, 0.0),
        Action(KIND_DOWN, 5, 5, 50.0, button="right"),
        Action(KIND_SCROLL, 5, 5, 80.0, dx=1, dy=-2),
        Action(KIND_UP, 5, 5, 120.0, button="right"),
    ]
    kept = macro_model.remove_actions(mixed, [0])
    check("删除后按键类型保持不变", [a.kind for a in kept] == [KIND_DOWN, KIND_SCROLL, KIND_UP], str([a.kind for a in kept]))
    check("删除后按键名保持不变", kept[0].button == "right", kept[0].button)
    check("删除后滚轮数值保持不变", (kept[1].dx, kept[1].dy) == (1, -2), str((kept[1].dx, kept[1].dy)))


# ------------------------------------------------------------------ 剧本模型


def test_script_model() -> None:
    print("\n[剧本模型]")

    import script_model as sm

    acts = sm.steps_to_actions([sm.Step(x=100, y=200, mouse=sm.MOUSE_LEFT, delay_ms=500)])
    check(
        "左键单击展开成 移动+按下+抬起",
        [a.kind for a in acts] == [KIND_MOVE, KIND_DOWN, KIND_UP],
        str([a.kind for a in acts]),
    )
    check("展开后每个动作都带同一坐标", all((a.x, a.y) == (100, 200) for a in acts))
    check("展开后按键是左键", acts[1].button == "left", acts[1].button)

    acts = sm.steps_to_actions([sm.Step(x=1, y=2, mouse=sm.MOUSE_NONE, delay_ms=0)])
    check("仅移动只展开出一个动作", [a.kind for a in acts] == [KIND_MOVE], str([a.kind for a in acts]))

    acts = sm.steps_to_actions([sm.Step(mouse=sm.MOUSE_DOUBLE, delay_ms=0)])
    check(
        "双击展开成 移动+两次按下抬起",
        [a.kind for a in acts] == [KIND_MOVE, KIND_DOWN, KIND_UP, KIND_DOWN, KIND_UP],
        str([a.kind for a in acts]),
    )
    downs = [a.t for a in acts if a.kind == KIND_DOWN]
    check(
        "双击两下之间有间隔",
        downs[1] - downs[0] == sm.CLICK_HOLD_MS + sm.DOUBLE_CLICK_GAP_MS,
        str(downs),
    )

    for name, mouse, positive in (("滚轮上", sm.MOUSE_WHEEL_UP, True), ("滚轮下", sm.MOUSE_WHEEL_DOWN, False)):
        acts = sm.steps_to_actions([sm.Step(mouse=mouse, delay_ms=0)])
        scroll = [a for a in acts if a.kind == KIND_SCROLL]
        check(f"{name}展开出滚轮动作", len(scroll) == 1, str([a.kind for a in acts]))
        check(f"{name}方向正确", (scroll[0].dy > 0) == positive, str(scroll[0].dy))

    # 右键 / 中键
    for name, mouse, button in (("右键", sm.MOUSE_RIGHT, "right"), ("中键", sm.MOUSE_MIDDLE, "middle")):
        acts = sm.steps_to_actions([sm.Step(mouse=mouse, delay_ms=0)])
        check(f"{name}展开后按键正确", acts[1].button == button, acts[1].button)

    acts = sm.steps_to_actions([sm.Step(mouse=sm.MOUSE_LEFT, text="admin", delay_ms=300)])
    check("输入文字展开成 type 动作", acts[-1].kind == KIND_TYPE, str([a.kind for a in acts]))
    check("输入内容完整保留", acts[-1].text == "admin", acts[-1].text)
    check("输入动作带坐标", (acts[-1].x, acts[-1].y) == (0, 0))

    # 输入本身要占时间，否则后面那步会在打字中途就触发
    steps = [
        sm.Step(x=0, y=0, mouse=sm.MOUSE_NONE, text="abcd", delay_ms=1000),
        sm.Step(x=9, y=9, mouse=sm.MOUSE_NONE, delay_ms=0),
    ]
    acts = sm.steps_to_actions(steps)
    second_move = [a for a in acts if a.kind == KIND_MOVE][1]
    check(
        "打字耗时被计入时间轴",
        second_move.t == 4 * macro_model.TYPE_CHAR_MS + 1000,
        str(second_move.t),
    )

    check("空剧本展开为空列表", sm.steps_to_actions([]) == [])

    steps = [sm.Step(x=i, delay_ms=0) for i in range(5)]
    kept = sm.remove_steps(steps, [1, 3])
    check("删除步骤", [s.x for s in kept] == [0, 2, 4], str([s.x for s in kept]))
    check("删除步骤不影响延时字段", all(s.delay_ms == 0 for s in kept))

    # 存取
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.json")
        original = [
            sm.Step(x=-1500, y=300, mouse=sm.MOUSE_DOUBLE, text="中文abc", delay_ms=1200),
            sm.Step(x=800, y=600, mouse=sm.MOUSE_WHEEL_UP, delay_ms=0),
        ]
        sm.save_script(path, original, {"note": "测试"})
        loaded, meta = sm.load_script(path)
        check("剧本存取往返一致", loaded == original, str(loaded))
        check("剧本 meta 保留", meta.get("note") == "测试", str(meta))
        check("副屏负坐标能存能读", loaded[0].x == -1500, str(loaded[0].x))

        kind, data, _ = sm.load_any(path)
        check("load_any 认出剧本", kind == KIND_SCRIPT, kind)
        check("load_any 取出步骤", data == original)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "m.json")
        macro_model.save_macro(path, [Action(KIND_MOVE, 1, 1, 0.0)])
        kind, data, _ = sm.load_any(path)
        check("load_any 认出录制", kind == KIND_RECORDING, kind)
        check("load_any 取出动作", len(data) == 1 and data[0].kind == KIND_MOVE, str(data))

        # 早期版本的文件没有 kind 字段
        old = os.path.join(tmp, "old.json")
        with open(old, "w", encoding="utf-8") as fh:
            fh.write('{"actions": [{"kind": "move", "x": 5, "y": 6, "t": 0}]}')
        kind, data, _ = sm.load_any(old)
        check("老格式没有 kind 也能认出录制", kind == KIND_RECORDING, kind)
        check("老格式内容正确读入", (data[0].x, data[0].y) == (5, 6), str(data))

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "script", "steps": [{"x": "abc", "mouse": "瞎写的", "delay_ms": -5}]}')
        loaded, _ = sm.load_script(path)
        check("非法坐标退化为 0", loaded[0].x == 0, str(loaded[0].x))
        check("非法鼠标动作退化为左键", loaded[0].mouse == sm.MOUSE_LEFT, loaded[0].mouse)
        check("负延时归零", loaded[0].delay_ms == 0, str(loaded[0].delay_ms))

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "empty.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "script"}')
        try:
            sm.load_script(path)
            check("没有 steps 时应当报错", False, "没有抛异常")
        except macro_model.MacroFormatError:
            check("没有 steps 会抛 MacroFormatError", True)

    check("每个鼠标动作都有中文名", all(k in sm.MOUSE_ACTION_CN for k in sm.MOUSE_ACTIONS))
    check("步骤概要有内容", "左键单击" in sm.Step(x=1, y=2).summary(), sm.Step(x=1, y=2).summary())


# ------------------------------------------------------------------ 键盘输入


def test_keyinput() -> None:
    print("\n[键盘输入]")

    import tkinter as tk

    import keyinput

    check(
        "SendInput 结构体大小和系统一致",
        keyinput.SENDINPUT_AVAILABLE,
        f"sizeof(INPUT)={ctypes.sizeof(keyinput._INPUT)}，系统要求 {keyinput._EXPECTED_INPUT_SIZE}",
    )
    check("字母可走原生按键路径", keyinput.can_type_natively("a"))
    check("数字可走原生按键路径", keyinput.can_type_natively("7"))
    check("符号可走原生按键路径", keyinput.can_type_natively("!"))
    check("中文不能走原生路径", not keyinput.can_type_natively("中"))
    check("多字符不算单字符", not keyinput.can_type_natively("ab"))

    # 真打一遍。输入框是自己开的，不会打扰用户正在用的窗口。
    root = tk.Tk()
    root.geometry("420x130+80+80")
    root.attributes("-topmost", True)
    entry = tk.Entry(root, font=("Consolas", 16))
    entry.pack(fill="both", expand=True, padx=20, pady=20)
    root.deiconify()
    root.update()
    entry.focus_force()
    for _ in range(20):
        root.update()
        time.sleep(0.02)

    typist = keyinput.Typist()

    def type_and_read(sample: str) -> str:
        entry.delete(0, "end")
        entry.focus_force()
        root.update()
        time.sleep(0.05)
        typist.type_text(sample, char_delay_ms=12)
        for _ in range(20):
            root.update()
            time.sleep(0.01)
        return entry.get()

    for sample in ("abcXYZ", "12345", "Hello123!"):
        got = type_and_read(sample)
        check(f"能打出 {sample!r}", got == sample, f"实际得到 {got!r}")

    got = type_and_read("中文测试")
    check("能打出中文（走 Unicode 通道）", got == "中文测试", f"实际得到 {got!r}")

    got = type_and_read("混合abc中文123")
    check("能打出中英数字混合", got == "混合abc中文123", f"实际得到 {got!r}")

    # 中途停止
    entry.delete(0, "end")
    entry.focus_force()
    root.update()
    time.sleep(0.05)
    typed = typist.type_text("abcdefghij", char_delay_ms=20, should_stop=lambda: True)
    check("should_stop 能立刻中断输入", typed == 0, str(typed))

    root.destroy()


# ------------------------------------------------------------------ 采集抽稀


def make_active_recorder() -> Recorder:
    """造一个已在录制状态的 Recorder，但不挂真实的系统钩子。"""
    rec = Recorder()
    rec._active = True
    rec._t0 = time.perf_counter()
    return rec


def test_recorder() -> None:
    print("\n[采集]")

    rec = make_active_recorder()
    rec._on_move(100, 100, False)
    rec._on_move(101, 100, False)  # 位移 1px，立即跟一个 → 丢掉
    rec._on_move(160, 100, False)  # 位移 60px → 保留
    check("微小抖动被抽稀", len(rec._actions) == 2, f"记录了 {len(rec._actions)} 个：{[a.label() for a in rec._actions]}")
    check("保留的点坐标正确", [(a.x, a.y) for a in rec._actions] == [(100, 100), (160, 100)])

    # 等待超过 MOVE_MIN_DT 之后，即使没怎么移动也要留下采样点
    rec = make_active_recorder()
    rec._on_move(100, 100, False)
    time.sleep(0.03)
    rec._on_move(101, 100, False)
    check("间隔够久的小位移会保留", len(rec._actions) == 2, f"记录了 {len(rec._actions)} 个")

    # 点击前会补一个定位移动
    rec = make_active_recorder()
    rec._on_move(100, 100, False)
    rec._on_click(500, 600, player_mod.Button.left, True, False)
    rec._on_click(500, 600, player_mod.Button.left, False, False)
    check(
        "点击前自动补定位移动",
        [(a.kind, a.x, a.y) for a in rec._actions]
        == [(KIND_MOVE, 100, 100), (KIND_MOVE, 500, 600), (KIND_DOWN, 500, 600), (KIND_UP, 500, 600)],
        str([(a.kind, a.x, a.y) for a in rec._actions]),
    )
    check("按键名称正确", rec._actions[2].button == "left", rec._actions[2].button)

    # 已经在目标位置时不该重复补点
    rec = make_active_recorder()
    rec._on_move(500, 600, False)
    rec._on_click(500, 600, player_mod.Button.left, True, False)
    check("同坐标不重复补移动", len(rec._actions) == 2, f"记录了 {len(rec._actions)} 个")

    # 注入事件
    rec = make_active_recorder()
    rec._on_click(700, 700, player_mod.Button.left, True, True)  # injected=True
    check("默认忽略注入事件", len(rec._actions) == 0, f"记录了 {len(rec._actions)} 个")
    rec.ignore_injected = False
    rec._on_click(700, 700, player_mod.Button.left, True, True)
    check("关掉开关后注入事件会被记录", len(rec._actions) == 2, f"记录了 {len(rec._actions)} 个")

    # 忽略本窗口
    rec = make_active_recorder()
    rec.ignore_rect = (0, 0, 800, 600)
    rec._on_move(400, 300, False)
    check("窗口内的事件被忽略", len(rec._actions) == 0, f"记录了 {len(rec._actions)} 个")
    rec._on_move(900, 300, False)
    check("窗口外的事件正常记录", len(rec._actions) == 1, f"记录了 {len(rec._actions)} 个")

    # 旧版 pynput 不带 injected 参数时也要能跑
    rec = make_active_recorder()
    rec._on_move(50, 50)
    rec._on_click(50, 50, player_mod.Button.right, True)
    rec._on_scroll(50, 50, 0, 1)
    check("兼容不带 injected 的旧回调签名", len(rec._actions) == 3, f"记录了 {len(rec._actions)} 个")
    check("滚轮方向解析正确", rec._actions[2].dy == 1 and rec._actions[2].label() == "滚轮 上")


def test_recorder_stats() -> None:
    print("\n[采集统计]")

    rec = make_active_recorder()
    rec._on_move(100, 100, False)  # 记录
    rec._on_move(101, 100, False)  # 位移 1px 且紧邻 → 抽稀
    rec._on_click(500, 500, player_mod.Button.left, True, True)  # 注入 → 忽略
    rec.ignore_rect = (0, 0, 800, 600)
    rec._on_move(400, 300, False)  # 落在本窗口 → 忽略

    stats = rec.stats()
    check("seen 统计到了全部事件", stats["seen"] == 4, str(stats))
    check("已记录数量正确", stats["recorded"] == 1, str(stats))
    check("注入过滤计数正确", stats["injected"] == 1, str(stats))
    check("本窗口过滤计数正确", stats["window"] == 1, str(stats))
    check("抽稀计数正确", stats["coalesced"] == 1, str(stats))
    check("最近一条事件的描述正确", stats["last"] == "移动 @ 100, 100", stats["last"])

    # 一条都没收到时，界面靠 seen==0 判定"钩子没抓到"
    idle = make_active_recorder()
    check("没收到事件时 seen 为 0", idle.stats()["seen"] == 0, str(idle.stats()))


def test_hotkeys() -> None:
    print("\n[热键]")

    import tkinter as tk

    from pynput import keyboard as kb

    import mouse_macro

    check("F9 转成 <f9>", mouse_macro._key_to_spec(kb.Key.f9) == "<f9>", mouse_macro._key_to_spec(kb.Key.f9))
    check("字母转成小写字符", mouse_macro._key_to_spec(kb.KeyCode.from_char("a")) == "a")
    check("大写字母归一为小写", mouse_macro._key_to_spec(kb.KeyCode.from_char("A")) == "a")
    check("<f9> 显示为 F9", mouse_macro._pretty_hotkey("<f9>") == "F9")
    check("单字符显示为大写", mouse_macro._pretty_hotkey("a") == "A")
    check("空热键显示为未设置", mouse_macro._pretty_hotkey("") == "未设置")

    class DummyListener:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    mouse_macro.keyboard.GlobalHotKeys = DummyListener
    mouse_macro.keyboard.Listener = DummyListener

    root = tk.Tk()
    root.withdraw()
    mouse_macro._apply_scaling(root)

    # 构造时先屏蔽掉读取，否则本机已存在的 settings.json 会让断言变得不确定
    original_load = mouse_macro.MacroApp._load_settings
    mouse_macro.MacroApp._load_settings = lambda self: None
    app = mouse_macro.MacroApp(root)
    mouse_macro.MacroApp._load_settings = original_load

    warnings: list[str] = []
    app._warn = lambda message: warnings.append(message)

    # 配置写到临时文件，别动用户真实的 settings.json
    tmpdir = tempfile.mkdtemp()
    app._settings_path = lambda: os.path.join(tmpdir, "settings.json")

    check("初始为默认热键", app.hotkeys == mouse_macro.DEFAULT_HOTKEYS, str(app.hotkeys))

    # 改键
    app._start_capture("record")
    check("进入改键状态", app._capturing == "record", str(app._capturing))
    check("改键时按钮提示按下按键", "按下" in app._hotkey_buttons["record"].cget("text"))
    app._apply_capture(kb.Key.f6)
    check("热键已改成 F6", app.hotkeys["record"] == "<f6>", app.hotkeys["record"])
    check("改键后退出采集状态", app._capturing is None, str(app._capturing))
    check("按钮文字同步更新", app._hotkey_buttons["record"].cget("text") == "F6")
    check("主按钮文字同步更新", "F6" in app.btn_record.cget("text"), app.btn_record.cget("text"))

    # 和别的功能撞键要被拒绝
    warnings.clear()
    app._start_capture("play")
    app._apply_capture(kb.Key.f6)
    check("撞键被拒绝", len(warnings) == 1, str(warnings))
    check("撞键后原热键不变", app.hotkeys["play"] == "<f10>", str(app.hotkeys))
    check("撞键后退出采集状态", app._capturing is None, str(app._capturing))

    # 取点热键也有自己的坑位，同样不能被抢
    warnings.clear()
    app._start_capture("record")
    app._apply_capture(kb.Key.f8)
    check("取点热键占用的键会被拒绝", len(warnings) == 1, str(warnings))
    check("被拒后录制热键不变", app.hotkeys["record"] == "<f6>", app.hotkeys["record"])

    # Esc 取消
    app._start_capture("stop")
    app._apply_capture(kb.Key.esc)
    check("Esc 取消不改动热键", app.hotkeys["stop"] == "<f11>", str(app.hotkeys))

    # 字母键也可以
    app._start_capture("stop")
    app._apply_capture(kb.KeyCode.from_char("q"))
    check("字母键能设为热键", app.hotkeys["stop"] == "q", app.hotkeys["stop"])

    # 持久化
    app.hotkeys["record"] = "<f7>"
    app._save_settings()
    app.hotkeys["record"] = "<f9>"
    app._load_settings()
    check("热键配置能持久化", app.hotkeys["record"] == "<f7>", app.hotkeys["record"])
    check("配置文件落在预期位置", os.path.isfile(os.path.join(tmpdir, "settings.json")))

    # 下次启动时（一个全新的实例）应当读到刚存下的热键
    fresh = mouse_macro.MacroApp.__new__(mouse_macro.MacroApp)
    fresh.hotkeys = dict(mouse_macro.DEFAULT_HOTKEYS)
    fresh._settings_path = lambda: os.path.join(tmpdir, "settings.json")
    mouse_macro.MacroApp._load_settings(fresh)
    check("新实例启动时读到已保存的热键", fresh.hotkeys["record"] == "<f7>", str(fresh.hotkeys))

    # 坏掉的配置文件不该让程序起不来
    with open(os.path.join(tmpdir, "broken.json"), "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 json")
    app._settings_path = lambda: os.path.join(tmpdir, "broken.json")
    app.hotkeys = dict(mouse_macro.DEFAULT_HOTKEYS)
    app._load_settings()
    check("配置文件损坏时回退到默认值", app.hotkeys == mouse_macro.DEFAULT_HOTKEYS, str(app.hotkeys))

    app._reset_hotkeys()
    check("恢复默认", app.hotkeys == mouse_macro.DEFAULT_HOTKEYS, str(app.hotkeys))

    # 右侧设置栏必须放得下，否则热键面板底部会被裁掉。
    # 这里刻意缩到 minsize，保证最窄的窗口也不会裁掉内容。
    root.geometry(f"{root.minsize()[0]}x{root.minsize()[1]}")
    root.deiconify()
    for _ in range(15):
        root.update()
        time.sleep(0.02)
    column = app._settings_column
    needed = sum(p.winfo_reqheight() for p in column.winfo_children()) + 10
    available = column.winfo_height()
    check(
        "最小窗口尺寸下右侧栏放得下",
        needed <= available,
        f"内容需要 {needed}px，可用 {available}px，溢出 {needed - available}px",
    )
    root.withdraw()

    # 录制 / 回放中不允许改键
    warnings.clear()
    app.recorder._active = True
    app._start_capture("record")
    check("录制中拒绝改键", app._capturing is None and len(warnings) == 1, str(warnings))
    app.recorder._active = False

    app._on_close()
    check("带热键面板的窗口正常关闭", True)


def test_editing() -> None:
    print("\n[列表编辑]")

    import tkinter as tk

    import mouse_macro

    class Dummy:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    mouse_macro.keyboard.GlobalHotKeys = Dummy

    root = tk.Tk()
    root.withdraw()
    mouse_macro._apply_scaling(root)
    app = mouse_macro.MacroApp(root)
    warnings: list[str] = []
    app._warn = lambda message: warnings.append(message)

    def load(times, kinds=None):
        app.actions = [
            Action(kinds[i] if kinds else KIND_MOVE, i, i, t)
            for i, t in enumerate(times)
        ]
        app._render_actions()
        for _ in range(3):
            root.update()

    load([0.0, 10.0, 20.0, 30.0])
    check("列表行数正确", len(app.tree.get_children()) == 4, str(len(app.tree.get_children())))

    items = app.tree.get_children()
    app.tree.selection_set(items[1])
    for _ in range(2):
        root.update()
    check("选中提示正确", app.var_selection.get() == "已选 1 项", app.var_selection.get())

    app.delete_selected()
    for _ in range(2):
        root.update()
    check("删除后动作数正确", len(app.actions) == 3, str(len(app.actions)))
    check("删除后列表同步", len(app.tree.get_children()) == 3, str(len(app.tree.get_children())))
    check(
        "删除后时间轴收紧",
        [a.t for a in app.actions] == [0.0, 10.0, 20.0],
        str([a.t for a in app.actions]),
    )
    check("删除后选中提示被清空", app.var_selection.get() == "", app.var_selection.get())

    # 多选删除
    load([0.0, 100.0, 200.0, 300.0])
    items = app.tree.get_children()
    app.tree.selection_set(items[0], items[2])
    for _ in range(2):
        root.update()
    check("多选提示正确", app.var_selection.get() == "已选 2 项", app.var_selection.get())
    app.delete_selected()
    # 删掉下标 0 和 2：动作 0 的等待本来就是 0（它是第一个），删它不缩短时间轴；
    # 动作 2 的等待是 100ms，被一并删掉。所以剩下动作 1 保留 100ms 等待、
    # 动作 3 的重算后等待也是 100ms。
    check("多选删除结果正确", [a.t for a in app.actions] == [100.0, 200.0], str([a.t for a in app.actions]))
    check("多选删除后坐标正确", [a.x for a in app.actions] == [1, 3], str([a.x for a in app.actions]))

    # 全选
    load([0.0, 50.0, 100.0])
    app.select_all()
    for _ in range(2):
        root.update()
    check("全选选中所有行", len(app.tree.selection()) == 3, str(len(app.tree.selection())))
    app.delete_selected()
    check("全选删除后为空", app.actions == [] and len(app.tree.get_children()) == 0, str(app.actions))

    # 没选中时给提示而不是静默
    load([0.0])
    warnings.clear()
    app.delete_selected()
    check("没选中时给出提示", len(warnings) == 1, str(warnings))
    check("没选中时不会误删", len(app.actions) == 1, str(len(app.actions)))

    # 从选中处回放
    load([0.0, 100.0, 200.0, 300.0])
    captured: dict = {}

    class FakePlayer:
        running = False

        def start(self, steps, **kwargs):
            captured["steps"] = list(steps)

    real_player = app.player
    app.player = FakePlayer()

    items = app.tree.get_children()
    app.tree.selection_set(items[2])
    for _ in range(2):
        root.update()
    app.play_from_selection()
    check("从选中处回放带上了后续动作", [a.x for a in captured.get("steps", [])] == [2, 3], str([a.x for a in captured.get("steps", [])]))
    check("从选中处回放给出了提示", "从第 3 个动作" in app.var_status.get(), app.var_status.get())

    app.tree.selection_remove(*app.tree.selection())
    captured.clear()
    warnings.clear()
    app.play_from_selection()
    check("没选中时拒绝从选中处回放", captured == {} and len(warnings) == 1, str(warnings))

    captured.clear()
    app.start_play()
    check("普通回放从第一个动作开始", [a.x for a in captured.get("steps", [])] == [0, 1, 2, 3], str([a.x for a in captured.get("steps", [])]))

    # 选中最后一行的下一格不该越界
    app.tree.selection_set(app.tree.get_children()[-1])
    captured.clear()
    app.play_from_selection()
    check("从最后一个动作回放只带一个", [a.x for a in captured.get("steps", [])] == [3], str([a.x for a in captured.get("steps", [])]))

    app.player = real_player
    app._set_playing_ui(False)

    # 录制中不允许编辑
    warnings.clear()
    app.recorder._active = True
    before = len(app.actions)
    app.tree.selection_set(app.tree.get_children()[0])
    app.delete_selected()
    app.select_all()
    check("录制中拒绝删除", len(app.actions) == before, str(len(app.actions)))
    app.recorder._active = False

    app._on_close()
    check("编辑功能接入后窗口正常关闭", True)


def test_script_tab() -> None:
    print("\n[剧本页签]")

    import tkinter as tk

    import mouse_macro
    import script_model as sm

    class Dummy:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    mouse_macro.keyboard.GlobalHotKeys = Dummy

    root = tk.Tk()
    root.withdraw()
    mouse_macro._apply_scaling(root)
    app = mouse_macro.MacroApp(root)
    warnings: list[str] = []
    app._warn = lambda message: warnings.append(message)

    def settle(times: int = 6) -> None:
        """把挂起的 Tk 事件跑完。

        切页签会触发 <<NotebookTabChanged>>，而它是延迟派发的，
        会重建列表并清掉选中。做依赖选中的操作之前必须先让它落定。
        """
        for _ in range(times):
            root.update()
            time.sleep(0.01)

    settle()
    check("默认停在录制页签", not app._on_script_tab())
    check("一共两个页签", app.notebook.index("end") == 2, str(app.notebook.index("end")))

    # 添加步骤（对话框打桩，避免模态窗口卡住测试）
    app._edit_step_dialog = lambda step, index=None: sm.Step(
        x=100, y=200, mouse=sm.MOUSE_RIGHT, text="admin", delay_ms=800
    )
    app.add_step()
    for _ in range(3):
        root.update()
    check("添加步骤成功", len(app.steps) == 1, str(len(app.steps)))
    check("列表渲染出步骤", len(app.step_tree.get_children()) == 1)
    row = app.step_tree.item(app.step_tree.get_children()[0], "values")
    check(
        "步骤行内容正确",
        row[1] == "100, 200" and row[2] == "右键单击" and row[3] == "admin" and row[4] == "800 ms",
        str(row),
    )
    check("步数提示更新", app.var_step_count.get() == "共 1 步", app.var_step_count.get())

    # 热键取点：按当前位置加一步，不弹对话框
    app.quick_pick_step()
    check("热键取点加了一步", len(app.steps) == 2, str(len(app.steps)))
    check("热键取点默认左键单击", app.steps[-1].mouse == sm.MOUSE_LEFT, app.steps[-1].mouse)
    check("热键取点会切到剧本页签", app._on_script_tab())
    check("热键取到的坐标就是当前鼠标位置", (app.steps[-1].x, app.steps[-1].y) is not None)

    # 对话框取消就不该添加
    app._edit_step_dialog = lambda step, index=None: None
    before = len(app.steps)
    app.add_step()
    check("取消对话框不添加步骤", len(app.steps) == before, str(len(app.steps)))

    # 编辑
    app._edit_step_dialog = lambda step, index=None: sm.Step(
        x=7, y=8, mouse=sm.MOUSE_NONE, delay_ms=1
    )
    settle()
    app.step_tree.selection_set(app.step_tree.get_children()[0])
    settle(3)
    check("编辑前选中确实生效", app._selected_step_indices() == [0], str(app._selected_step_indices()))
    app.edit_step()
    check("编辑生效", (app.steps[0].x, app.steps[0].y) == (7, 8), str(app.steps[0]))

    # 调整顺序
    app.steps = [sm.Step(x=i) for i in range(3)]
    app._render_steps()
    settle(3)
    app.step_tree.selection_set(app.step_tree.get_children()[0])
    settle(3)
    app.move_step(1)
    check("下移交换顺序", [s.x for s in app.steps] == [1, 0, 2], str([s.x for s in app.steps]))
    app.move_step(-1)
    check("上移换回来", [s.x for s in app.steps] == [0, 1, 2], str([s.x for s in app.steps]))
    app.move_step(-1)
    check("已经在顶部时上移无效", [s.x for s in app.steps] == [0, 1, 2], str([s.x for s in app.steps]))

    # 删除
    app.step_tree.selection_set(app.step_tree.get_children()[1])
    for _ in range(2):
        root.update()
    app.delete_steps()
    check("删除步骤", [s.x for s in app.steps] == [0, 2], str([s.x for s in app.steps]))

    # 空选择时三处操作都要给提示，而不是静默
    warnings.clear()
    app.step_tree.selection_remove(*app.step_tree.selection())
    for _ in range(2):
        root.update()
    app.delete_steps()
    app.edit_step()
    app.move_step(1)
    check("没选中时都给出提示", len(warnings) == 3, str(warnings))

    # 从剧本页签回放：内容必须先展开成动作
    app.steps = [
        sm.Step(x=11, y=22, mouse=sm.MOUSE_LEFT, delay_ms=0),
        sm.Step(x=33, y=44, mouse=sm.MOUSE_NONE, delay_ms=0),
    ]
    app._render_steps()
    captured: dict = {}

    class FakePlayer:
        running = False

        def start(self, steps, **kwargs):
            captured["steps"] = list(steps)
            captured["kw"] = kwargs

    real_player = app.player
    app.player = FakePlayer()
    app.start_play()
    check(
        "剧本回放会展开成动作",
        [a.kind for a in captured["steps"]] == [KIND_MOVE, KIND_DOWN, KIND_UP, KIND_MOVE],
        str([a.kind for a in captured["steps"]]),
    )
    check(
        "展开后坐标正确",
        [a.x for a in captured["steps"]] == [11, 11, 11, 33],
        str([a.x for a in captured["steps"]]),
    )
    check("回放设置带上了输入方式", "native_typing" in captured["kw"], str(list(captured["kw"])))

    app.step_tree.selection_set(app.step_tree.get_children()[1])
    for _ in range(2):
        root.update()
    captured.clear()
    app.play_from_selection()
    check("剧本从选中步骤回放", [a.x for a in captured["steps"]] == [33], str([a.x for a in captured["steps"]]))

    app.player = real_player
    app._set_playing_ui(False)

    # 录制页签为空时不给回放
    app.notebook.select(0)
    for _ in range(2):
        root.update()
    captured.clear()
    warnings.clear()
    app.start_play()
    check("录制为空时给提示且不回放", len(warnings) == 1 and captured == {}, str(warnings))

    # 保存 / 加载要跟着当前页签走
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "s.json")
    mouse_macro.filedialog.asksaveasfilename = lambda **kwargs: path
    app.notebook.select(1)
    for _ in range(2):
        root.update()
    app.steps = [sm.Step(x=5, y=6, text="hi")]
    app._render_steps()
    app.save_macro()
    check("剧本页签保存出文件", os.path.isfile(path))
    kind, data, _ = sm.load_any(path)
    check("存下来的是剧本格式", kind == KIND_SCRIPT, kind)
    check("存下来的步骤正确", data == [sm.Step(x=5, y=6, text="hi")], str(data))

    app.steps = []
    app._render_steps()
    app.notebook.select(0)
    for _ in range(2):
        root.update()
    mouse_macro.filedialog.askopenfilename = lambda **kwargs: path
    app.load_macro()
    check("加载剧本后自动切到剧本页签", app._on_script_tab())
    check("加载的步骤正确", len(app.steps) == 1 and app.steps[0].x == 5, str(app.steps))

    # 清空
    app.clear_steps()
    check("清空剧本", app.steps == [] and len(app.step_tree.get_children()) == 0)

    # 剧本工具栏必须放得下，否则最右边几个按钮会被裁掉
    app.notebook.select(1)
    root.geometry(f"{root.minsize()[0]}x{root.minsize()[1]}")
    root.deiconify()
    settle(12)
    bar = app._step_bar
    check(
        "最小尺寸下剧本工具栏放得下",
        bar.winfo_reqwidth() <= app.notebook.winfo_width(),
        f"工具栏需要 {bar.winfo_reqwidth()}px，可用 {app.notebook.winfo_width()}px",
    )
    root.withdraw()
    settle(3)

    app._on_close()
    check("剧本页签窗口正常关闭", True)


def test_step_dialog() -> None:
    print("\n[步骤对话框]")

    import tkinter as tk

    import step_dialog
    from step_dialog import StepDialog

    import script_model as sm

    root = tk.Tk()
    root.withdraw()

    original_warn = step_dialog.messagebox.showwarning
    seen: list = []
    step_dialog.messagebox.showwarning = lambda *a, **k: seen.append(a)

    # 编辑已有步骤：字段要预填
    original = sm.Step(x=100, y=200, mouse=sm.MOUSE_DOUBLE, text="abc", delay_ms=700)
    dlg = StepDialog(root, original, 2)
    for _ in range(8):
        root.update()
    check("预填坐标", (dlg.var_x.get(), dlg.var_y.get()) == ("100", "200"), dlg.var_x.get())
    check("预填鼠标动作", dlg.var_mouse.get() == "双击", dlg.var_mouse.get())
    check("预填输入文字", dlg.var_text.get() == "abc", dlg.var_text.get())
    check("预填延时", dlg.var_delay.get() == "700", dlg.var_delay.get())
    dlg._confirm()
    check("确定后原样返回步骤", dlg.result == original, str(dlg.result))

    # 新建：默认值
    dlg = StepDialog(root)
    for _ in range(5):
        root.update()
    check("新建默认左键单击", dlg.var_mouse.get() == "左键单击", dlg.var_mouse.get())
    check("新建默认延时是 500", dlg.var_delay.get() == str(sm.DEFAULT_DELAY_MS), dlg.var_delay.get())
    check("新建默认没有输入文字", dlg.var_text.get() == "")

    dlg._cancel()
    check("取消返回 None", dlg.result is None, str(dlg.result))

    # 所有鼠标动作都能选中
    dlg = StepDialog(root)
    for _ in range(5):
        root.update()
    options = list(dlg.winfo_children())  # 只是确认窗口建起来了
    check("对话框建起来了", bool(options))
    for key in sm.MOUSE_ACTIONS:
        dlg.var_mouse.set(sm.MOUSE_ACTION_CN[key])
        dlg.var_x.set("10")
        dlg.var_y.set("20")
        dlg.var_delay.set("0")
        seen.clear()
        dlg._confirm()
        check(
            f"{sm.MOUSE_ACTION_CN[key]}能正常确定",
            dlg.result is not None and dlg.result.mouse == key,
            f"{dlg.result} / 警告 {seen}",
        )
        if key != sm.MOUSE_ACTIONS[-1]:
            dlg = StepDialog(root, dlg.result)
            for _ in range(3):
                root.update()
    dlg._cancel()

    # 非法输入要被拦下，而不是把坏数据写进去
    dlg = StepDialog(root)
    for _ in range(5):
        root.update()

    seen.clear()
    dlg.var_x.set("不是数字")
    dlg._confirm()
    check("非法 X 被拦下", dlg.result is None and len(seen) == 1, f"{dlg.result} / {seen}")

    dlg.var_x.set("10")
    dlg.var_y.set("")
    seen.clear()
    dlg._confirm()
    check("空 Y 被拦下", dlg.result is None and len(seen) == 1, f"{dlg.result} / {seen}")

    dlg.var_y.set("20")
    dlg.var_delay.set("-5")
    seen.clear()
    dlg._confirm()
    check("负延时被拦下", dlg.result is None and len(seen) == 1, f"{dlg.result} / {seen}")

    dlg.var_delay.set("abc")
    seen.clear()
    dlg._confirm()
    check("非数字延时被拦下", dlg.result is None and len(seen) == 1, f"{dlg.result} / {seen}")

    dlg.var_delay.set("300")
    dlg.var_x.set("70000")
    seen.clear()
    dlg._confirm()
    check("超范围坐标被拦下", dlg.result is None and len(seen) == 1, f"{dlg.result} / {seen}")

    # 负坐标是合法的（副屏在主屏左边就是负数）
    dlg.var_x.set("-1500")
    dlg.var_y.set("300")
    dlg.var_delay.set("0")
    dlg.var_text.set("中文输入 ok")
    dlg._confirm()
    check(
        "负坐标和中文输入都能通过",
        dlg.result is not None and dlg.result.x == -1500 and dlg.result.text == "中文输入 ok",
        str(dlg.result),
    )

    # 超长文字要被截断，免得文件失控
    dlg = StepDialog(root, sm.Step(x=0, y=0, text="x" * 5000, delay_ms=0))
    for _ in range(3):
        root.update()
    dlg._confirm()
    check("超长输入被截断", dlg.result is not None and len(dlg.result.text) == 2000, str(len(dlg.result.text or "")))

    # 小数会被取整
    dlg = StepDialog(root, sm.Step(x=0, y=0, delay_ms=0))
    for _ in range(3):
        root.update()
    dlg.var_x.set("100.7")
    dlg.var_delay.set("250.9")
    dlg._confirm()
    check("小数坐标被取整", dlg.result is not None and dlg.result.x == 100, str(dlg.result))
    check("小数延时被取整", dlg.result is not None and dlg.result.delay_ms == 250, str(dlg.result))

    step_dialog.messagebox.showwarning = original_warn
    root.destroy()
    check("对话框测试结束", True)


def test_main_entry() -> None:
    print("\n[入口]")
    import mouse_macro

    # 入口函数存在且能拿到，避免"bat 跑了但什么都没发生"
    check("main 可调用", callable(mouse_macro.main))


# ------------------------------------------------------------------ 回放


def run_player(actions, **kwargs):
    FakeController.log = []
    player_mod.Controller = FakeController
    p = player_mod.Player()
    done = threading.Event()
    result = {}

    def on_done(error, completed):
        result["error"] = error
        result["completed"] = completed
        done.set()

    kwargs.setdefault("on_done", on_done)
    p.start(actions, **kwargs)
    if not done.wait(15):
        p.stop()
        done.wait(3)
    return p, result


def test_player() -> None:
    print("\n[回放]")

    actions = [
        Action(KIND_MOVE, 100, 100, 0.0),
        Action(KIND_MOVE, 200, 200, 100.0),
        Action(KIND_DOWN, 200, 200, 110.0, button="left"),
        Action(KIND_UP, 200, 200, 150.0, button="left"),
        Action(KIND_SCROLL, 200, 200, 200.0, dy=2),
    ]

    _, result = run_player(actions, repeat=1, speed=1.0)
    log = FakeController.log
    check("单轮回放无异常", result.get("error") is None, str(result.get("error")))
    check("单轮计数为 1", result.get("completed") == 1, str(result.get("completed")))
    check(
        "按键与滚轮顺序正确",
        events(log) == [("down", "left"), ("up", "left"), ("scroll", 0, 2)],
        str(log),
    )
    check("移动轨迹的落点正确", (100, 100) in positions(log) and positions(log)[-1] == (200, 200), str(positions(log)))
    check("滚轮数值正确", ("scroll", 0, 2) in log, str(log))

    # 重复次数
    _, result = run_player(actions, repeat=3, speed=8.0, gap_ms=0)
    check("重复 3 轮", result.get("completed") == 3, str(result.get("completed")))
    check("3 轮共出现 3 次按下", kinds(FakeController.log).count("down") == 3)

    # 循环间隔
    started = time.perf_counter()
    actions_fast = [Action(KIND_MOVE, 5, 5, 0.0)]
    _, result = run_player(actions_fast, repeat=3, speed=1.0, gap_ms=200)
    elapsed = time.perf_counter() - started
    check("循环间隔被计入", 0.35 <= elapsed < 1.2, f"耗时 {elapsed:.2f}s")

    # 倍速
    actions_slow = [Action(KIND_MOVE, 0, 0, 0.0), Action(KIND_MOVE, 1, 1, 400.0)]
    started = time.perf_counter()
    run_player(actions_slow, repeat=1, speed=4.0)
    fast = time.perf_counter() - started
    started = time.perf_counter()
    run_player(actions_slow, repeat=1, speed=1.0)
    slow = time.perf_counter() - started
    check("4 倍速明显更快", fast < slow * 0.6, f"fast={fast:.2f}s slow={slow:.2f}s")
    check("1 倍速接近原始时长", 0.3 <= slow < 0.9, f"{slow:.2f}s")

    # 跳过移动轨迹
    _, result = run_player(actions, repeat=1, use_moves=False)
    log = FakeController.log
    check("按键与滚轮不受影响", events(log) == [("down", "left"), ("up", "left"), ("scroll", 0, 2)], str(log))
    check("轨迹点只剩点击时的定位", positions(log) == [(200, 200)] * 3, str(positions(log)))

    # 中途停止：应当很快返回，且把按住的键释放掉
    long_actions = [Action(KIND_DOWN, 10, 10, 0.0, button="left")] + [
        Action(KIND_MOVE, 10 + i, 10, 20.0 * (i + 1)) for i in range(200)
    ]
    FakeController.log = []
    player_mod.Controller = FakeController
    p = player_mod.Player()
    done = threading.Event()
    result = {}

    def on_done(error, completed):
        result["error"] = error
        result["completed"] = completed
        done.set()

    p.start(long_actions, repeat=1, speed=1.0, on_done=on_done)
    time.sleep(0.4)
    started = time.perf_counter()
    p.stop()
    ok = done.wait(3)
    stop_cost = time.perf_counter() - started
    check("停止请求能被及时响应", ok and stop_cost < 0.5, f"{stop_cost:.2f}s")
    check("停止时按住的键被释放", ("up", "left") in events(FakeController.log), str(events(FakeController.log)[-3:]))
    check("中途停止不计入完成轮数", result.get("completed") == 0, str(result.get("completed")))

    # 空动作列表
    player_mod.Controller = FakeController
    p = player_mod.Player()
    try:
        p.start([], repeat=1)
        check("空动作列表应当报错", False, "没有抛异常")
    except ValueError:
        check("空动作列表会抛 ValueError", True)

    # 非法倍速会被夹紧而不是除零崩掉
    check("极小的倍速被夹到下限", player_mod._clamp_speed(0) == player_mod.MIN_SPEED)
    check("NaN 退化为 1.0", player_mod._clamp_speed(float("nan")) == 1.0)
    check("超大的倍速被夹到上限", player_mod._clamp_speed(999) == player_mod.MAX_SPEED)


# ------------------------------------------------------------------ 界面


def test_gui() -> None:
    print("\n[界面]")

    try:
        import tkinter as tk
    except ImportError:
        check("tkinter 可用", False, "导入失败")
        return

    import mouse_macro

    class DummyHotkeys:
        def start(self):
            pass

        def stop(self):
            pass

    mouse_macro.keyboard.GlobalHotKeys = lambda *a, **k: DummyHotkeys()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        check("能创建窗口", False, str(exc))
        return

    root.withdraw()
    mouse_macro._apply_scaling(root)
    app = mouse_macro.MacroApp(root)

    app.actions = [
        Action(KIND_MOVE, 10, 20, 0.0),
        Action(KIND_DOWN, 10, 20, 12.0, button="left"),
        Action(KIND_UP, 10, 20, 60.0, button="left"),
    ]
    app._render_actions()
    for _ in range(3):
        root.update()
    check("动作列表渲染正确", len(app.tree.get_children()) == 3, str(len(app.tree.get_children())))
    first = app.tree.item(app.tree.get_children()[0], "values")
    check("首行内容正确", first[1] == "移动" and first[2] == "10, 20", str(first))
    check("首行间隔显示为占位符", first[3] == "—", str(first))
    second = app.tree.item(app.tree.get_children()[1], "values")
    check("第二行间隔为 12 ms", second[3] == "12 ms", str(second))

    # 设置校验
    app.var_repeat.set(5)
    app.var_speed.set(2.0)
    app.var_gap.set(300)
    settings = app._read_settings()
    check(
        "设置读取正确",
        settings == {
            "repeat": 5,
            "speed": 2.0,
            "gap_ms": 300,
            "use_moves": True,
            "native_typing": False,
        },
        str(settings),
    )

    app.var_infinite.set(True)
    check("无限循环映射为 repeat=0", app._read_settings()["repeat"] == 0)
    app.var_infinite.set(False)

    app.var_speed.set(99.0)
    app._warn = lambda message: None  # 测试里不要弹窗
    check("超出范围的速度被拒绝", app._read_settings() is None)
    app.var_speed.set(1.0)

    app.var_repeat.set(0)
    check("重复次数 0 被拒绝", app._read_settings() is None)
    app.var_repeat.set(1)

    check("宏文件目录可创建", os.path.isdir(app._macro_dir()))

    app._on_close()
    check("窗口正常关闭", True)


def main() -> int:
    print("鼠标宏自检")
    test_model()
    test_remove_actions()
    test_script_model()
    test_keyinput()
    test_recorder()
    test_recorder_stats()
    test_player()
    test_gui()
    test_hotkeys()
    test_editing()
    test_script_tab()
    test_step_dialog()
    test_main_entry()

    print(f"\n通过 {PASSED} 项，失败 {len(FAILED)} 项。")
    for name in FAILED:
        print(f"  - {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())