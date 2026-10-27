import unittest

from n1_protocol import (Channel, FrameDecoder, N1Session, crc16, fields, packet,
                         parse_response, uint, blob, validate_channel_changes)
from dataclasses import replace


class ProtocolTests(unittest.TestCase):
    def test_preset_permissions_protect_import_and_bulk_save(self):
        preset = Channel(seq=1, kind=1, name="", rx=430137500, tx=430137500)
        for change in [dict(name="changed"), dict(rx=preset.rx+12500), dict(tx=preset.tx+12500)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_channel_changes([replace(preset, **change)], [preset])
        with self.assertRaisesRegex(ValueError, "新增预设"):
            validate_channel_changes([preset, replace(preset, seq=21)], [preset])
        with self.assertRaisesRegex(ValueError, "删除预设"):
            validate_channel_changes([], [preset])
        validate_channel_changes([replace(preset, rx_tone=260, tx_tone=515)], [preset])

    def test_capture_crc(self):
        raw = bytes.fromhex("fe0100104e290006080110001803b89e")
        self.assertEqual(crc16(raw[:-2]), 0xb89e)
        self.assertEqual(parse_response(bytes.fromhex("fe0100104e2b0034080110021803772d"))['result'], 2)

    def test_fragmented_decoder(self):
        raw = bytes.fromhex("fe0100104e2b0034080110021803772d")
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(raw[:5]), [])
        self.assertEqual(decoder.feed(raw[5:])[0], raw)

    def test_channel_round_trip_and_unknown_fields(self):
        raw = (uint(1, 1) + uint(2, 2) + uint(3, 1) + uint(4, 430177000) +
               uint(5, 430177000) + b"2\x03abc" + uint(7, 1) + uint(8, 0) +
               uint(9, 260) + uint(10, 260) + uint(11, 1))
        c = Channel.from_wire(raw)
        self.assertEqual((c.seq, c.kind, c.rx_tone), (1, 2, 260))
        encoded = c.encode()
        self.assertEqual(fields(encoded)[7], 1)
        self.assertEqual(fields(encoded)[11], 1)


class DeviceSettingsTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        session = N1Session(None)
        session.initialized = True
        return session

    async def test_sparse_update_and_readback(self):
        session = self.session()
        calls = []
        replies = [dict(result=1, fields={1: 1, 2: 1, 4: 6, 10: 1, 99: b"unknown"}),
                   dict(result=3, fields={2: 3}),
                   dict(result=1, fields={2: 1, 10: 0})]
        async def request(command, body, response):
            calls.append((command, fields(body), response))
            return replies.pop(0)
        session.request = request
        actual = await session.update_device_info({10: 0})
        self.assertEqual(calls[1], (0x4e24, {1: 1, 10: 0}, 0x4e25))
        self.assertEqual(actual[10], 0)

    async def test_reject_channel_success_code_for_device_update(self):
        session = self.session()
        replies = [dict(result=1, fields={10: 1}), dict(result=0, fields={})]
        async def request(*args):
            return replies.pop(0)
        session.request = request
        with self.assertRaisesRegex(RuntimeError, "被拒绝"):
            await session.update_device_info({10: 0})

    async def test_nested_setting_readback_mismatch(self):
        session = self.session()
        replies = [dict(result=1, fields={9: blob(1, b"")}), dict(result=3, fields={}),
                   dict(result=1, fields={9: uint(8, 0)})]
        async def request(*args):
            return replies.pop(0)
        session.request = request
        with self.assertRaisesRegex(RuntimeError, "读回不一致"):
            await session.update_device_info({}, {8: 1})

    async def test_save_single_channel_stops_at_target(self):
        session = self.session()
        target = Channel(seq=3, name="edited", rx_tone=260, tx_tone=768)
        replies = [dict(result=0, option=2),
                   dict(result=4, option=3, channel=Channel(seq=1)),
                   dict(result=4, option=3, channel=Channel(seq=2)),
                   dict(result=4, option=3, channel=target)]
        calls = []
        async def request(command, body, response):
            calls.append((command, fields(body)))
            return replies.pop(0)
        session.request = request
        self.assertEqual(await session.save_channel(target, True), target)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][0], 0x4e2a)
        self.assertEqual(calls[0][1][3], 2)
        self.assertEqual(fields(calls[1][1][4]), {2: 2})

    async def test_single_save_rejected_never_reads_or_retries(self):
        session = self.session()
        calls = []
        async def request(*args):
            calls.append(args)
            return dict(result=3, option=0)
        session.request = request
        with self.assertRaisesRegex(RuntimeError, "保存失败"):
            await session.save_channel(Channel(), False)
        self.assertEqual(len(calls), 1)

    async def test_delete_only_one_command_and_checks_custom_bank(self):
        session = self.session(); events = []
        async def request(command, body, response):
            events.append((command, fields(body)))
            return dict(result=0, option=1)
        async def read_channels(progress=None, kinds=(1,2)):
            events.append(kinds)
            return [Channel(seq=1)]
        async def current():
            session.current = (2,1); return session.current
        session.request = request; session._read_channels = read_channels; session._read_current = current
        await session.delete_channel(Channel(seq=2))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][0], 20010)
        self.assertEqual(fields(events[0][1][4]), {1:2,2:2})
        self.assertEqual(events[1], (2,))

    async def test_reorder_switches_only_after_verified_writes(self):
        session = self.session(); events = []
        before = [Channel(seq=4,name="four"), Channel(seq=5,name="five")]
        after = [replace(before[1],seq=4), replace(before[0],seq=5)]
        async def request(command,body,response):
            events.append("write"); return dict(result=0,option=2)
        async def read_channels():
            events.append("verify"); return after
        async def current():
            events.append("current"); session.current = (2,4); return session.current
        async def switch(key):
            events.append(("switch",key)); session.current = key; return key
        session.request=request; session._read_channels=read_channels
        session._read_current=current; session._switch=switch
        await session.sync(after,before,{(2,4):(2,5),(2,5):(2,4)})
        self.assertEqual(events,["current","write","write","verify","current",("switch",(2,5))])

    async def test_partial_sync_only_mutates_and_verifies_requested_bank(self):
        session=self.session(); calls=[]
        before=[Channel(seq=1,name="before")]; after=[replace(before[0],name="after")]
        async def request(command,body,response):
            calls.append((command,fields(body))); return dict(result=0,option=2)
        async def read_channels(kinds):
            self.assertEqual(kinds,(2,)); return after
        async def current(): session.current=(1,1); return session.current
        session.request=request; session._read_channels=read_channels; session._read_current=current
        self.assertEqual(await session.sync(after,before,kinds=(2,)),after)
        self.assertEqual(len(calls),1)
        self.assertEqual(fields(calls[0][1][4])[2],2)
        with self.assertRaisesRegex(ValueError,"范围"):
            await session.sync(after+[Channel(kind=1)],before,kinds=(2,))
        self.assertEqual(len(calls),1)


if __name__ == '__main__':
    unittest.main()
