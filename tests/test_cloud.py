import asyncio
import json
import tempfile
import time
import unittest

from toros_cloud import cloud

# Captured from the module on 2026-10-23. The 4-byte module id in the header (bytes 5-8) is zeroed and the checksum redone.
DATA_98 = bytes.fromhex(
    'a516305b00000000005e' '1edb6ab21100008400c0000000000000' '01000000000000be0000010000001701'
    '00d2000000f10000b007000006000000' '010000bb0000cc0000001200008d0000' '00040000ef00000005000007010000bc'
    '000007020000520c')
DATA_63 = bytes.fromhex(
    'a515303800000000005e' '1edb6ab0110000830007000000000000' '20000000000000eb000001000000bd00'
    '0090fdffff020000ed000000030000e9' '0000005d0b')
SERVER_ACK = bytes.fromhex('a515010000bb00')
DATA_FRAME_SEQ_1E = cloud.Frame(cloud.build(0x1E, cloud.DATA, bytes(14)))
CONFIG_REPLY = '01:00112233445566778899aabbccddeeff00112233'   # the real reply's shape, made-up payload


def data_frame(*records, clock=0x6ADB1E5E):
    """A 0x30 frame with the given (raw id, value) records and header time."""
    payload = bytes(4) + clock.to_bytes(4, 'little') + bytes(6)
    payload += b''.join(i.to_bytes(3, 'little') + v.to_bytes(4, 'little', signed=True) for i, v in records)
    return cloud.Frame(cloud.build(1, cloud.DATA, payload))


class FrameTests(unittest.TestCase):
    def test_captured_frames_verify(self):
        frames = cloud.FrameReader().feed(DATA_98 + DATA_63 + SERVER_ACK)
        self.assertEqual([(f.seq, f.kind, len(f.raw)) for f in frames], [(0x16, 0x30, 98), (0x15, 0x30, 63), (0x15, 1, 7)])

    def test_ack_matches_the_real_server(self):
        self.assertEqual(cloud.build(0x15, cloud.ACK), SERVER_ACK)

    def test_decode(self):
        fields = cloud.decode(cloud.Frame(DATA_98))
        self.assertEqual(fields['tbu_c'], 14.1)
        self.assertEqual(fields['ttw_c'], 51.9)
        self.assertEqual(fields['room_c'], 20.4)
        self.assertEqual(fields['tci_c'], 23.9)
        self.assertEqual(fields['fault'], 0)
        self.assertEqual(fields['tzc_solar_c'], 196.8)
        self.assertEqual(cloud.decode(cloud.Frame(DATA_63))['tbr_c'], -62.4)
        self.assertIsNone(cloud.decode(cloud.Frame(SERVER_ACK)))

    def test_flagged_records_are_skipped(self):
        # Seen as 65543 (id 7, 0 while the compressor ran at ~51 Hz) and 65773 (id 237, 0 instead of 2205).
        fields = cloud.decode(data_frame((7 | cloud.FLAG, 0), (237 | cloud.FLAG, 0), (18, 13)))
        self.assertNotIn('fvd_hz', fields)
        self.assertNotIn('fw_crc', fields)
        self.assertEqual(fields['tbu_c'], 1.3)

    def test_sentinel_temperature_is_skipped(self):
        fields = cloud.decode(data_frame((187, 32767), (188, 554), (1, 32767)))
        self.assertNotIn('room_c', fields)
        self.assertEqual((fields['ttw_c'], fields['fault']), (55.4, 32767))   # unscaled fields keep their value

    def test_frames_before_clock_sync_are_not_stored(self):
        # The 98-byte frame sent 3 s after a module boot on 2026-11-26: placeholders throughout.
        boot = data_frame((187, 32767), (190, 0), (279, 0), (192, 0), (18, 32), clock=0xFFFFFFFF)
        self.assertIsNone(cloud.decode(boot)['frame_time'])
        self.assertEqual(cloud.storable(cloud.decode(boot)), {})
        self.assertEqual(cloud.storable(cloud.decode(data_frame((18, 13), clock=0))), {})

    def test_run_state_fields(self):
        def state(value):
            record = (235).to_bytes(3, 'little') + value.to_bytes(4, 'little', signed=True)
            body = bytes([0xA5, 1, 0x30]) + (14 + 7).to_bytes(2, 'little') + bytes(14) + record
            return cloud.decode(cloud.Frame(body + cloud.checksum(body).to_bytes(2, 'little')))
        pick = lambda f: (f['compressor_on'], f['dhw_on'], f['heat_on'], f['element_on'])
        self.assertEqual(pick(state(1)), (0, 0, 0, 0))
        self.assertEqual(pick(state(16)), (0, 0, 0, 0))         # heating mode, compressor off
        self.assertEqual(pick(state(1028)), (1, 1, 0, 0))
        self.assertEqual(pick(state(1040)), (1, 0, 1, 0))
        self.assertEqual(pick(state(3088)), (1, 0, 1, 1))

    def test_storable_drops_open_circuit_and_zero_unknowns(self):
        fields = {'frame_time': 1, 'tbr_c': -62.4, 'tzc_solar_c': 196.8, 'tmg_c': 6490.7, 'id_184': 0, 'id_999': 3, 'id_12': 0, 'tbu_c': 14.1}
        self.assertEqual(cloud.storable(fields), {'frame_time': 1, 'id_999': 3, 'id_12': 0, 'tbu_c': 14.1})

    def test_bad_bytes_are_skipped(self):
        reader = cloud.FrameReader()
        corrupt = bytearray(DATA_63)
        corrupt[30] ^= 0xFF
        frames = reader.feed(b'\x00junk' + bytes(corrupt) + DATA_98[:40])
        self.assertEqual(frames, [])
        self.assertEqual(len(reader.feed(DATA_98[40:])), 1)       # split across reads
        self.assertGreater(reader.dropped, 0)

    def test_oversized_length_is_rejected(self):
        self.assertEqual(cloud.FrameReader().feed(b'\xa5\x01\x30\xff\xff' + b'\x00' * 20), [])

    def test_line_protocol(self):
        self.assertEqual(cloud.line('hp_live', {'tbu_c': 14.1, 'fault': 0}, 1792745054.7),
                         'hp_live fault=0.0,tbu_c=14.1 1792745054')


