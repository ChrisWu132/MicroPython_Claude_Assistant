#!/usr/bin/env python3
# tests/test_session_slot_stability.py
#
# Issue #6 复现 + 回归测试：
#   Session 超时清理后重新激活，设备端 Tab 槽位漂移 + 历史误清。
#
# 期望：当前 main 上失败（暴露 bug），修复后通过。
#
# 模拟两层：
#   1. daemon 层：直接驱 _handle_envelope + _pusher_tick，捕获 wire 序列
#   2. device 层：用一个 _FakeDevice 重放 wire，跟踪 _slot_names 和 history.clear() 次数
#      （照搬 device/display_renderer.py:566-581 逻辑）

import asyncio
import json
import os
import sys

# Windows PowerShell 默认 GBK；强制 UTF-8 stdout 避免中文 mojibake
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "daemon"))
import ble_daemon as d  # noqa: E402

MAX_SESSIONS = 5

# ── mock time ──────────────────────────────────────────
_clock = [100.0]


class _MockTime:
    @staticmethod
    def time():
        return _clock[0]


def _set(t): _clock[0] = t
def _adv(dt): _clock[0] += dt


# ── capture _send ─────────────────────────────────────
_sent_wires = []


async def _capture_send(payload):
    _sent_wires.append(json.loads(json.dumps(payload)))  # deep copy
    return True


class _MockTransport:
    def connected(self): return True


def _reset():
    d._sessions.clear()
    # v6 关键：清 slot_assignments，否则跨测试会污染（其它测试共用 module-level state）
    for i in range(len(d._slot_assignments)):
        d._slot_assignments[i] = None
    d._dirty = False
    d._stub = True
    d._last_pushed_wire = None
    d._transport = _MockTransport()
    _sent_wires.clear()
    _set(100.0)


def _env_user_prompt(sid, cwd):
    return {"type": "event", "v": 2,
            "event": {"kind": "user_prompt", "prompt": "go"},
            "generic": {"session_id": sid, "cwd": cwd,
                        "transcript_path": "/x.j",
                        "hook_event_name": "UserPromptSubmit",
                        "permission_mode": "auto"}}


def _env_stop(sid, cwd):
    return {"type": "event", "v": 2,
            "event": {"kind": "stop"},
            "generic": {"session_id": sid, "cwd": cwd,
                        "transcript_path": "/x.j",
                        "hook_event_name": "Stop",
                        "permission_mode": "auto"}}


# ── 设备端最小模拟（照搬 display_renderer.py:566-581 + v6 slot 派发） ──
class _FakeDevice:
    """模拟 ESP32 panel 的 _slot_names / _histories 行为，看会不会误清。

    slot_mode='index'：v5 老固件路径，按数组下标渲染（用于演示 bug 仍在）
    slot_mode='slot' ：v6 新固件路径，按 wire entry 的 slot 字段渲染（修复后）
    """
    def __init__(self, slot_mode="slot"):
        self.slot_mode = slot_mode
        self.slot_names = [""] * MAX_SESSIONS
        self.histories = [[f"<initial-history-slot-{i}>"] for i in range(MAX_SESSIONS)]
        self.clear_events = []

    def render(self, wire):
        sessions = wire.get("ss", [])
        slot_table = [None] * MAX_SESSIONS
        if self.slot_mode == "slot":
            for s in sessions:
                slot = s.get("slot", -1)
                if 0 <= slot < MAX_SESSIONS:
                    slot_table[slot] = s
        else:  # index 模式
            for i, s in enumerate(sessions[:MAX_SESSIONS]):
                slot_table[i] = s
        for i in range(MAX_SESSIONS):
            self._update_tab(i, slot_table[i])

    def _update_tab(self, index, sess):
        if sess is None:
            self.slot_names[index] = ""
            return
        new_name = sess.get("n", "")
        if new_name != self.slot_names[index]:
            old_name = self.slot_names[index]
            old_history = list(self.histories[index])
            self.histories[index] = []
            self.clear_events.append({
                "slot": index,
                "old_name": old_name,
                "new_name": new_name,
                "cleared_history": old_history,
            })
            self.slot_names[index] = new_name


