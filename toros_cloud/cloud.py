"""Stand-in for the cloud server the Toros Wi-Fi module reports to (server.techneco.nl, TCP 7001).

Frame: A5, sequence, type, payload length (2 bytes LE), payload, 16-bit sum of all preceding bytes (LE).
A type 0x30 frame has 14 header bytes after the length, then 7-byte records: id (3 bytes LE), value (4 bytes LE, signed).
"""
import asyncio
import datetime
import json
import logging
from pathlib import Path
import queue
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

START = 0xA5
ACK = 0x01
DATA = 0x30
ACKED_KINDS = {DATA}          # the only type the real server was seen acknowledging
HEADER = 19
RECORD = 7
MAX_LENGTH = 1024

# Record ids identified from community findings, vendor metrics exports, and live captures.
FIELDS = {
    1: ('fault', 1),
    2: ('tvi_c', 0.1),
    3: ('tvu_c', 0.1),
    4: ('tci_c', 0.1),
    5: ('tcu_c', 0.1),
    6: ('tau_c', 0.1),
    7: ('fvd_hz', 1),
    8: ('kwh_standby', 1),
    # 9/11/197 relabelled 2026-10-24 after a hot-water-only run (see docs/operations.md).
    9: ('kwh_dhw', 1),
    11: ('kwh_heating', 1),
    13: ('kwh_cooling_passive', 1),
    18: ('tbu_c', 0.1),
    32: ('lockout_error', 1),
    187: ('room_c', 0.1),
    188: ('ttw_c', 0.1),
    189: ('tbr_c', 0.1),
    190: ('distribution_state', 1),
    192: ('control_value_c', 0.1),
    197: ('hours_dhw', 1),
    198: ('starts_dhw_compressor', 1),
    220: ('hours_heating_compressor', 1),
    221: ('starts_heating_compressor', 1),
    224: ('hours_compressor_total', 1),
    225: ('starts_cooling_passive', 1),
    235: ('monitor_state', 1),
    237: ('fw_crc', 1),
    241: ('tzc_solar_c', 0.1),
    251: ('tmg_c', 0.1),
    279: ('ot_room_setpoint_c', 0.1),
}


def checksum(data):
    return sum(data) & 0xFFFF


def build(seq, kind, payload=b''):
    body = bytes([START, seq & 0xFF, kind]) + len(payload).to_bytes(2, 'little') + payload
    return body + checksum(body).to_bytes(2, 'little')


class Frame:
    __slots__ = ('raw', 'seq', 'kind')

    def __init__(self, raw):
        self.raw, self.seq, self.kind = raw, raw[1], raw[2]


class FrameReader:
    """Splits a TCP byte stream into checked frames, skipping anything that doesn't verify."""

    def __init__(self):
        self.buffer = bytearray()
        self.dropped = 0

    def feed(self, data):
        self.buffer += data
        frames = []
        while True:
            start = self.buffer.find(START)
            if start < 0:
                self.dropped += len(self.buffer)
                self.buffer.clear()
                return frames
            if start:
                self.dropped += start
                del self.buffer[:start]
            if len(self.buffer) < 7:
                return frames
            length = int.from_bytes(self.buffer[3:5], 'little')
            if length > MAX_LENGTH:
                self._skip()
                continue
            total = length + 7
            if len(self.buffer) < total:
                return frames
            raw = bytes(self.buffer[:total])
            if checksum(raw[:-2]) != int.from_bytes(raw[-2:], 'little'):
                self._skip()
                continue
            del self.buffer[:total]
            frames.append(Frame(raw))

    def _skip(self):
        self.dropped += 1
        del self.buffer[0]


def decode(frame):
    """Field values of a data frame, or None for other frame types."""
    records = frame.raw[HEADER:-2]
    if frame.kind != DATA or not records or len(records) % RECORD:
        return None
    fields = {'frame_time': int.from_bytes(frame.raw[9:13], 'little')}
    for i in range(0, len(records), RECORD):
        record_id = int.from_bytes(records[i:i + 3], 'little')
        value = int.from_bytes(records[i + 3:i + 7], 'little', signed=True)
        name, scale = FIELDS.get(record_id, ('id_%d' % record_id, 1))
        fields[name] = round(value * scale, 1) if scale != 1 else value
    return fields


def line(measurement, fields, when):
    # Floats only, so a field never changes type between writes (InfluxDB 1.x rejects that).
    return '%s %s %d' % (measurement, ','.join('%s=%r' % (k, float(v)) for k, v in sorted(fields.items())), int(when))


