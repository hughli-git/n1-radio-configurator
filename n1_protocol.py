"""N1 wire protocol
"""
from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, field

SERVICE = "0000febe-0000-1000-8000-00805f9b34fb"
COMMAND = "0000bec7-0000-1000-8000-00805f9b34fb"
NOTIFICATIONS = ("0000bec8-0000-1000-8000-00805f9b34fb",
                 "0000feab-0000-1000-8000-00805f9b34fb")
LOG = logging.getLogger("n1.protocol")


def crc16(data: bytes) -> int:
    # Uint8ListExtension.crc16CCITT @0x5bf064: reflected table,
    # initial 0xffff, no final xor. BasePacket appends big-endian CRC.
    crc = 0xffff
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
    return crc


def packet(command: int, seq: int, payload: bytes) -> bytes:
    size = 10 + len(payload)
    if not 10 <= size <= 65535 or not 0 <= seq <= 65535:
        raise ValueError("Invalid N1 packet length/sequence")
    raw = struct.pack(">2sHHH", b"\xfe\x01", size, command, seq) + payload
    return raw + crc16(raw).to_bytes(2, "big")


def varint(value: int) -> bytes:
    if not isinstance(value, int) or not 0 <= value <= 0xffffffffffffffff:
        raise ValueError("Protobuf integer outside uint64 range")
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def uint(tag: int, value: int) -> bytes:
    return varint(tag << 3) + varint(value)


def blob(tag: int, value: bytes) -> bytes:
    return varint((tag << 3) | 2) + varint(len(value)) + value


def read_varint(data: bytes, offset: int):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated protobuf varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("Protobuf varint overflow")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Protobuf varint overflow")


def wire_fields(data: bytes):
    """Return (tag, wire type, value, original bytes), preserving unknown fields."""
    offset = 0
    result = []
    while offset < len(data):
        start = offset
        tag, offset = read_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("Invalid protobuf tag 0")
        if wire == 0:
            value, offset = read_varint(data, offset)
        elif wire in (1, 2, 5):
            size = 8 if wire == 1 else 4
            if wire == 2:
                size, offset = read_varint(data, offset)
            if offset + size > len(data):
                raise ValueError("Truncated protobuf field")
            value = data[offset:offset + size]
            offset += size
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire}")
        result.append((number, wire, value, data[start:offset]))
    return result


def fields(data: bytes):
    return {tag: value for tag, _, value, _ in wire_fields(data)}


class FrameDecoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, raw: bytes):
        self.buffer.extend(raw)
        packets = []
        while len(self.buffer) >= 4:
            if self.buffer[:2] != b"\xfe\x01":
                del self.buffer[0]
                continue
            size = int.from_bytes(self.buffer[2:4], "big")
            if not 10 <= size <= 4096:
                LOG.warning("Invalid frame size %d; resynchronizing", size)
                del self.buffer[0]
                continue
            if len(self.buffer) < size:
                break
            frame = bytes(self.buffer[:size])
            if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "big"):
                LOG.warning("CRC rejected: %s", frame.hex(" "))
                del self.buffer[0]
                continue
            del self.buffer[:size]
            packets.append(frame)
        return packets


@dataclass
class Channel:
    seq: int = 1
    kind: int = 2
    name: str = "新频道"
    rx: int = 430000000
    tx: int = 430000000
    rx_tone: int = 0
    tx_tone: int = 0
    raw: str = field(default="", repr=False)

    @property
    def key(self):
        return self.kind, self.seq

    @property
    def display_name(self):
        return self.name or f"{'预设' if self.kind == 1 else '自定义'}频道 {self.seq:02d}"

    @classmethod
    def from_wire(cls, raw: bytes):
        f = fields(raw)
        name = f.get(6, b"").decode("gbk")
        return cls(f.get(1, 0), f.get(2, 0), name, f.get(4, 0),
                   f.get(5, 0), f.get(9, 0), f.get(10, 0), raw.hex())

    @classmethod
    def from_apk(cls, data):
        return cls(int(data["channelSeq"]), int(data["channelType"]),
                   str(data.get("channelName", "")), int(data["receiveFreq"]),
                   int(data["sendFreq"]), int(data.get("receiveToneValue", 0)),
                   int(data.get("sendToneValue", 0)), data.get("protocolRaw", ""))

    def to_apk(self):
        return dict(channelSeq=self.seq, channelType=self.kind, channelName=self.name,
                    receiveFreq=self.rx, sendFreq=self.tx, receiveToneValue=self.rx_tone,
                    sendToneValue=self.tx_tone, protocolRaw=self.raw)

    def encode(self):
        values = {1: uint(1, self.seq), 2: uint(2, self.kind),
                  4: uint(4, self.rx), 5: uint(5, self.tx),
                  6: blob(6, self.name.encode("gbk")),
                  9: uint(9, self.rx_tone), 10: uint(10, self.tx_tone)}
        retained = {}
        if self.raw:
            for tag, _, _, raw in wire_fields(bytes.fromhex(self.raw)):
                retained.setdefault(tag, []).append(raw)
        # Keep power, squelch, VOX, bandwidth, and any unknown fields intact.
        return b"".join(values[tag] if tag in values else b"".join(retained[tag])
                        for tag in sorted(set(values) | set(retained)))


