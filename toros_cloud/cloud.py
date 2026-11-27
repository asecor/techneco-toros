"""Stand-in for the cloud server the Toros Wi-Fi module reports to (server.techneco.nl, TCP 7001).

Frame: A5, sequence, type, payload length (2 bytes LE), payload, 16-bit sum of all preceding bytes (LE).
A type 0x30 frame has 14 header bytes after the length, then 7-byte records: id (3 bytes LE), value (4 bytes LE, signed).
"""
import asyncio
import collections
import datetime
import json
import logging
import os
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
HELLO = 0x12                  # module -> server every ~10 s: firmware string and MAC, never answered
TIME_REQUEST = 0x17           # module -> server, empty; the server answers type 1 with a 4-byte LE Unix time
QUERY = 0x20                  # module -> server, 1 payload byte (0 or 1); the server answers type 1 or 2
ACKED_KINDS = {DATA}          # data frames get the 7-byte ack
STATS_INTERVAL = 60
HEADER = 19
RECORD = 7
MAX_LENGTH = 1024
FLAG = 0x10000                # bit 16 of a record id: no valid value (always 0 in the archive, even with the compressor running)
CLOCK_UNSET = {0, 0xFFFFFFFF}  # frame header time before the module has asked for the time (first frames after it boots)
SENTINELS = {32767, -32768}   # raw values of a x0.1 sensor with no reading (room_c 3276.7 right after a module boot)
# Shape (frame type, payload length) of the vendor's replies to the module's 0x20 queries, constant from 23 Oct to 26 Nov 2026.
# The payload of the 20:00 reply is module-specific (it contains the id from the data frame headers), so it is not built in:
# it is learned while the vendor answers, or set with [cloud] replies.
REPLY_SHAPES = {'20:00': (0x01, 20), '20:01': (0x02, 1)}
DEFAULT_REPLIES = {'20:01': '02:02'}

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


# Open-circuit readings when the sensor is not fitted (tbr, tzc, tmg); decoded but never written to InfluxDB.
OPEN_CIRCUIT = {189, 241, 251}
# Unknown ids that read 0 are not stored, except these: electric element and active cooling kWh, which should stay 0.
WATCHED = {10, 12, 14}
# monitor_state bits (status code 539E)
COMPRESSOR, DHW, HEATING, ELEMENT = 1024, 4, 16, 2048


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
    clock = int.from_bytes(frame.raw[9:13], 'little')
    fields = {'frame_time': None if clock in CLOCK_UNSET else clock}
    for i in range(0, len(records), RECORD):
        record_id = int.from_bytes(records[i:i + 3], 'little')
        value = int.from_bytes(records[i + 3:i + 7], 'little', signed=True)
        if record_id & FLAG:
            continue                    # e.g. 65543 (id 7) and 65773 (id 237): the value is a placeholder 0
        name, scale = FIELDS.get(record_id, ('id_%d' % record_id, 1))
        if scale != 1 and value in SENTINELS:
            continue
        fields[name] = round(value * scale, 1) if scale != 1 else value
    state = fields.get('monitor_state')
    if state is not None:
        on = bool(state & COMPRESSOR)
        fields.update(compressor_on=int(on), dhw_on=int(on and bool(state & DHW)),
                      heat_on=int(on and bool(state & HEATING)), element_on=int(bool(state & ELEMENT)))
    return fields


def storable(fields):
    """The fields worth keeping in InfluxDB: no open-circuit sensors and no unknown ids stuck at 0.

    Nothing from a frame sent before the module's clock was set: after a boot those carry placeholders
    (setpoint 0, distribution state 0, control value 0). The archive keeps them.
    """
    if fields.get('frame_time') is None:
        return {}
    drop = {FIELDS[i][0] for i in OPEN_CIRCUIT if i in FIELDS}
    watched = {'id_%d' % i for i in WATCHED}
    return {k: v for k, v in fields.items()
            if k not in drop and (not k.startswith('id_') or v != 0 or k in watched)}


