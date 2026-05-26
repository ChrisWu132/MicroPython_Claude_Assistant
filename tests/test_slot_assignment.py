#!/usr/bin/env python3
# tests/test_slot_assignment.py
#
# 覆盖 daemon 端 v6 slot 映射的边界场景（issue #6 修复后的回归保护）：
#   1. 第 6 个 sid 进来：返回 slot=-1 不覆盖现有 slot
#   2. sid 删除后槽位释放可被新 sid 抢占
#   3. _retire_stale_waiting_sessions 路径也释放 slot
#   4. _pusher_tick cleanup 后 _mark_dirty 触发下一帧推送（sibling bug 回归）
#
# 与 test_session_slot_stability.py 互补：那个测端到端漂移现象，这个测 daemon
# 内部 slot 状态机的健全性。

import asyncio
import json
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "daemon"))
import ble_daemon as d  # noqa: E402


_clock = [100.0]


class _MockTime:
    @staticmethod
    def time():
        return _clock[0]


def _set(t): _clock[0] = t
def _adv(dt): _clock[0] += dt


_sent_wires = []


async def _capture_send(payload):
    _sent_wires.append(json.loads(json.dumps(payload)))
    return True


class _MockTransport:
    def connected(self): return True


def _reset():
    d._sessions.clear()
    for i in range(len(d._slot_assignments)):
        d._slot_assignments[i] = None
    d._dirty = False
    d._stub = True
    d._last_pushed_wire = None
    d._transport = _MockTransport()
    _sent_wires.clear()
    _set(100.0)


def _env_prompt(sid, cwd):
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


def _assert(cond, msg):
    if not cond:
        print(f"  FAIL: {msg}")
        raise AssertionError(msg)


# ── Test 1: 6th session overflow ──
async def test_overflow_returns_minus_one():
    _reset()
    # 占满 5 个 slot
    for i in range(5):
        await d._handle_envelope(_env_prompt(f"sid-{i}", f"/proj{i}"))
    _assert(d._slot_assignments.count(None) == 0,
            f"5 sids 后 slot 应全占满，实际 {d._slot_assignments}")

    # 第 6 个进来
    slot = d._assign_slot("sid-overflow")
    _assert(slot == -1, f"溢出时 _assign_slot 应返 -1，实际 {slot}")

    # _sid_to_slot 应同样返 -1
    _assert(d._sid_to_slot("sid-overflow") == -1, "未占槽的 sid 查询应返 -1")

    # 现有 5 个 sid 的 slot 不能被覆盖
    occupied = [s for s in d._slot_assignments if s is not None]
    _assert(len(occupied) == 5, f"溢出请求不应改 slot table：{d._slot_assignments}")
    print("  ok  6th session returns slot=-1, existing slots untouched")


# ── Test 2: release allows new sid to claim slot ──
async def test_release_allows_reuse():
    _reset()
    await d._handle_envelope(_env_prompt("sid-A", "/projA"))
    await d._handle_envelope(_env_prompt("sid-B", "/projB"))
    _assert(d._sid_to_slot("sid-A") == 0, "A 应在 slot 0")
    _assert(d._sid_to_slot("sid-B") == 1, "B 应在 slot 1")

    # 主动释放 A
    d._release_slot("sid-A")
    _assert(d._sid_to_slot("sid-A") == -1, "释放后 A 应查不到")
    _assert(d._slot_assignments[0] is None, "slot 0 应空")

    # 新 sid 进来应抢 slot 0（最低空槽）
    new_slot = d._assign_slot("sid-C")
    _assert(new_slot == 0, f"新 sid 应抢回 slot 0，实际 {new_slot}")
    _assert(d._sid_to_slot("sid-C") == 0, "sid-C 应在 slot 0")
    print("  ok  released slot is reclaimable by new sid (lowest first)")


