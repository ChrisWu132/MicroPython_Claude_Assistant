#!/usr/bin/env python3
"""
virtual_device.py —— PC 端无硬件虚拟设备。

通过 TCP 连接 daemon 的 --tcp-fanout 端口，接收 wire JSON 行，
复刻 device/display_renderer.py 的核心副作用到日志：

  - slot tab name 变化 → history clear（issue #6 的核心 bug 指示器）
  - history 追加 / 去重
  - 状态跳变 → voice 触发
  - dominant state 变化

复用 device/protocol.py + device/state.py 纯 Python 逻辑，不画 UI、不放声音。

用法：
  python scripts/virtual_device.py                       # 连默认 127.0.0.1:57321
  python scripts/virtual_device.py --port 57321 --log-file vd.log
  python scripts/virtual_device.py --slot-mode index     # 阶段 0：按数组下标渲染（v5 wire）
  python scripts/virtual_device.py --slot-mode slot      # 阶段 1：按 wire 的 slot 字段渲染
"""

import argparse
import asyncio
import json
import os
import sys
import time

# Windows PowerShell 默认 GBK；强制 UTF-8 stdout 避免中文 mojibake
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 把 device/ 加到 sys.path（用脚本绝对路径，不依赖 cwd）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEVICE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "device"))
sys.path.insert(0, DEVICE_DIR)

from protocol import parse, MultiSessionMsg  # noqa: E402
from state import (  # noqa: E402
    sess_state as _sess_state,
    S_IDLE, S_WORKING, S_PENDING, S_DONE, S_ERROR,
)

MAX_SESSIONS = 5
HISTORY_MAX_LEN = 20
STATE_LABELS = {S_WORKING: "Working", S_ERROR: "Error",
                S_DONE: "Done", S_IDLE: "Idle", S_PENDING: "Pending"}


def _dominant_state(sessions):
    states = [_sess_state(s) for s in sessions] if sessions else []
    for s in (S_ERROR, S_PENDING, S_WORKING, S_DONE):
        if s in states:
            return s
    return S_IDLE


class _Logger:
    def __init__(self, log_file=None):
        self._fh = open(log_file, "w", encoding="utf-8", buffering=1) if log_file else None
        self._t0 = time.time()

    def log(self, line: str):
        ts = time.time() - self._t0
        stamped = f"[t={ts:7.3f}] {line}"
        print(stamped)
        if self._fh:
            self._fh.write(stamped + "\n")