def parse_reply(text):
    """'kk:hex' (frame type and payload) -> (kind, payload bytes), or None if malformed."""
    try:
        kind, payload = text.split(':')
        kind, payload = int(kind, 16), bytes.fromhex(payload)
    except (AttributeError, ValueError):
        return None
    return (kind, payload) if kind <= 0xFF and len(payload) <= MAX_LENGTH else None


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
        self.stats = collections.Counter()
        self.replies_path = Path(config['archive_directory']) / 'learned_replies.json' if config.get('archive_directory') else None
        self.replies = self._load_replies()

    def _load_replies(self):
        replies = dict(DEFAULT_REPLIES)
        for key, value in self.config.get('replies', {}).items():
            if parse_reply(value) and self.acceptable(key, value):
                replies[key] = value
            else:
                log.warning('ignoring configured reply %r: %r', key, value)
        if not self.replies_path:
            return replies
        try:
            stored = json.loads(self.replies_path.read_text())
        except FileNotFoundError:
            return replies
        except (OSError, ValueError) as error:
            log.warning('ignoring %s (%r); using the built-in replies', self.replies_path, error)
            return replies
        for key, value in (stored.items() if isinstance(stored, dict) else ()):
            if parse_reply(value) and self.acceptable(key, value):
                replies[key] = value
            else:
                log.warning('ignoring stored reply %r: %r', key, value)
        return replies

    @staticmethod
    def acceptable(key, value):
        """A plausible answer to a query: never an empty ack, and the observed type and size where those are known."""
        kind, payload = parse_reply(value)
        shape = REPLY_SHAPES.get(key)
        return bool(payload) and (not shape or (kind, len(payload)) == shape)

    def learn(self, request, reply):
        """Remember what the real server answered to a query, so that the answer can be repeated if it goes away."""
        key = '%02x:%s' % (request[0], request[1].hex())
        value = '%02x:%s' % (reply.kind, reply.raw[5:-2].hex())
        if self.replies.get(key) == value:
            return
        if not self.acceptable(key, value):
            log.info('not learning %s as the reply to %s', value, key)
            return
        self.replies[key] = value
        log.info('learned server reply to %s: %s', key, value)
        if self.replies_path:
            temporary = self.replies_path.with_name(self.replies_path.name + '.tmp')
            temporary.write_text(json.dumps(self.replies, indent=1))
            os.replace(temporary, self.replies_path)   # a restart mid-write never leaves a truncated file

    def local_reply(self, frame):
        """What to send for a module frame when the real server is not answering, or None."""
        if frame.kind in ACKED_KINDS:
            return build(frame.seq, ACK)
        if frame.kind == TIME_REQUEST:
            return build(frame.seq, ACK, int(time.time()).to_bytes(4, 'little'))
        if frame.kind == QUERY:
            known = parse_reply(self.replies.get('%02x:%s' % (frame.kind, frame.raw[5:-2].hex()), ''))
            if known:
                return build(frame.seq, *known)
            self.stats['unanswered'] += 1
            log.warning('no known reply to query %s', frame.raw.hex())
        return None

    async def start(self):
        if not self.allow:
            log.warning('[cloud] allow is empty: any client on the network can connect')
        server = await asyncio.start_server(self.handle, self.config['listen'], self.config['port'])
        log.info('listening on %s, mode %s, upstream %s:%d', ', '.join(str(s.getsockname()) for s in server.sockets),
                 self.config['mode'], *self.upstream)
        if self.sink and self.config.get('bridge_measurement'):
            self.reporter = asyncio.create_task(self._report())
        return server

    async def _report(self):
        """Once a minute, the bridge's own counters since the last report."""
        names = ('connections', 'hello', 'time_requests', 'queries', 'local_replies', 'upstream_failures', 'unanswered')
        while True:
            await asyncio.sleep(STATS_INTERVAL)
            counts, self.stats = self.stats, collections.Counter()
            self.sink.put(line(self.config['bridge_measurement'], {n: counts[n] for n in names}, time.time()))

    def record(self, direction, peer, frame, pending=None):
        """Archive, count and store one frame. pending maps seq -> (kind, payload) of this connection's open requests."""
        self.archive.write(direction, peer, frame)
        log.debug('%s %s %s', peer, direction, frame.raw.hex())
        if direction == 'local':
            self.stats['local_replies'] += 1
        elif direction == 'down' and pending is not None:
            request = pending.pop(frame.seq, None)
            if request and request[0] == QUERY:
                self.learn(request, frame)
        if direction != 'up':
            return
        names = {HELLO: 'hello', TIME_REQUEST: 'time_requests', QUERY: 'queries'}
        if frame.kind in names:
            self.stats[names[frame.kind]] += 1
        if frame.kind in (TIME_REQUEST, QUERY) and pending is not None:
            pending[frame.seq] = (frame.kind, frame.raw[5:-2])
        fields = storable(decode(frame) or {})
        if fields and self.sink:
            self.sink.put(line(self.config['measurement'], fields, time.time()))

    async def handle(self, reader, writer):
        peer = writer.get_extra_info('peername')[0]
        if self.allow and peer not in self.allow:
            log.warning('refused connection from %s (not in [cloud] allow)', peer)
            writer.close()
            return
        upstream = None
        self.stats['connections'] += 1
        if self.config['mode'] != 'standalone':
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(*self.upstream), self.config['connect_timeout'])
            except (OSError, asyncio.TimeoutError) as error:
                self.stats['upstream_failures'] += 1
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
        pending = {}        # per connection: a request left unanswered here must not match a later connection's frame

        async def pump(source, target, direction):
            parser = FrameReader()
            while data := await self._read(source):
                target.write(data)          # forward bytes untouched; parsing is only a copy
                await target.drain()
                for frame in parser.feed(data):
                    self.record(direction, peer, frame, pending)

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
                raw = self.local_reply(frame)
                if raw:
                    writer.write(raw)
                    self.record('local', peer, Frame(raw))
            await writer.drain()


async def serve(config):
    sink = InfluxSink(config['influx_url'], config['influx_db']) if config['influx_url'] else None
    server = await Bridge(config, sink, Archive(config['archive_directory'])).start()
    async with server:
        await server.serve_forever()


def run(config):
    asyncio.run(serve(config))