# ── 复现：slot 漂移 + 历史误清 ──
async def test_session_slot_drift_after_cleanup():
    """
    场景：
      t=100: prompt sid=A cwd=/projA → S1=projA
      t=101: prompt sid=B cwd=/projB → S2=projB
      t=102: stop A (completed_until=104, last_activity_ts=102)
      t=115: cleanup 触发（102 + SESSION_CLEANUP_S 10 = 112 已过）→ del A
      t=116: prompt A 重发 → setdefault 把 A 插到 dict 末尾
      期望：A 还在 S1 槽
      实际：A 跑到 S2 槽
    """
    _reset()
    device = _FakeDevice(slot_mode="slot")  # v6：按 slot 字段渲染
    last = None

    def _accum_history(label):
        """模拟用户在两个 session 各积累几条对话历史。"""
        for i in range(MAX_SESSIONS):
            if device.slot_names[i]:
                device.histories[i].append(f"<{label}:{device.slot_names[i]}-msg>")

    # ① t=100 A 起对话
    _set(100.0)
    await d._handle_envelope(_env_user_prompt("sid-A", "/projA"))
    # ② t=101 B 起对话
    _set(101.0)
    await d._handle_envelope(_env_user_prompt("sid-B", "/projB"))
    last = await d._pusher_tick(last)
    wire1 = _sent_wires[-1]
    print(f"\n[wire1 @t=101] {json.dumps(wire1, ensure_ascii=False)}")
    device.render(wire1)
    _accum_history("turn1")
    _accum_history("turn1")  # 每个 session 模拟 2 条历史
    print(f"  device.slot_names = {device.slot_names}")
    print(f"  device.histories  = {device.histories[:2]}")

    names_w1 = [s.get("n") for s in wire1["ss"]]
    assert names_w1 == ["projA", "projB"], f"initial wire order: {names_w1}"

    # ③ t=102 stop A
    _set(102.0)
    await d._handle_envelope(_env_stop("sid-A", "/projA"))
    last = await d._pusher_tick(last)
    wire2 = _sent_wires[-1]
    print(f"\n[wire2 @t=102 stop A] {json.dumps(wire2, ensure_ascii=False)}")
    device.render(wire2)
    print(f"  device.slot_names = {device.slot_names}")

    # ④ 模拟 5Hz pusher 持续推送，让 wire 自然过渡 C→I→消失
    #    在 t=104 (C 到期)、t=112 (recently 到期) 各推一帧
    for tt in (104.5, 113.0):
        _set(tt)
        last = await d._pusher_tick(last)
        if _sent_wires[-1] != wire2 and (not wire2 or _sent_wires[-1] != _sent_wires[-2]):
            w = _sent_wires[-1]
            print(f"\n[wire @t={tt}] {json.dumps(w, ensure_ascii=False)}")
            device.render(w)
            print(f"  device.slot_names = {device.slot_names}")
            wire2 = w

    # ⑤ t=115 cleanup（102+10=112 已过）
    _set(115.0)
    last = await d._pusher_tick(last)
    assert "sid-A" not in d._sessions, "A should be cleaned up"
    wire3 = _sent_wires[-1]
    print(f"\n[wire3 @t=115 after cleanup] {json.dumps(wire3, ensure_ascii=False)}")
    device.render(wire3)
    print(f"  device.slot_names = {device.slot_names}")
    # 在 A 缺席期间 B 继续累积历史
    _accum_history("turn2")

    # ⑥ t=116 A 重发对话
    _set(116.0)
    await d._handle_envelope(_env_user_prompt("sid-A", "/projA"))
    last = await d._pusher_tick(last)
    wire4 = _sent_wires[-1]
    print(f"\n[wire4 @t=116 A 重连] {json.dumps(wire4, ensure_ascii=False)}")
    device.render(wire4)
    print(f"  device.slot_names = {device.slot_names}")
    print(f"  device.histories  = {device.histories[:2]}")

    # ── slot 稳定性断言（v6 修复要点）──
    # 取每个 wire 里 projA 对应 entry 的 slot 字段，跨 cleanup→重连应保持一致
    def _slot_of(wire, name):
        for s in wire["ss"]:
            if s.get("n") == name:
                return s.get("slot")
        return None

    slot_A_w1 = _slot_of(wire1, "projA")
    slot_A_w4 = _slot_of(wire4, "projA")
    slot_B_w1 = _slot_of(wire1, "projB")
    slot_B_w4 = _slot_of(wire4, "projB")
    names_w4 = [s.get("n") for s in wire4["ss"]]
    print(f"\n=== v6 修复验证 ===")
    print(f"初始 wire 顺序 : {[s.get('n') for s in wire1['ss']]}")
    print(f"A 重连后顺序   : {names_w4}（数组顺序可不一致——device 按 slot 渲染）")
    print(f"projA: slot {slot_A_w1} → {slot_A_w4}（必须一致）")
    print(f"projB: slot {slot_B_w1} → {slot_B_w4}（必须一致）")
    print(f"history clear 事件: {len(device.clear_events)} 次")
    for ev in device.clear_events:
        print(f"  slot={ev['slot']}  {ev['old_name']!r} → {ev['new_name']!r}  "
              f"cleared {len(ev['cleared_history'])} items")

    # 断言 1：wire entry 必须带 slot 字段（v6 协议契约）
    for s in wire1["ss"] + wire4["ss"]:
        assert "slot" in s, f"v6 wire entry missing slot field: {s}"
        assert isinstance(s["slot"], int) and s["slot"] >= 0, f"invalid slot: {s}"

    # 断言 2：projA / projB 的 slot 跨 cleanup→重连必须保持稳定
    assert slot_A_w1 == slot_A_w4, (
        f"projA slot 漂移：{slot_A_w1} → {slot_A_w4}（v6 应保持稳定）"
    )
    assert slot_B_w1 == slot_B_w4, (
        f"projB slot 漂移：{slot_B_w1} → {slot_B_w4}"
    )

    # 断言 3：device 端按 slot 渲染时不应发生 name swap clear
    unexpected_clears = [
        ev for ev in device.clear_events
        if ev["old_name"] and ev["new_name"] and ev["old_name"] != ev["new_name"]
    ]
    assert not unexpected_clears, (
        f"device 端发生 name 互换清历史 ×{len(unexpected_clears)}：{unexpected_clears}"
    )
    print("  ok  v6 修复：slot 稳定 + 历史无误清")


async def main():
    orig_time = d.time
    orig_send = d._send
    d.time = _MockTime()
    d._send = _capture_send
    try:
        await test_session_slot_drift_after_cleanup()
        print("\nPASS")
        return 0
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        return 1
    finally:
        d.time = orig_time
        d._send = orig_send


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