class InfluxSink:
    """Writes line protocol to InfluxDB 1.x from a background thread so a slow database never stalls the module."""

    def __init__(self, url, database, size=10000):
        self.endpoint = url.rstrip('/') + '/write?' + urllib.parse.urlencode({'db': database, 'precision': 's'})
        self.queue = queue.Queue(maxsize=size)
        threading.Thread(target=self._run, name='influx', daemon=True).start()

    def put(self, text):
        try:
            self.queue.put_nowait(text)
        except queue.Full:
            log.warning('InfluxDB queue full; dropping a point')

    def _run(self):
        while True:
            batch = [self.queue.get()]
            while len(batch) < 500 and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            for attempt in range(6):
                try:
                    request = urllib.request.Request(self.endpoint, data='\n'.join(batch).encode(), method='POST')
                    with urllib.request.urlopen(request, timeout=10):
                        break
                except Exception as error:      # network errors and HTTP errors alike: retry, then give up
                    log.warning('InfluxDB write failed (%s); retrying in %d s', error, 2 ** attempt)
                    time.sleep(2 ** attempt)
            else:
                log.error('InfluxDB unreachable; dropped %d points', len(batch))


class Archive:
    """One JSONL file per UTC day with every frame in both directions, for working out the protocol later."""

    def __init__(self, directory):
        self.directory = Path(directory) if directory else None
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)

    def write(self, direction, peer, frame):
        if not self.directory:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        entry = {'time': now.isoformat(timespec='milliseconds'), 'dir': direction, 'peer': peer, 'hex': frame.raw.hex()}
        with (self.directory / ('cloud-%s.jsonl' % now.strftime('%Y-%m-%d'))).open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry) + '\n')


class Bridge:
    """auto: relay to the real server and answer locally when it can't be reached; relay or standalone: only that."""

    def __init__(self, config, sink=None, archive=None):
        self.config = config
        host, port = config['upstream'].rsplit(':', 1)
        self.upstream = (host, int(port))
        self.allow = set(config['allow'])
        self.sink = sink
        self.archive = archive or Archive('')

    async def start(self):
        if not self.allow:
            log.warning('[cloud] allow is empty: any client on the network can connect')
        server = await asyncio.start_server(self.handle, self.config['listen'], self.config['port'])
        log.info('listening on %s, mode %s, upstream %s:%d', ', '.join(str(s.getsockname()) for s in server.sockets),
                 self.config['mode'], *self.upstream)
        return server

    def record(self, direction, peer, frame):
        self.archive.write(direction, peer, frame)
        log.debug('%s %s %s', peer, direction, frame.raw.hex())
        if direction != 'up':
            return
        fields = decode(frame)
        if fields and self.sink:
            self.sink.put(line(self.config['measurement'], fields, time.time()))

    async def handle(self, reader, writer):
        peer = writer.get_extra_info('peername')[0]
        if self.allow and peer not in self.allow:
            log.warning('refused connection from %s (not in [cloud] allow)', peer)
            writer.close()
            return
        upstream = None
        if self.config['mode'] != 'standalone':
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(*self.upstream), self.config['connect_timeout'])
            except (OSError, asyncio.TimeoutError) as error:
                if self.config['mode'] == 'relay':
                    log.warning('upstream %s:%d unreachable (%r); closing %s', *self.upstream, error, peer)
                    writer.close()
                    return
                log.info('upstream %s:%d unreachable (%r); answering %s locally', *self.upstream, error, peer)
        try:
            if upstream:
                await self._relay(peer, reader, writer, *upstream)
            else:
                await self._answer(peer, reader, writer)
        except (OSError, asyncio.TimeoutError) as error:
            log.info('connection from %s ended: %r', peer, error)
        finally:
            for stream in (writer, upstream[1] if upstream else None):
                if stream:
                    stream.close()

    async def _read(self, stream):
        return await asyncio.wait_for(stream.read(4096), self.config['idle_timeout'])

    async def _relay(self, peer, reader, writer, up_reader, up_writer):
        async def pump(source, target, direction):
            parser = FrameReader()
            while data := await self._read(source):
                target.write(data)          # forward bytes untouched; parsing is only a copy
                await target.drain()
                for frame in parser.feed(data):
                    self.record(direction, peer, frame)

        tasks = [asyncio.create_task(pump(reader, up_writer, 'up')), asyncio.create_task(pump(up_reader, writer, 'down'))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                log.info('relay for %s ended: %r', peer, result)

    async def _answer(self, peer, reader, writer):
        parser = FrameReader()
        while data := await self._read(reader):
            for frame in parser.feed(data):
                self.record('up', peer, frame)
                if frame.kind in ACKED_KINDS:
                    reply = Frame(build(frame.seq, ACK))
                    writer.write(reply.raw)
                    self.record('local', peer, reply)
            await writer.drain()


async def serve(config):
    sink = InfluxSink(config['influx_url'], config['influx_db']) if config['influx_url'] else None
    server = await Bridge(config, sink, Archive(config['archive_directory'])).start()
    async with server:
        await server.serve_forever()


def run(config):
    asyncio.run(serve(config))