def parse_response(raw: bytes):
    if len(raw) < 10 or raw[:2] != b"\xfe\x01" or int.from_bytes(raw[2:4], "big") != len(raw):
        raise ValueError("Invalid frame")
    if crc16(raw[:-2]) != int.from_bytes(raw[-2:], "big"):
        raise ValueError("Invalid CRC")
    command, seq = struct.unpack(">HH", raw[4:8])
    f = fields(raw[8:-2])
    ch = None
    # Only N1 ChannelInfoOperate/ChannelStateOperate have ChannelInfo at tag 4.
    if command in (0x4e28, 0x4e2b) and isinstance(f.get(4), bytes) and f[4]:
        ch = Channel.from_wire(f[4])
    return {"command": command, "seq": seq, "fields": f,
            "result": f.get(2, 0), "option": f.get(3, 0), "channel": ch}


def validate_preset_change(channel, previous):
    """N1 preset writes silently ignore fixed fields; only CSS is writable."""
    if channel.kind != 1:
        return
    if previous is None:
        raise ValueError("设备不支持新增预设频道，只能新增自定义频道")
    if channel.key != previous.key or (channel.name, channel.rx, channel.tx) != (previous.name, previous.rx, previous.tx):
        raise ValueError("预设频道的序号、类型、名称和收发频率不可修改；仅可修改收发亚音")


def validate_channel_changes(desired, original):
    before = {c.key: c for c in original}
    after = {c.key: c for c in desired}
    if any(key not in after for key in before if key[0] == 1):
        raise ValueError("设备不支持删除预设频道")
    for channel in desired:
        validate_preset_change(channel, before.get(channel.key))


def channel_matches(desired, actual):
    """Verify supplied fields including hidden settings moved during reordering."""
    expected = fields(desired.encode())
    received = fields(actual.encode())
    return all(received.get(tag, b"" if isinstance(value, bytes) else 0) == value
               for tag, value in expected.items())