class FakeSink:
    def __init__(self):
        self.lines = []

    def put(self, text):
        self.lines.append(text)


def bridge_config(mode, upstream_port):
    return {'listen': '127.0.0.1', 'port': 0, 'mode': mode, 'upstream': '127.0.0.1:%d' % upstream_port, 'allow': [],
            'connect_timeout': 1.0, 'idle_timeout': 5, 'measurement': 'hp_live'}


class BridgeTests(unittest.TestCase):
    def exchange(self, mode, upstream_port, payload):
        async def go():
            sink = FakeSink()
            server = await cloud.Bridge(bridge_config(mode, upstream_port), sink).start()
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(payload)
            await writer.drain()
            try:
                reply = await asyncio.wait_for(reader.read(64), 2)
            except asyncio.TimeoutError:
                reply = b''
            writer.close()
            server.close()
            await server.wait_closed()
            return reply, sink.lines
        return asyncio.run(go())

    def test_auto_answers_locally_when_upstream_is_down(self):
        reply, lines = self.exchange('auto', 1, DATA_63)
        self.assertEqual(reply, cloud.build(0x15, cloud.ACK))
        self.assertEqual(len(lines), 1)

    def test_local_answers_time_and_learned_query(self):
        async def go():
            bridge = cloud.Bridge(dict(bridge_config('standalone', 1), replies={'20:00': CONFIG_REPLY}), FakeSink())
            server = await bridge.start()
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            replies = []
            for request in (cloud.build(0x60, cloud.TIME_REQUEST), cloud.build(0x1E, cloud.QUERY, b'\x00'),
                            cloud.build(0x1F, cloud.QUERY, b'\x07'), cloud.build(0x20, cloud.HELLO, b'\x00x')):
                writer.write(request)
                await writer.drain()
                try:
                    replies.append(await asyncio.wait_for(reader.read(64), 0.3))
                except asyncio.TimeoutError:
                    replies.append(b'')
            writer.close()
            server.close()
            await server.wait_closed()
            return replies, bridge.stats
        replies, stats = asyncio.run(go())
        time_reply = cloud.FrameReader().feed(replies[0])[0]
        self.assertEqual((time_reply.seq, time_reply.kind, len(time_reply.raw)), (0x60, 1, 11))
        self.assertAlmostEqual(int.from_bytes(time_reply.raw[5:9], 'little'), time.time(), delta=5)
        self.assertEqual(replies[1], cloud.build(0x1E, 1, bytes.fromhex(CONFIG_REPLY[3:])))   # configured reply
        self.assertEqual(replies[2:], [b'', b''])               # unknown query and hello get no reply
        self.assertEqual((stats['hello'], stats['time_requests'], stats['queries'], stats['unanswered']), (1, 1, 2, 1))

    def test_server_replies_to_queries_are_learned(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = cloud.Bridge(dict(bridge_config('relay', 1), archive_directory=directory), FakeSink())
            pending = {}
            bridge.record('up', 'm', cloud.Frame(cloud.build(0x1E, cloud.QUERY, b'\x07')), pending)
            bridge.record('down', 'm', cloud.Frame(cloud.build(0x1E, 2, b'\x05')), pending)
            self.assertEqual(bridge.replies['20:07'], '02:05')
            self.assertEqual(json.loads((cloud.Path(directory) / 'learned_replies.json').read_text())['20:07'], '02:05')
            self.assertEqual(list(cloud.Path(directory).iterdir()), [cloud.Path(directory) / 'learned_replies.json'])

    def test_empty_ack_is_never_learned_as_a_query_reply(self):
        bridge = cloud.Bridge(bridge_config('relay', 1), FakeSink())
        for query, reply in ((b'\x00', cloud.build(0x1E, 1)), (b'\x00', cloud.build(0x1E, 1, b'\xce\x12\xee\x6a')),
                             (b'\x01', cloud.build(0x1E, 1)), (b'\x07', cloud.build(0x1E, 1))):
            pending = {}
            bridge.record('up', 'm', cloud.Frame(cloud.build(0x1E, cloud.QUERY, query)), pending)
            bridge.record('down', 'm', cloud.Frame(reply), pending)
        self.assertEqual(bridge.replies, cloud.DEFAULT_REPLIES)

    def test_unanswered_request_does_not_match_a_later_connection(self):
        bridge = cloud.Bridge(bridge_config('relay', 1), FakeSink())
        first, second = {}, {}
        bridge.record('up', 'm', cloud.Frame(cloud.build(0x1E, cloud.QUERY, b'\x07')), first)   # never answered
        bridge.record('up', 'm', DATA_FRAME_SEQ_1E, second)
        bridge.record('down', 'm', cloud.Frame(cloud.build(0x1E, 2, b'\x09')), second)
        self.assertNotIn('20:07', bridge.replies)

    def test_stored_replies_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = cloud.Path(directory) / 'learned_replies.json'
            path.write_text(json.dumps({'20:00': '01:', '20:01': 'zz:02', '20:07': '02:05'}))
            replies = cloud.Bridge(dict(bridge_config('relay', 1), archive_directory=directory)).replies
            self.assertEqual(replies, dict(cloud.DEFAULT_REPLIES, **{'20:07': '02:05'}))
            path.write_text('{truncated')
            self.assertEqual(cloud.Bridge(dict(bridge_config('relay', 1), archive_directory=directory)).replies,
                             cloud.DEFAULT_REPLIES)

    def test_configured_and_learned_replies_must_have_the_observed_shape(self):
        bridge = cloud.Bridge(dict(bridge_config('relay', 1), replies={'20:00': CONFIG_REPLY, '20:01': '01:0203'}))
        self.assertEqual(bridge.replies, {'20:00': CONFIG_REPLY, '20:01': '02:02'})     # bad 20:01 ignored
        other = '01:' + 'ab' * 20
        pending = {}
        bridge.record('up', 'm', cloud.Frame(cloud.build(0x1E, cloud.QUERY, b'\x00')), pending)
        bridge.record('down', 'm', cloud.Frame(cloud.build(0x1E, 1, bytes.fromhex(other[3:]))), pending)
        self.assertEqual(bridge.replies['20:00'], other)

    def test_relay_forwards_both_ways(self):
        async def go():
            received = []

            async def fake_upstream(reader, writer):
                data = await reader.read(64)
                received.append(data)
                writer.write(SERVER_ACK)
                await writer.drain()
                writer.close()

            upstream = await asyncio.start_server(fake_upstream, '127.0.0.1', 0)
            sink = FakeSink()
            bridge = await cloud.Bridge(bridge_config('relay', upstream.sockets[0].getsockname()[1]), sink).start()
            reader, writer = await asyncio.open_connection('127.0.0.1', bridge.sockets[0].getsockname()[1])
            writer.write(DATA_63)
            await writer.drain()
            reply = await asyncio.wait_for(reader.read(64), 2)
            writer.close()
            for server in (bridge, upstream):
                server.close()
                await server.wait_closed()
            return received, reply, sink.lines

        received, reply, lines = asyncio.run(go())
        self.assertEqual(received, [DATA_63])
        self.assertEqual(reply, SERVER_ACK)
        self.assertEqual(len(lines), 1)

    def test_relay_mode_closes_without_upstream(self):
        reply, lines = self.exchange('relay', 1, DATA_63)
        self.assertEqual(reply, b'')

    def test_allow_list_refuses_others(self):
        async def go():
            config = dict(bridge_config('standalone', 1), allow=['192.0.2.1'])
            server = await cloud.Bridge(config, FakeSink()).start()
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            writer.write(DATA_63)
            try:
                reply = await asyncio.wait_for(reader.read(64), 2)
            except ConnectionResetError:        # closed with our bytes unread, so the OS sends a reset
                reply = b''
            server.close()
            await server.wait_closed()
            return reply
        self.assertEqual(asyncio.run(go()), b'')


if __name__ == '__main__':
    unittest.main()
