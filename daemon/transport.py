# transport.py —— PC 端通信传输抽象层
#
# Transport 基类定义统一接口，具体实现类负责各自传输细节。
# 当前实现：BleTransport（BLE NUS，基于 bleak）
# 预留接口：WifiTransport（TCP）、UartTransport（USB 串口）

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional


class Transport:
    async def start(
        self,
        on_recv: Callable[[dict], None],
        on_connect: Callable[[], None],
        on_disconnect: Callable[[], None],
    ): raise NotImplementedError

    async def send(self, payload: dict): raise NotImplementedError
    def connected(self) -> bool: raise NotImplementedError
    def device_online(self) -> bool: raise NotImplementedError


# ── BLE 实现 ─────────────────────────────────────────────────

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


def _get_config_path() -> Path:
    """获取配对配置文件路径（跨平台）。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", "~"))
    else:
        base = Path.home() / ".config"
    return base / "claude-buddy" / "device.json"


def _load_pairing_config() -> dict:
    """加载配对配置，返回 {"device_name": str, "paired_mac": str, ...}。"""
    path = _get_config_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


class BleTransport(Transport):
    def __init__(self):
        self._client       = None
        self._connected    = False
        self._rx_buf       = ""
        self._send_lock    = None   # 初始化在 start()（需要 event loop）

        self._on_recv       = None
        self._on_connect    = None
        self._on_disconnect = None

        # v2.2 §A-3: 读取配对配置——优先 paired_mac，fallback device_name
        # pair_device.py 一直在写 paired_mac，但旧版 transport 只读 device_name，
        # 这就是 codex review 抓的 bug：同名设备会随机抽一台。
        config = _load_pairing_config()
        self._paired_mac = config.get("paired_mac")
        self._target_device_name = config.get("device_name")
        if self._paired_mac or self._target_device_name:
            print(
                f"[daemon] 已加载配对: mac={self._paired_mac} "
                f"name={self._target_device_name}"
            )
        else:
            print("[daemon] 未配对设备，将连接任意 Claude-Buddy-* 设备")

    # ── 公开接口 ────────────────────────────────────────────

    async def start(self, on_recv, on_connect, on_disconnect):
        self._on_recv       = on_recv
        self._on_connect    = on_connect
        self._on_disconnect = on_disconnect
        self._send_lock     = asyncio.Lock()
        await self._connect_loop()

    async def send(self, payload: dict):
        data = (json.dumps(payload) + "\n").encode()
        print(f"[send] t={time.time():.3f} {payload} ({len(data)}B)")
        async with self._send_lock:
            for i in range(0, len(data), 20):
                await self._client.write_gatt_char(NUS_RX, data[i:i+20], response=False)

    def connected(self) -> bool:
        return self._connected

    # ── 内部 BLE 回调 ────────────────────────────────────────

    def _on_ble_disconnect(self, client):
        self._connected = False
        if self._on_disconnect:
            self._on_disconnect()

    def _on_ble_notify(self, sender, data: bytearray):
        self._rx_buf += data.decode(errors="ignore")
        while "\n" in self._rx_buf:
            line, self._rx_buf = self._rx_buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if self._on_recv:
                    self._on_recv(msg)
            except Exception:
                pass

    # ── 连接循环 ─────────────────────────────────────────────

    async def _connect_loop(self):
        from bleak import BleakClient, BleakScanner
        while True:
            if self._connected:
                await asyncio.sleep(1)
                continue
            try:
                devices = await BleakScanner.discover(timeout=5.0)

                # v2.2 §A-3: 三段匹配
                #   1. paired_mac 精确匹配 (V1 防多设备同名误连)
                #   2. device_name 兜底 (macOS bleak 返回 UUID 而非 MAC，
                #      或老 device.json 没 paired_mac 字段)
                #   3. 都没配过 → Claude-Buddy-* 前缀首个
                addr = None
                if self._paired_mac:
                    target_mac = self._paired_mac.upper()
                    addr = next(
                        (d.address for d in devices
                         if d.address and d.address.upper() == target_mac),
                        None,
                    )

                if not addr and self._target_device_name:
                    addr = next(
                        (d.address for d in devices if d.name == self._target_device_name),
                        None,
                    )

                if not addr and not self._paired_mac and not self._target_device_name:
                    addr = next(
                        (d.address for d in devices
                         if d.name and d.name.startswith("Claude-Buddy-")),
                        None,
                    )

                if not addr:
                    if self._paired_mac or self._target_device_name:
                        print(
                            f"[daemon] 配对设备未找到 "
                            f"(mac={self._paired_mac} name={self._target_device_name})"
                            f"，重试中..."
                        )
                    else:
                        print("[daemon] 未找到任何 Claude-Buddy 设备，重试中...")
                    await asyncio.sleep(3)
                    continue

                self._client = BleakClient(addr, disconnected_callback=self._on_ble_disconnect)
                await self._client.connect()
                await self._client.start_notify(NUS_TX, self._on_ble_notify)
                self._connected     = True
                print(f"[daemon] connected to {addr}")
                if self._on_connect:
                    self._on_connect()
                await asyncio.sleep(1.0)
            except Exception as e:
                print(f"[daemon] connect failed: {e}")
                self._client        = None
                self._connected     = False
                await asyncio.sleep(3)

    # ── 连接循环结束 ─────────────────────────────────────────


# ── TCP fanout 实现 ───────────────────────────────────────────

class TcpFanoutTransport(Transport):
    """TCP fanout：listen 127.0.0.1:port，accept 多 client，send() 把
    ``json.dumps(payload)+"\n"`` 广播给所有已连接 client。

    用途：PC 端跑多个虚拟设备脚本接 wire JSON 做无硬件长测，与 BleTransport
    并存。``connected()`` 在无 client 时返 False，``send()`` 在无 client 时
    无操作不抛——确保不污染 BLE 的重连补发 dedup 语义。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 57321):
        self._host = host
        self._port = port
        self._clients: set = set()
        self._server = None
        self._on_connect = None
        self._on_disconnect = None

    async def start(self, on_recv, on_connect, on_disconnect):
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        print(f"[tcp-fanout] listening on {self._host}:{self._port}")
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        self._clients.add(writer)
        print(f"[tcp-fanout] client connected: {peer} (total={len(self._clients)})")
        if self._on_connect:
            self._on_connect()
        try:
            await reader.read()
        finally:
            self._clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            print(f"[tcp-fanout] client disconnected: {peer} (total={len(self._clients)})")
            if self._on_disconnect:
                self._on_disconnect()

    async def send(self, payload: dict):
        if not self._clients:
            return
        data = (json.dumps(payload) + "\n").encode()
        dead = []
        for w in list(self._clients):
            try:
                w.write(data)
                await w.drain()
            except Exception as e:
                dead.append(w)
                print(f"[tcp-fanout] write failed, dropping: {e}")
        for w in dead:
            self._clients.discard(w)

    def connected(self) -> bool:
        return bool(self._clients)

    def device_online(self) -> bool:
        return self.connected()


# ── WiFi 实现（预留：设备端 WiFi 连接，区别于 TcpFanoutTransport 的 fanout 用途） ─

class WifiTransport(Transport):
    """TCP socket 传输（未实现）。"""
    async def start(self, on_recv, on_connect, on_disconnect): raise NotImplementedError
    async def send(self, payload: dict): raise NotImplementedError
    def connected(self) -> bool: raise NotImplementedError


# ── 串口实现（预留） ─────────────────────────────────────────

class UartTransport(Transport):
    """USB-UART 串口传输（未实现）。"""
    async def start(self, on_recv, on_connect, on_disconnect): raise NotImplementedError
    async def send(self, payload: dict): raise NotImplementedError
    def connected(self) -> bool: raise NotImplementedError