# ── Test 3: cleanup path releases slot + marks dirty (sibling bug) ──
async def test_cleanup_releases_and_marks_dirty():
    _reset()
    last = None
    # A 起对话 → 停止
    _set(100.0)
    await d._handle_envelope(_env_prompt("sid-A", "/projA"))
    _set(101.0)
    await d._handle_envelope(_env_stop("sid-A", "/projA"))
    last = await d._pusher_tick(last)
    _assert(d._sid_to_slot("sid-A") == 0, "A 起步应在 slot 0")

    # 等过 SESSION_CLEANUP_S (10s) + completed_until 过期
    _set(115.0)
    pre_count = len(_sent_wires)
    last = await d._pusher_tick(last)

    # 关键 1：cleanup 后 A 应从 _sessions del，且 slot 释放
    _assert("sid-A" not in d._sessions, "A 应被 cleanup 删")
    _assert(d._sid_to_slot("sid-A") == -1, "A 的 slot 应释放")
    _assert(d._slot_assignments[0] is None, "slot 0 应空")

    # 关键 2 (sibling bug fix)：cleanup 后 _mark_dirty 被调用，下一帧推 wire
    # —— pusher_tick 内部 _dirty 被 cleanup 翻起后会立刻 _to_device_wire 并 _send
    post_count = len(_sent_wires)
    _assert(post_count > pre_count,
            f"cleanup tick 应触发新一帧推送（sibling bug fix），"
            f"pre={pre_count} post={post_count}")
    print("  ok  cleanup releases slot + mark_dirty → device gets cleared wire immediately")


# ── Test 4: retire_stale_waiting also releases slot ──
async def test_retire_stale_waiting_releases_slot():
    _reset()
    # A 开始对话 + 进入 waiting 状态（模拟 permission_prompt）
    await d._handle_envelope(_env_prompt("sid-A", "/proj"))
    sess_a = d._sessions["sid-A"]
    sess_a.waiting = 1  # 直接造 waiting=1，方便测 retire 路径
    sess_a.turn_active = False  # retire 要求 turn 已结束
    _assert(d._sid_to_slot("sid-A") == 0, "A 应在 slot 0")

    # B 在同 cwd 起新 prompt：
    # daemon 顺序：B setdefault → _assign_slot(B)=slot 1（A 还在 slot 0）
    #              → _retire_stale_waiting_sessions(B) 把 A 删并 _release_slot(A)
    # 所以 B 拿到的是新 slot 1，A 的 slot 0 被腾出来给后续 sid 用
    await d._handle_envelope(_env_prompt("sid-B", "/proj"))

    _assert("sid-A" not in d._sessions, "A 应被 retire")
    _assert(d._sid_to_slot("sid-A") == -1, "A 的 slot 应被 release")
    _assert(d._slot_assignments[0] is None, "A 原 slot 0 应空，可被新 sid 抢")
    _assert(d._sid_to_slot("sid-B") == 1, f"B 拿到下一个空 slot 1，实际 {d._sid_to_slot('sid-B')}")

    # 验证 A 的 slot 0 确实能被新 sid C 抢走
    await d._handle_envelope(_env_prompt("sid-C", "/projC"))
    _assert(d._sid_to_slot("sid-C") == 0, f"sid-C 应抢回 slot 0，实际 {d._sid_to_slot('sid-C')}")
    print("  ok  retire_stale_waiting releases slot, freed slot becomes available")


# ── Test 5: wire entry contains slot field ──
async def test_wire_carries_slot_field():
    _reset()
    last = None
    _set(100.0)
    await d._handle_envelope(_env_prompt("sid-X", "/projX"))
    last = await d._pusher_tick(last)
    _assert(_sent_wires, "应有 wire 推送")
    last_wire = _sent_wires[-1]
    _assert("ss" in last_wire, "wire 应有 ss 字段")
    _assert(len(last_wire["ss"]) >= 1, "应至少有一个 session entry")
    entry = last_wire["ss"][0]
    _assert("slot" in entry, f"wire entry 应有 slot 字段，实际 keys={list(entry.keys())}")
    _assert(entry["slot"] == 0, f"sid-X 在 slot 0，wire entry slot 应为 0，实际 {entry['slot']}")
    print(f"  ok  wire entry carries slot field: {entry}")


# ── runner ──
async def main():
    orig_time = d.time
    orig_send = d._send
    d.time = _MockTime()
    d._send = _capture_send

    tests = [
        test_overflow_returns_minus_one,
        test_release_allows_reuse,
        test_cleanup_releases_and_marks_dirty,
        test_retire_stale_waiting_releases_slot,
        test_wire_carries_slot_field,
    ]
    print(f"running {len(tests)} slot assignment tests...")
    try:
        for tt in tests:
            print(f"\n[{tt.__name__}]")
            await tt()
        print(f"\n{'='*50}\n  ALL SLOT ASSIGNMENT TESTS PASSED ({len(tests)} groups)")
        return 0
    finally:
        d.time = orig_time
        d._send = orig_send


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
