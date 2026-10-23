import asyncio
import unittest

from toros_cloud import cloud

# Captured from the module on 2026-10-23 (module address and MAC removed from the hello frame).
DATA_98 = bytes.fromhex(
    'a516305b00d59894585e' '1edb6ab21100008400c0000000000000' '01000000000000be0000010000001701'
    '00d2000000f10000b007000006000000' '010000bb0000cc0000001200008d0000' '00040000ef00000005000007010000bc'
    '000007020000ab0e')
DATA_63 = bytes.fromhex(
    'a515303800d59894585e' '1edb6ab0110000830007000000000000' '20000000000000eb000001000000bd00'
    '0090fdffff020000ed000000030000e9' '000000b60d')
SERVER_ACK = bytes.fromhex('a515010000bb00')


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
