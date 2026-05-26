#!/usr/bin/env python3
# tests/test_tcp_fanout.py
#
# 测试 daemon.transport.TcpFanoutTransport：
#   - 无 client 时 connected() == False 且 send() 不抛
#   - 单 client：wire JSON 字节级一致到达
#   - 多 client：广播给所有已连接 client
#   - client 断开后 connected() 重新归 False

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "daemon"))
from transport import TcpFanoutTransport  # noqa: E402


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _read_one_line(reader: asyncio.StreamReader, timeout=2.0) -> bytes:
    return await asyncio.wait_for(reader.readline(), timeout=timeout)


async def test_no_client_send_noop():
    """无 client 时 connected()=False, send() 不抛、不发数据。"""
    port = _free_port()
    t = TcpFanoutTransport(port=port)
    server_task = asyncio.create_task(t.start(
        on_recv=lambda m: None,
        on_connect=lambda: None,
        on_disconnect=lambda: None,
    ))
    await asyncio.sleep(0.1)
    assert t.connected() is False, "no client should report connected=False"
    await t.send({"ss": [{"n": "x", "s": "I"}]})
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass
    print("  ok  no-client send is noop")


async def test_single_client_byte_exact():
    """单 client：发送的 wire JSON+\\n 字节级一致到达。"""
    port = _free_port()
    t = TcpFanoutTransport(port=port)
    server_task = asyncio.create_task(t.start(
        on_recv=lambda m: None,
        on_connect=lambda: None,
        on_disconnect=lambda: None,
    ))
    await asyncio.sleep(0.1)

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0.1)
    assert t.connected() is True, "after client connects, connected() should be True"

    payload = {"ss": [{"n": "projA", "s": "W", "m": "Bash: ls"}]}
    await t.send(payload)

    line = await _read_one_line(reader)
    expected = (json.dumps(payload) + "\n").encode()
    assert line == expected, f"received {line!r} != expected {expected!r}"

    writer.close()
    await writer.wait_closed()
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass
    print("  ok  single client byte-exact wire delivery")


async def test_multi_client_broadcast():
    """多 client：同一 wire 广播给所有 client。"""
    port = _free_port()
    t = TcpFanoutTransport(port=port)
    server_task = asyncio.create_task(t.start(
        on_recv=lambda m: None,
        on_connect=lambda: None,
        on_disconnect=lambda: None,
    ))
    await asyncio.sleep(0.1)

    r1, w1 = await asyncio.open_connection("127.0.0.1", port)
    r2, w2 = await asyncio.open_connection("127.0.0.1", port)
    r3, w3 = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0.1)

    payload = {"ss": [{"n": "X", "s": "C"}]}
    await t.send(payload)
    expected = (json.dumps(payload) + "\n").encode()
    for r in (r1, r2, r3):
        line = await _read_one_line(r)
        assert line == expected, f"client received {line!r} != {expected!r}"

    for w in (w1, w2, w3):
        w.close()
        await w.wait_closed()
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass
    print("  ok  multi-client broadcast")


async def test_disconnect_releases_slot():
    """client 断开后 connected() 重新归 False。"""
    port = _free_port()
    t = TcpFanoutTransport(port=port)
    server_task = asyncio.create_task(t.start(
        on_recv=lambda m: None,
        on_connect=lambda: None,
        on_disconnect=lambda: None,
    ))
    await asyncio.sleep(0.1)

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0.1)
    assert t.connected() is True

    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.2)
    assert t.connected() is False, "after client disconnect connected() should be False again"

    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass
    print("  ok  disconnect releases connected state")


async def main():
    tests = [
        test_no_client_send_noop,
        test_single_client_byte_exact,
        test_multi_client_broadcast,
        test_disconnect_releases_slot,
    ]
    print(f"running {len(tests)} TcpFanoutTransport tests...")
    for tt in tests:
        print(f"\n[{tt.__name__}]")
        await tt()
    print(f"\n{'='*50}\n  ALL TCP FANOUT TESTS PASSED ({len(tests)} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