class VirtualDevice:
    """复刻 device/display_renderer.py 关键副作用到日志。

    阶段 0 (slot_mode='index')：按数组下标渲染——和当前 device 端 v5 wire 行为一致，
    expected 会复现 issue #6 的 history 误清。
    阶段 1 (slot_mode='slot')：按 wire entry 的 slot 字段渲染——B 方案修复后行为，
    expected 不再发生 swap-clear。
    """

    def __init__(self, logger: _Logger, slot_mode: str = "index"):
        self._log = logger
        self._slot_mode = slot_mode
        self._slot_names = [""] * MAX_SESSIONS
        self._histories = [[] for _ in range(MAX_SESSIONS)]
        self._prev_states = {}            # sess.name → 上一帧 state（语音触发判定，照搬 display_renderer）
        self._dominant = S_IDLE
        self._swap_clear_count = 0         # 跨 session 的 history clear 次数（bug 指示器）
        self._normal_clear_count = 0       # 首次入槽的正常 clear 次数

    # ── slot 派发 ────────────────────────────────────────────
    def _resolve_slots(self, sessions):
        """返回 list[Optional[SessionStatus]] 长度 MAX_SESSIONS。"""
        out = [None] * MAX_SESSIONS
        if self._slot_mode == "slot":
            for sess in sessions:
                slot = getattr(sess, "slot", -1)
                if 0 <= slot < MAX_SESSIONS:
                    out[slot] = sess
        else:  # index 模式
            for i, sess in enumerate(sessions[:MAX_SESSIONS]):
                out[i] = sess
        return out

    # ── 副作用：tab 渲染 + history clear 检测 ─────────────────
    def _update_tab(self, index: int, sess):
        if sess is None:
            self._slot_names[index] = ""
            # 空槽不清 history（照搬 display_renderer.py:573 的行为）
            return
        if sess.name != self._slot_names[index]:
            old = self._slot_names[index]
            cleared_items = len(self._histories[index])
            self._histories[index] = []
            if old and old != sess.name:
                self._swap_clear_count += 1
                self._log.log(
                    f"[history-clear *SWAP*] slot={index} old={old!r} → "
                    f"new={sess.name!r} cleared {cleared_items} items "
                    f"(total_swap_clears={self._swap_clear_count})"
                )
            else:
                self._normal_clear_count += 1
                self._log.log(
                    f"[history-clear normal] slot={index} new={sess.name!r} "
                    f"(first-time-in-slot, total_normal={self._normal_clear_count})"
                )
            self._slot_names[index] = sess.name

        state = _sess_state(sess)
        self._log.log(f"[tab] slot={index} name={sess.name!r} state={state}")

    # ── 副作用：history 追加（照搬 _update_history） ──────────
    def _update_history(self, index: int, sess):
        state = _sess_state(sess)
        history = self._histories[index]
        text = sess.msg if sess.msg else STATE_LABELS.get(state, "?")
        record = {"msg": text, "state": state}

        if history and history[-1]["msg"] == text and history[-1]["state"] == state:
            return  # dedup
        if history and history[-1]["msg"] == text:
            history[-1]["state"] = state
            return
        history.append(record)
        if len(history) > HISTORY_MAX_LEN:
            history.pop(0)

    # ── 副作用：语音触发（照搬 display_renderer.py:489-497） ──
    def _check_voice(self, sessions):
        for sess in sessions:
            cur = _sess_state(sess)
            prev = self._prev_states.get(sess.name)
            if cur != prev:
                if cur in (S_DONE, S_ERROR, S_PENDING):
                    self._log.log(f"[voice] sess={sess.name!r} prev={prev} cur={cur}")
                self._prev_states[sess.name] = cur

    # ── 副作用：dominant state ───────────────────────────────
    def _update_main(self, sessions):
        new_dom = _dominant_state(sessions)
        if new_dom != self._dominant:
            self._log.log(f"[dominant] {self._dominant} → {new_dom}")
            self._dominant = new_dom

    # ── 主入口 ───────────────────────────────────────────────
    def render(self, msg: MultiSessionMsg):
        sessions = msg.sessions[:MAX_SESSIONS]
        wire_summary = [
            (s.name, _sess_state(s), getattr(s, "slot", None))
            for s in sessions
        ]
        self._log.log(f"WIRE sessions={wire_summary} slot_mode={self._slot_mode}")

        slot_table = self._resolve_slots(sessions)
        for i, sess in enumerate(slot_table):
            self._update_tab(i, sess)
            if sess is not None:
                self._update_history(i, sess)

        self._check_voice(sessions)
        self._update_main(sessions)

        self._log.log(
            f"slot_names={self._slot_names}  "
            f"history_lens={[len(h) for h in self._histories]}"
        )

    def stats(self):
        return {
            "swap_clears": self._swap_clear_count,
            "normal_clears": self._normal_clear_count,
        }


async def run(host: str, port: int, slot_mode: str, log_file: str):
    logger = _Logger(log_file)
    dev = VirtualDevice(logger, slot_mode=slot_mode)
    logger.log(f"virtual_device starting: host={host} port={port} slot_mode={slot_mode}")

    while True:
        try:
            logger.log(f"connecting to {host}:{port}...")
            reader, writer = await asyncio.open_connection(host, port)
            logger.log("connected")
        except OSError as e:
            logger.log(f"connect failed: {e}; retry in 2s")
            await asyncio.sleep(2)
            continue

        try:
            while True:
                line = await reader.readline()
                if not line:
                    logger.log("server closed connection")
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    text = line.decode("utf-8", errors="replace")
                except Exception:
                    text = repr(line)
                msg = parse(text)
                if msg is None:
                    logger.log(f"PARSE FAIL: {text!r}")
                    continue
                if isinstance(msg, dict):
                    logger.log(f"CMD: {msg}")
                    continue
                if isinstance(msg, MultiSessionMsg):
                    dev.render(msg)
        except (ConnectionResetError, asyncio.IncompleteReadError) as e:
            logger.log(f"read error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        stats = dev.stats()
        logger.log(f"stats: {stats}")
        await asyncio.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=57321,
                        help="daemon TCP fanout 端口（与 ble_daemon --tcp-port 一致，默认 57321）")
    parser.add_argument("--slot-mode", choices=["index", "slot"], default="index",
                        help="index = 按数组下标（阶段 0 / v5 wire 兼容），"
                             "slot = 按 wire entry 的 slot 字段（阶段 1 / B 方案修复后）")
    parser.add_argument("--log-file", default=None, help="日志文件路径")
    args = parser.parse_args()

    try:
        asyncio.run(run(args.host, args.port, args.slot_mode, args.log_file))
    except KeyboardInterrupt:
        print("\n[virtual_device] interrupted")


if __name__ == "__main__":
    main()