class N1Session:
    def __init__(self, client, on_state=None, emit=lambda s: None, timeout=3):
        self.client, self.on_state, self.emit = client, on_state, emit
        self.timeout = timeout
        self.seq = 0
        self.pending = {}
        self.decoders = {}
        self.subscribed = []
        self.operation_lock = asyncio.Lock()
        self.initialized = False
        self.current = None
        self.device_info = {}
        self.on_headset = None

    async def start(self):
        for uuid in NOTIFICATIONS:
            decoder = FrameDecoder()
            self.decoders[uuid] = decoder
            def cb(sender, raw, uuid=uuid, decoder=decoder):
                LOG.info("RX %s %s", uuid, bytes(raw).hex(" "))
                for p in decoder.feed(bytes(raw)):
                    try:
                        result = parse_response(p)
                        if result["command"] == 0x4e34 and result["result"] == 0 and self.on_headset:
                            for tag, wire, value, _ in wire_fields(p[8:-2]):
                                if tag == 3 and wire == 2:
                                    self.on_headset(fields(value))
                        LOG.info("RESPONSE command=%04x seq=%d result=%d option=%d channel=%s",
                                 result["command"], result["seq"], result["result"],
                                 result["option"], result["channel"])
                        key = result["seq"], result["command"]
                        future = self.pending.get(key)
                        if future is not None and not future.done():
                            future.set_result(result)
                        if result["command"] == 0x4e28 and result["result"] == 0 and result["channel"]:
                            self.current = result["channel"].key
                            if self.on_state:
                                self.on_state(self.current)
                    except Exception:
                        LOG.exception("Malformed response")
            try:
                await self.client.start_notify(uuid, cb)
                self.subscribed.append(uuid)
            except Exception:
                LOG.exception("Subscribe failed: %s", uuid)
        if not self.subscribed:
            raise RuntimeError("无法订阅 N1 响应通知")

    def disconnected(self):
        for f in list(self.pending.values()):
            if not f.done():
                f.set_exception(ConnectionError("蓝牙连接已断开"))
        self.initialized = False
        self.current = None

    async def close(self):
        self.disconnected()
        for uuid in self.subscribed:
            try:
                await self.client.stop_notify(uuid)
            except Exception:
                LOG.debug("Notification already stopped", exc_info=True)
        self.subscribed.clear()

    async def request(self, command, payload, response):
        if not self.client.is_connected:
            raise ConnectionError("尚未连接设备")
        seq = self.seq
        self.seq = (seq + 1) & 0xffff
        future = asyncio.get_running_loop().create_future()
        key = seq, response
        self.pending[key] = future
        raw = packet(command, seq, payload)
        LOG.info("TX command=%04x seq=%d hex=%s", command, seq, raw.hex(" "))
        try:
            await self.client.write_gatt_char(COMMAND, raw, response=True)
            return await asyncio.wait_for(future, self.timeout)
        except asyncio.TimeoutError as error:
            raise TimeoutError(f"设备响应超时：命令 {command:04x}，序号 {seq}；未跳过频道") from error
        finally:
            self.pending.pop(key, None)
            if not future.done():
                future.cancel()

    async def initialize(self):
        if self.initialized:
            return
        # Connection/query payloads verified against the original capture. Sequence/CRC are generated.
        for cmd, body, response in [
            (0x4e21, "080110eb82c8c902", 0x4e22),
            (0x4e23, "08011801", 0x4e25),
            (0x4e26, "08011801", 0x4e28),
            (0x4e23, "08011801", 0x4e25),
            (0x4e2f, "08011800", 0x4e31),
        ]:
            reply = await self.request(cmd, bytes.fromhex(body), response)
            if cmd == 0x4e21 and reply["result"] != 0:
                raise RuntimeError(f"N1 握手失败：{reply['result']}")
        self.initialized = True

    async def read_channels(self, progress=None, kinds=(1, 2)):
        async with self.operation_lock:
            await self.initialize()
            return await self._read_channels(progress, kinds)

    async def _read_channels(self, progress=None, kinds=(1, 2)):
        result = {}
        # A308/N1 CH_EMPTY terminates a list; CH_QUERY_FINISH is also defined by APK.
        for kind in kinds:
            body = uint(1, 1) + uint(3, 3) + blob(4, uint(2, kind))
            for count in range(1024):
                reply = await self.request(0x4e29, body, 0x4e2b)
                if reply["option"] != 3:
                    raise RuntimeError("频道读取响应的操作码不匹配")
                if reply["result"] in (2, 5):
                    self.emit(f"{'预设' if kind == 1 else '自定义'}频道读取结束：{count} 个")
                    break
                if reply["result"] != 4 or reply["channel"] is None:
                    raise RuntimeError(f"频道查询被拒绝：result={reply['result']}")
                channel = reply["channel"]
                if channel.kind != kind or channel.seq <= 0 or channel.key in result:
                    raise RuntimeError("设备返回重复频道或错误的频道类型，读取未完成")
                result[channel.key] = channel
                if progress:
                    progress(channel)
                body = uint(1, 1) + uint(2, 0) + uint(3, 3)
            else:
                raise RuntimeError("设备未返回频道列表结束标志")
        return sorted(result.values(), key=lambda c: c.key)

    async def read_current(self):
        async with self.operation_lock:
            await self.initialize()
            return await self._read_current()

    async def read_device_info(self):
        async with self.operation_lock:
            await self.initialize()
            return await self._read_device_info()

    async def _read_device_info(self):
        # A308BleService.queryDevInfo @9edc40; DeviceParam fullInquiry=ON.
        reply = await self.request(0x4e23, uint(1, 1) + uint(3, 1), 0x4e25)
        if reply["result"] != 1:  # PARAM_QUERY_OK (different from CH_OK!)
            raise RuntimeError(f"设备信息查询失败：{reply['result']}")
        self.device_info = reply["fields"]
        return self.device_info

    async def update_device_info(self, changes, channel_changes=None):
        """Sparse DeviceParam update; never resend read-only or unknown fields."""
        async with self.operation_lock:
            await self.initialize()
            before = await self._read_device_info()
            allowed = {7, 10, 12, 13, 14, 15}
            if not set(changes) <= allowed:
                raise ValueError("不支持的设备设置字段")
            channel_changes = channel_changes or {}
            if not set(channel_changes) <= {7, 8, 12, 13}:
                raise ValueError("不支持的全局频道设置字段")
            parts = [uint(1, 1)]
            for tag, value in sorted(changes.items()):
                if tag == 7:
                    if not isinstance(value, bytes) or not value:
                        raise ValueError("蓝牙名称不能为空")
                    parts.append(blob(tag, value))
                else:
                    options = {10: {0, 1}, 12: {0, 1}, 13: {1, 2, 3}, 14: {0, 1}, 15: {0, 1}}
                    if not isinstance(value, int) or value not in options[tag]:
                        raise ValueError("设置值无效")
                    parts.append(uint(tag, value))
            if channel_changes:
                if any(not isinstance(v, int) or v not in (0, 1) for v in channel_changes.values()):
                    raise ValueError("N1 全局频道设置须使用 APK 的开关 / 两档选项")
                parts.append(blob(9, b"".join(uint(t, v) for t, v in sorted(channel_changes.items()))))
            if len(parts) == 1:
                return before
            reply = await self.request(0x4e24, b"".join(parts), 0x4e25)
            if reply["result"] != 3:  # PARAM_UPDATE_OK
                raise RuntimeError(f"设备设置写入被拒绝：{reply['result']}")
            actual = await self._read_device_info()
            for tag, value in changes.items():
                if actual.get(tag, b"" if tag == 7 else 0) != value:
                    raise RuntimeError(f"设备设置字段 {tag} 读回不一致，请刷新设备信息")
            ch = fields(actual.get(9, b""))
            for tag, value in channel_changes.items():
                if ch.get(tag, 0) != value:
                    raise RuntimeError(f"全局频道设置字段 {tag} 读回不一致")
            return actual

    async def headset_state(self, option=0, headset=None):
        async with self.operation_lock:
            await self.initialize()
            if option not in (0, 1, 2):
                raise ValueError("无效的耳机操作")
            body = uint(1, 1) + uint(3, option)
            if option == 1:
                if not headset:
                    raise ValueError("请选择耳机")
                body += blob(4, b"".join(blob(t, headset[t]) if t in (1, 2) else uint(t, headset[t])
                                         for t in (1, 2, 3, 4) if t in headset))
            reply = await self.request(0x4e2f if option == 0 else 0x4e30, body, 0x4e31)
            if reply["result"] != 0:
                raise RuntimeError(f"耳机操作失败：{reply['result']}")
            if option:
                for attempt in range(5):
                    await asyncio.sleep(0.5)
                    status = await self.request(0x4e2f, uint(1, 1) + uint(3, 0), 0x4e31)
                    if status["result"] != 0:
                        raise RuntimeError("耳机状态读回失败")
                    actual = fields(status["fields"].get(4, b""))
                    if (option == 1 and actual.get(1) == headset.get(1)) or (option == 2 and not actual.get(1)):
                        return status["fields"]
                raise RuntimeError("耳机状态读回与预期不一致，请刷新状态")
            return reply["fields"]

    async def headset_scan(self, option):
        async with self.operation_lock:
            await self.initialize()
            if option not in (0, 1, 2):
                raise ValueError("无效扫描操作")
            reply = await self.request(0x4e32, uint(1, 1) + uint(3, option), 0x4e33)
            if reply["result"] != 0:
                raise RuntimeError(f"耳机扫描操作失败：{reply['result']}")
            return reply["fields"]

    async def broadcast_config(self, start):
        async with self.operation_lock:
            await self.initialize()
            command = 0x4e38 if start else 0x4e3a
            reply = await self.request(command, uint(1, 1), command + 1)
            if reply["result"] != 1:
                raise RuntimeError(f"批量配置{'启动' if start else '停止'}失败：{reply['result']}")
            return start

    async def _read_current(self):
        reply = await self.request(0x4e26, uint(1, 1) + uint(3, 1), 0x4e28)
        if reply["result"] != 0 or reply["channel"] is None:
            raise RuntimeError(f"当前频道读取失败：{reply['result']}")
        self.current = reply["channel"].key
        return self.current

    async def switch(self, channel):
        async with self.operation_lock:
            await self.initialize()
            return await self._switch(channel.key)

    async def _switch(self, key):
        kind, seq = key
        body = uint(1, 1) + uint(3, 0) + blob(4, uint(1, seq) + uint(2, kind))
        reply = await self.request(0x4e27, body, 0x4e28)
        if reply["result"] != 0:
            raise RuntimeError(f"切换被设备拒绝：{reply['result']}")
        actual = await self._read_current()
        if actual != key:
            raise RuntimeError(f"切换核验失败：期望 {key}，设备当前 {actual}")
        return actual

    async def sync(self, desired, original, current_mapping=None, kinds=(1, 2)):
        """Apply a reviewed diff and read back. Never retry mutating commands blindly."""
        async with self.operation_lock:
            if not kinds or not set(kinds) <= {1,2} or any(c.kind not in kinds for c in desired + original):
                raise ValueError("频道不在本次保存范围内")
            validate_channel_changes(desired, original)
            await self.initialize()
            target_current = None
            if current_mapping:
                current = await self._read_current()
                target_current = current_mapping.get(current, current)
            before = {c.key: c for c in original}
            after = {c.key: c for c in desired}
            operations = []
            for key in sorted(before.keys() - after.keys()):
                operations.append((1, before[key]))
            for key, ch in sorted(after.items()):
                if key not in before:
                    operations.append((0, ch))
                elif ch.encode() != before[key].encode():
                    operations.append((2, ch))
            for option, ch in operations:
                self.emit(f"{'新增' if option == 0 else '删除' if option == 1 else '修改'}频道 {ch.key}")
                body = uint(1, 1) + uint(3, option) + blob(4, ch.encode())
                reply = await self.request(0x4e2a, body, 0x4e2b)
                if reply["result"] != 0 or reply["option"] != option:
                    raise RuntimeError(f"频道 {ch.key} 写入失败：{reply['result']}；请重新读取确认设备")
            actual = await self._read_channels() if tuple(kinds) == (1,2) else await self._read_channels(kinds=kinds)
            received = {c.key: c for c in actual}
            if set(received) != set(after):
                raise RuntimeError("写入后的频道集合核验失败，请重新读取")
            for key, ch in after.items():
                a = received[key]
                if not channel_matches(ch, a):
                    raise RuntimeError(f"频道 {key} 写入后读回不一致，请重新读取")
            await self._read_current()
            if target_current and self.current != target_current:
                await self._switch(target_current)
            return actual

    async def delete_channel(self, channel):
        if channel.kind != 2:
            raise ValueError("设备不支持删除预设频道")
        async with self.operation_lock:
            await self.initialize()
            body = uint(1, 1) + uint(3, 1) + blob(4, uint(1, channel.seq) + uint(2, channel.kind))
            reply = await self.request(0x4e2a, body, 0x4e2b)
            if reply["result"] != 0 or reply["option"] != 1:
                raise RuntimeError(f"频道 {channel.key} 删除失败：{reply['result']}")
            channels = await self._read_channels(kinds=(2,))
            if any(c.key == channel.key for c in channels):
                raise RuntimeError("删除后频道仍在设备中，请重新读取")
            await self._read_current()
            return channels

    async def read_channel(self, kind, seq):
        async with self.operation_lock:
            await self.initialize()
            return await self._read_channel(kind, seq)

    async def _read_channel(self, kind, seq):
        body = uint(1, 1) + uint(3, 3) + blob(4, uint(2, kind))
        seen = set()
        for _ in range(1024):
            reply = await self.request(0x4e29, body, 0x4e2b)
            channel = reply["channel"]
            if reply["option"] != 3 or reply["result"] not in (0, 4) or channel is None:
                raise RuntimeError(f"频道 {(kind, seq)} 读回失败：{reply['result']}")
            if channel.kind != kind or channel.key in seen:
                raise RuntimeError("频道读回类型错误或重复")
            if channel.key == (kind, seq):
                return channel
            seen.add(channel.key)
            body = uint(1, 1) + uint(2, 0) + uint(3, 3)
        raise RuntimeError("频道读回未结束")

    async def save_channel(self, channel, existing):
        async with self.operation_lock:
            if channel.kind == 1 and not existing:
                validate_preset_change(channel, None)
            await self.initialize()
            if channel.kind == 1:
                validate_preset_change(channel, await self._read_channel(*channel.key))
            option = 2 if existing else 0
            body = uint(1, 1) + uint(3, option) + blob(4, channel.encode())
            reply = await self.request(0x4e2a, body, 0x4e2b)
            if reply["result"] != 0 or reply["option"] != option:
                raise RuntimeError(f"频道 {channel.key} 保存失败：{reply['result']}；请重新读取设备")
            actual = await self._read_channel(*channel.key)
            if (channel.name, channel.rx, channel.tx, channel.rx_tone, channel.tx_tone) != (
                    actual.name, actual.rx, actual.tx, actual.rx_tone, actual.tx_tone):
                raise RuntimeError(f"频道 {channel.key} 保存后读回不一致，请重新读取设备")
            return actual
