# mqtt_as.py Asynchronous version of umqtt.robust
# (C) Copyright Peter Hinch 2017-2022.
# Released under the MIT licence.

# Pyboard D support added also RP2/default
# Various improvements contributed by Kevin Köck.

import gc

from micropython import const
try:
    import usocket as socket
    import uasyncio as asyncio
    import time
    from time import ticks_ms, ticks_diff
    print("running in micropython")
except:
    import micropython # for @micropython.native stub
    socket = micropython.patch_socket()
    asyncio = micropython.patch_asyncio()
    time = micropython.patch_time()
    ticks_ms = time.ticks_ms
    ticks_diff = time.ticks_diff
    print("running in cpython")
gc.collect()

import struct
from binascii import hexlify
from errno import EINPROGRESS, ETIMEDOUT
from errno import ENOTCONN, ECONNRESET
try:
    from machine import unique_id
except:
    # micropython unix port also lacks unique_id
    print(" - unix port")
    def unique_id():
        import random
        return f'some.uid.{random.choice("abcdefg")}s{random.randint(0,9999)}'.encode("ascii")

from sys import platform, implementation
gc.collect()

VERSION = (0, 7, 0)

# Default short delay for good SynCom throughput (avoid sleep(0) with SynCom).
_DEFAULT_MS = const(20)
_SOCKET_POLL_DELAY = const(5)  # 100ms added greatly to publish latency

# Legitimate errors while waiting on a socket. See uasyncio __init__.py open_connection().
ESP32 = platform == "esp32"
RP2 = platform == "rp2"
LINUX = platform == "linux"
WIN32 = platform == "win32"

BUSY_ERRORS = [EINPROGRESS, ETIMEDOUT]
if ESP32:
    # https://forum.micropython.org/viewtopic.php?f=16&t=3608&p=20942#p20942
    BUSY_ERRORS += [118, 119]  # Add in weird ESP32 errors
elif RP2:
    BUSY_ERRORS += [-110]
elif LINUX:
    BUSY_ERRORS += [11] # BlockingIOError Resource temporarily unavaliable

LINK_DOWN_ERRORS = [ENOTCONN, ECONNRESET]

ESP8266 = platform == "esp8266"
PYBOARD = platform == "pyboard"

class MsgQueue:
    def __init__(self, size):
        self._q = [0 for _ in range(max(size, 4))]
        self._size = size
        self._wi = 0
        self._ri = 0
        self._evt = asyncio.Event()
        self.discards = 0

    def put(self, *v):
        self._q[self._wi] = v
        self._evt.set()
        self._wi = (self._wi + 1) % self._size
        if self._wi == self._ri:  # Would indicate empty
            self._ri = (self._ri + 1) % self._size  # Discard a message
            self.discards += 1

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._ri == self._wi:  # Empty
            self._evt.clear()
            await self._evt.wait()
        r = self._q[self._ri]
        self._ri = (self._ri + 1) % self._size
        return r


config = {
    "client_id": hexlify(unique_id()),
    "server": None,
    "port": 0,
    "user": "",
    "password": "",
    "keepalive": 60,
    "ping_interval": 0,
    "ssl": False,
    "ssl_params": {},
    "response_time": 10,
    "clean_init": True,
    "clean": True,
    "max_repubs": 4,
    "will": None,
    "queue_len": 2 # must define, callbaskcs has been removed
}


class MQTTException(Exception):
    pass


def pid_gen():
    pid = 0
    while True:
        pid = pid + 1 if pid < 65535 else 1
        yield pid


def qos_check(qos):
    if not (qos == 0 or qos == 1):
        raise ValueError("Only qos 0 and 1 are supported.")


# MQTT_base class. Handles MQTT protocol on the basis of a good connection.
# Exceptions from connectivity failures are handled by MQTTClient subclass.
class MQTT_base:
    REPUB_COUNT = 0  # TEST

    def __init__(self, config):
        self.DEBUG = False
        self._use_poll_fix = getattr(implementation, "_mpy",2400) <= 2310
        # MQTT config
        self._client_id = config["client_id"]
        self._user = config["user"]
        self._pswd = config["password"]

        self._keepalive = config["keepalive"]
        if self._keepalive >= 65536:
            raise ValueError("invalid keepalive time")
        self._response_time = config["response_time"] * 1000  # Repub if no PUBACK received (ms).
        keepalive = 1000 * self._keepalive  # ms
        self._ping_interval = keepalive // 4 if keepalive else 20000

        self._max_repubs = config["max_repubs"]
        self._clean_init = config["clean_init"]  # clean_session state on first connection
        self._clean = config["clean"]  # clean_session state on reconnect
        will = config["will"]
        if will is None:
            self._lw_topic = ""
        else:
            self._set_last_will(*will)
        self._ssl = config["ssl"]
        self._ssl_params = config["ssl_params"]
        # Callbacks and coros
        self.up = asyncio.Event()
        self.down = asyncio.Event()
        self.queue = MsgQueue(config["queue_len"])
        # Network
        self.port = config["port"]
        if self.port == 0:
            self.port = 8883 if self._ssl else 1883
        self.server = config["server"]
        if self.server is None:
            raise ValueError("no server specified.")
        self._addr = None #cache the resolved DNS target
        self._sock:socket.socket = None #type:ignore
        self._sock_connect_timeout:int = 5*1000 #ms

        self.newpid = pid_gen()
        self.rcv_pids = set()  # PUBACK and SUBACK pids awaiting ACK response
        self.last_rx = ticks_ms()  # Time of last communication from broker
        self.lock = asyncio.Lock()
        
        self._has_connected = False
        self._in_connect = False # connect() as need _as_read/_as_write

    def _set_discnct(self, rc):
        if self._has_connected:
            print("mq: down rc:", rc)
            self._has_connected = False
            self.down.set()
        #else:
        #    if self.DEBUG:
        #        print("mq: (repeated) down rc:", rc)



    def _set_last_will(self, topic, msg, retain=False, qos=0):
        qos_check(qos)
        if not topic:
            raise ValueError("Empty topic.")
        self._lw_topic = topic
        self._lw_msg = msg
        self._lw_qos = qos
        self._lw_retain = retain

    def dprint(self, msg, *args):
        if self.DEBUG:
            try:
                print(time.time(), msg % args)
            except Exception as ex:
                print("dprint error:", ex.args, type(ex))
                print("dprint error:", msg)

    def _timeout(self, t):
        return ticks_diff(ticks_ms(), t) > self._response_time

    async def _as_read(self, n, sock=None):  # OSError caught by superclass
        if sock is None:
            sock = self._sock
        # Declare a byte array of size n. That space is needed anyway, better
        # to just 'allocate' it in one go instead of appending to an
        # existing object, this prevents reallocation and fragmentation.
        data = bytearray(n)
        buffer = memoryview(data)
        size = 0
        t = ticks_ms()
        while size < n:
            if not self._has_connected and not self._in_connect:
                raise OSError(-1, "Not connected on socket read")
            elif self._timeout(t):
                self._set_discnct("rtimeout")
                raise OSError(-1, "Timeout on socket read")
            try:
                msg_size = sock.readinto(buffer[size:], n - size)
            except OSError as e:  # ESP32 issues weird 119 errors here
                msg_size = None
                if e.args[0] in LINK_DOWN_ERRORS:
                    self._set_discnct("aread")
                    continue
                else:
                    if e.args[0] not in BUSY_ERRORS:
                        self.dprint("5e args: %s", e.args)
                        raise
            if msg_size == 0:  # Connection closed by host
                self._set_discnct("rhclose")
                raise OSError(-1, "Connection closed by host")
            if msg_size is not None:  # data received
                size += msg_size
                t = ticks_ms()
                self.last_rx = ticks_ms()
            await asyncio.sleep_ms(_SOCKET_POLL_DELAY)
        return data

    async def _as_write(self, bytes_wr:bytes, length=0, sock=None):
        if sock is None:
            sock = self._sock

        # Wrap bytes in memoryview to avoid copying during slicing
        if not isinstance(bytes_wr, memoryview):
            bytes_wr = memoryview(bytes_wr)
        if length:
            bytes_wr = bytes_wr[:length]
        t = ticks_ms()
        while bytes_wr:
            if not self._has_connected and not self._in_connect:
                raise OSError(-1, "Not connected on socket write")
            elif self._timeout(t):
                self._set_discnct("wtimeout")
                raise OSError(-1, "Timeout on socket write")
            try:
                n = sock.write(bytes_wr)
            except OSError as e:  # ESP32 issues weird 119 errors here
                n = 0
                if e.errno in LINK_DOWN_ERRORS:
                    self._set_discnct("awrt")
                    continue
                else:
                    if e.args[0] not in BUSY_ERRORS:
                        self.dprint("6e args: %s, msg: %s", e.args, bytes_wr)
                        raise
            if n:
                t = ticks_ms()
                bytes_wr = bytes_wr[n:]
            await asyncio.sleep_ms(_SOCKET_POLL_DELAY)

    async def _send_str(self, s, encoding="ascii"):
        try:
            s = s.encode(encoding)
        except:
            pass
        await self._as_write(struct.pack("!H", len(s)))
        await self._as_write(s)

    async def _recv_len(self) -> int:
        n = 0
        sh = 0
        while 1:
            res = await self._as_read(1)
            b = res[0]
            n |= (b & 0x7F) << sh
            if not b & 0x80:
                return n
            sh += 7
            await asyncio.sleep_ms(0)#no_tightloop
        raise Exception("_recv_len failed")
    
    def resolve(self, host):
        addrs4 = []
        for _ in range(0,5):
            try:
                addrs = socket.getaddrinfo(self.server, self.port, \
                                           socket.AF_INET, socket.SOCK_STREAM)
                for i, addr in enumerate(addrs):
                    self.dprint("DNS#%s: %s", i, addr)
                    addrs4.append(addr[-1])
                acnt = len(addrs4)
                if acnt > 0:
                    if acnt > 1:
                        self.dprint(f"%s has %s IPs", host, acnt)
                    import random
                    return random.choice(addrs4)
            except Exception as ex:
                if ex.errno == -3:
                    self.dprint("DNS failed, retrying")
                    continue
                else:
                    print("DNS unknown err:", ex.args)
        raise OSError(-1, "DNS failed")
    
    def _connect_poll_fix(self):
        import select
        poller = select.poll()
        poller.register(self._sock, select.POLLIN | select.POLLOUT)

        try:
            self._sock.connect(self._addr)
        except OSError as e:
            if e.errno != EINPROGRESS:
                raise e

        self.dprint("- poll_fix: polling sock for connect open")
        res = poller.poll(self._sock_connect_timeout)
        #print("c2u", res)
        poller.unregister(self._sock)
        #print("c2ud", res)
        if not res:
            #print("c2e", res)
            self._sock.close()
            raise OSError('Socket Connect Timeout')
    
    
    def _connect(self):
        if self._sock and self._sock.fileno() > 0:
            self.dprint("found socket %s left open before connect, closing", self._sock)
            try:
                self._sock.close()
            except Exception as ex:
                self.dprint("error closing socket, ignored: %s", ex)
            gc.collect()

        self.dprint("mq: creating socket")
        self._sock = socket.socket()
        if self._addr == None:
            self._addr = self.resolve(self.server)
            gc.collect()

        if self.DEBUG:
            if isinstance(self._addr, bytearray):
                _addr = self._addr
                _addr = (
                    socket.inet_ntop(_addr[0], _addr[4:4+4]),
                    int.from_bytes(_addr[2:4],"big"))
            else:
                _addr = self._addr
            self.dprint("mq:sock connecting to: %s", _addr)
        self._sock.setblocking(False)
        ## for now, necessary on windows
        if WIN32:
            self._sock.settimeout(0.2)
        if self._use_poll_fix:
            self._connect_poll_fix()
            gc.collect()
        else:
            try:
                self._sock.connect(self._addr)
            except Exception as ex:
                    raise ex
        if self._sock.fileno() < 0:
            raise OSError('Socket Connect Failed, RST?')
        if self.DEBUG:
            print("sock#", self._sock.fileno())

        # Socket connected

    async def connect(self, clean=True):

        self._in_connect = True
        self._has_connected = False
        self.dprint(f"mq:Connecting to broker {self.server}:{self.port}")
        try:
            self._connect()
            self.dprint("mq: socket connected")
        except OSError as e:
            if e.args[0] not in BUSY_ERRORS:
                self.dprint("mq: socket connect err, %s", e)
                raise
        await asyncio.sleep_ms(_DEFAULT_MS)
        if self._ssl:
            import ussl

            self._sock = ussl.wrap_socket(self._sock, **self._ssl_params)
        premsg = bytearray(b"\x10\0\0\0\0\0")
        msg = bytearray(b"\x04MQTT\x04\0\0\0")  # Protocol 3.1.1

        sz = 10 + 2 + len(self._client_id)
        msg[6] = clean << 1
        if self._user:
            sz += 2 + len(self._user)
            #msg[6] |= 0xC0
            msg[6] |= 0x80
            if self._pswd:
                sz += 2 + len(self._pswd)
                msg[6] |= 0x40
        if self._keepalive:
            msg[7] |= self._keepalive >> 8
            msg[8] |= self._keepalive & 0x00FF
        if self._lw_topic:
            sz += 2 + len(self._lw_topic) + 2 + len(self._lw_msg)
            msg[6] |= 0x4 | (self._lw_qos & 0x1) << 3 | (self._lw_qos & 0x2) << 3
            msg[6] |= self._lw_retain << 5

        i = 1
        while sz > 0x7F:
            premsg[i] = (sz & 0x7F) | 0x80
            sz >>= 7
            i += 1
        premsg[i] = sz
        async with self.lock:
            self.dprint("mq: sending connect pkt.")
            await self._as_write(premsg, i + 2)
            #self.dprint("mq: 1")
            await self._as_write(msg)
            #self.dprint("mq: 2")
            await self._send_str(self._client_id)
            #self.dprint("mq: 3")
            if self._lw_topic:
                await self._send_str(self._lw_topic)
                await self._send_str(self._lw_msg)
            if self._user:
                await self._send_str(self._user)
            if self._pswd:
                await self._send_str(self._pswd)
            #self.dprint("mq: 4")

        # Await CONNACK
        # read causes ECONNABORTED if broker is out; triggers a reconnect.
        resp = await self._as_read(4)
        if resp[3] != 0 or resp[0] != 0x20 or resp[1] != 0x02:  # Bad CONNACK e.g. authentication fail.
            raise OSError(-1, f"Connect fail: 0x{(resp[0] << 8) + resp[1]:04x} {resp[3]} (README 7)")

        #self._dbg_last_pub = None

        print("mq: connected to broker.")  # Got CONNACK
        self._in_connect = False
        self._has_connected = True
        asyncio.create_task(self._handle_msg())  # Task quits on connection fail.
        asyncio.create_task(self._keep_alive())
        self.up.set()  # Connectivity is up

    # Keep broker alive MQTT spec 3.1.2.10 Keep Alive.
    # Runs until ping failure or no response in keepalive period.
    async def _keep_alive(self):
        while True:
            pings_due = ticks_diff(ticks_ms(), self.last_rx) // self._ping_interval
            if pings_due >= 4:
                self.dprint("kalive: broker timeout.")
                break
            await asyncio.sleep_ms(self._ping_interval)
            try:
                if self._has_connected:
                    await self._ping()
                else:
                    break
            except OSError:
                break
        self._set_discnct("kplive")

    async def _handle_msg(self):
        while self._has_connected:
            async with self.lock:
                try:
                    await self._wait_msg()  # Immediate return if no message
                except Exception as e:
                    if e.args[0] in LINK_DOWN_ERRORS:
                        self._set_discnct("wmsg")
                        continue
            await asyncio.sleep_ms(_DEFAULT_MS)  # Let other tasks get lock

    async def _ping(self):
        async with self.lock:
            await self._as_write(b"\xc0\0")

    async def broker_up(self):  # Test broker connectivity
        if not self._has_connected:
            return False
        tlast = self.last_rx
        if ticks_diff(ticks_ms(), tlast) < 1000:
            return True
        try:
            await self._ping()
        except OSError:
            return False
        t = ticks_ms()
        while not self._timeout(t):
            await asyncio.sleep_ms(100)
            if ticks_diff(self.last_rx, tlast) > 0:  # Response received
                return True
        return False

    async def disconnect(self):
        if self._sock is not None:
            try:
                async with self.lock:
                    self._sock.write(b"\xe0\0")  # Close broker connection
                    await asyncio.sleep_ms(100)
            except OSError:
                pass
            self._close()
        self._set_discnct("disc")

    def _close(self):
        if self._sock is not None:
            self._sock.close()

    async def _await_pid(self, pid):
        t = ticks_ms()
        while pid in self.rcv_pids:  # local copy
            if self._timeout(t) or not self._has_connected:
                break  # Must repub or bail out
            await asyncio.sleep_ms(100)
        else:
            return True  # PID received. All done.
        return False

    # qos == 1: coro blocks until _wait_msg gets correct PID.
    # If WiFi fails completely subclass re-publishes with new PID.
    async def publish(self, topic, msg:bytes, retain=False, qos=0, oneshot=False):
        pid = next(self.newpid)
        if qos:
            self.rcv_pids.add(pid)
        async with self.lock:
            if oneshot:
                await self._publish2(topic, msg, retain, qos, 0, pid)
            else:
                await self._publish(topic, msg, retain, qos, 0, pid)
        if qos == 0:
            return

        count = 0
        while 1:  # Await PUBACK, republish on timeout
            if await self._await_pid(pid):
                return
            # No match
            if count >= self._max_repubs or not self._has_connected:
                raise OSError(-1)  # Subclass to re-publish with new PID
            async with self.lock:
                if oneshot:
                    await self._publish2(topic, msg, retain, qos, dup=1, pid=pid)  # Add pid
                else:
                    await self._publish(topic, msg, retain, qos, dup=1, pid=pid)  # Add pid
                await asyncio.sleep_ms(0) #no_tightloop
            count += 1
            self.REPUB_COUNT += 1

    @micropython.native
    def _mk_pub_header(self, pkt, sz2, retain, qos, dup):
        pkt[0] |= qos << 1 | retain | dup << 3
        sz = 2 + sz2
        if qos > 0:
            sz += 2
        if sz >= 0x200000: #128**3=2MB
            return -1
        i = 1
        while sz > 0x7F:
            pkt[i] = (sz & 0x7F) | 0x80
            sz >>= 7
            i += 1
        pkt[i] = sz
        return i

    async def _publish(self, topic, msg:bytes, retain, qos, dup, pid):
        pkt = bytearray(b"\x30\0\0\0")
        i = self._mk_pub_header(pkt, len(topic)+len(msg), retain, qos, dup)
        if i < 0:
            raise MQTTException("Strings too long.")
        try:
            await self._as_write(pkt, i+1)
            await self._send_str(topic)
            if qos > 0:
                struct.pack_into("!H", pkt, 0, pid)
                await self._as_write(pkt, 2)
            await self._as_write(msg)
        except Exception as ex:
            if ex.args[0] in LINK_DOWN_ERRORS:
                self._set_discnct("pub")
            raise

    async def _publish2(self, topic, msg:bytes, retain, qos, dup, pid):
        pos = 0
        slen = len(topic)+len(msg)
        plen = slen + 3 # extra bytes for i increament on large pkt
        # slen+2 <=128b = 1byte
        # slen+3 <=16k  = 2byte
        # slen+2 <=2M   = 3byte
        if qos > 0:
            plen += 2
        #print("mlen", len(msg), "slen", slen, "plen", plen)
        pkt = bytearray(b"\x30\0\0\0"+b"\0"*plen)
        #print("pkt_tpl", pkt)
        buf = memoryview(pkt)
        i = self._mk_pub_header(buf, slen, retain, qos, dup)
        if i < 0:
            raise MQTTException("Strings too long.")
        pos = i+1
        #print("pos(smallpkt=2)", pos)
        struct.pack_into("!H", buf, pos, len(topic))
        pos += 2
        #print("pkt.tsz", pos, pkt)
        buf[pos:pos+len(topic)] = topic.encode("ascii")
        pos += len(topic)
        #print("pkt.tpc", pos, pkt)
        if qos > 0:
            struct.pack_into("!H", buf, pos, pid)
            pos += 2
            #print("pkt.qos", pos, pkt)
        buf[pos:pos+len(msg)] = msg
        pos += len(msg)
        #print("pkt.msg", pkt, pos)
        try:
            await self._as_write(buf, pos)
        except Exception as ex:
            if ex.args[0] in LINK_DOWN_ERRORS:
                self._set_discnct("pub")
            raise

    # Can raise OSError if WiFi fails. Subclass traps.
    async def subscribe(self, topic, qos=0):
        pkt = bytearray(b"\x82\0\0\0")
        pid = next(self.newpid)
        self.rcv_pids.add(pid)
        struct.pack_into("!BH", pkt, 1, 2 + 2 + len(topic) + 1, pid)
        
        try:
            async with self.lock:
                await self._as_write(pkt)
                await self._send_str(topic)
                await self._as_write(qos.to_bytes(1, "little"))
        except Exception as ex:
            if ex.args[0] in LINK_DOWN_ERRORS:
                self._set_discnct("sub")

        if not await self._await_pid(pid):
            raise OSError(-1)

    # Can raise OSError if WiFi fails. Subclass traps.
    async def unsubscribe(self, topic):
        pkt = bytearray(b"\xa2\0\0\0")
        pid = next(self.newpid)
        self.rcv_pids.add(pid)
        struct.pack_into("!BH", pkt, 1, 2 + 2 + len(topic), pid)

        try:
            async with self.lock:
                await self._as_write(pkt)
                await self._send_str(topic)
        except Exception as ex:
            if ex.args[0] in LINK_DOWN_ERRORS:
                self._set_discnct("unsub")

        if not await self._await_pid(pid):
            raise OSError(-1)

    # Wait for a single incoming MQTT message and process it.
    # Subscribed messages are delivered to a callback previously
    # set by .setup() method. Other (internal) MQTT
    # messages processed internally.
    # Immediate return if no data available. Called from ._handle_msg().
    async def _wait_msg(self): # with lock from _handle_msg()
        try:
            res = self._sock.read(1)  # Throws OSError on WiFi fail
        except OSError as e:
            if e.args[0] in BUSY_ERRORS:  # Needed by RP2
                await asyncio.sleep_ms(0)
                return
            raise
        if res is None:
            return
        if res == b"":
            raise OSError(-1, "Empty response")

        if res == b"\xd0":  # PINGRESP
            self.dprint("mq.rcv:pingrsp")
            await self._as_read(1)  # Update .last_rx time
            return
        op = res[0]
        pid = b''

        if op == 0x40:  # PUBACK: save pid
            self.dprint("mq.rcv:pubAck")
            sz = await self._as_read(1)
            if sz != b"\x02":
                raise OSError(-1, "Invalid PUBACK packet")
            rcv_pid = await self._as_read(2)
            pid = rcv_pid[0] << 8 | rcv_pid[1]
            if pid in self.rcv_pids:
                self.rcv_pids.discard(pid)
            else:
                raise OSError(-1, "Invalid pid in PUBACK packet")

        if op == 0x90:  # SUBACK
            self.dprint("mq.rcv:subAck")
            resp = await self._as_read(4)
            if resp[3] == 0x80:
                raise OSError(-1, "Invalid SUBACK packet")
            pid = resp[2] | (resp[1] << 8)
            if pid in self.rcv_pids:
                self.rcv_pids.discard(pid)
            else:
                raise OSError(-1, "Invalid pid in SUBACK packet")

        if op == 0xB0:  # UNSUBACK
            self.dprint("mq.rcv:unsubAck")
            resp = await self._as_read(3)
            pid = resp[2] | (resp[1] << 8)
            if pid in self.rcv_pids:
                self.rcv_pids.discard(pid)
            else:
                raise OSError(-1)

        if op & 0xF0 != 0x30:
            return

        pid = b""
        self.dprint("mq.rcv:decMsg")
        sz = await self._recv_len()
        topic_len = await self._as_read(2)
        topic_len = (topic_len[0] << 8) | topic_len[1]
        topic = await self._as_read(topic_len)
        sz -= topic_len + 2
        if op & 6:
            pid = await self._as_read(2)
            pid = pid[0] << 8 | pid[1]
            sz -= 2
        msg = await self._as_read(sz)
        retained = op & 0x01
        self.queue.put(topic, msg, bool(retained))

        if op & 6 == 2:  # qos 1
            if pid == b'':
                raise Exception("decMsg: pid undefined.")
            pkt = bytearray(b"\x40\x02\0\0")  # Send PUBACK
            struct.pack_into("!H", pkt, 2, pid)
            await self._as_write(pkt)
        elif op & 6 == 4:  # qos 2 not supported
            raise OSError(-1, "QoS 2 not supported")

    async def _loop_for_event(self, evt_name, cbi): # callback_info
        if hasattr(self, "cbis_"+evt_name):
            getattr(self, "cbis_"+evt_name).append(cbi)
            return
        else:
            setattr(self, "cbis_"+evt_name, [cbi])
        
        evt = getattr(self, evt_name)
        print("start loop for callbacks on event:", evt_name)
        while True:
            try:
                await evt.wait()
                evt.clear()
                cbis = getattr(self, "cbis_"+evt_name)
                if len(cbis) < 1:
                    break
                print("event:", evt_name)
                for cbi in cbis:
                    if cbi[1]:
                        asyncio.create_task(cbi[0])
                    else:
                        await cbi[0]()
            except Exception as ex:
                print("cb_on_event:", evt, cbi)
                print("cb_on_event error:", ex.args, type(ex))
                raise
    
    async def _loop_for_msg(self, cbi): # callback_info
        if hasattr(self, "cbis_msg"):
            getattr(self, "cbis_msg").append(cbi)
            return
        else:
            setattr(self, "cbis_msg", [cbi])
        
        print("start loop for callbacks on msg")
        async for topic, msg, retained in self.queue: #type: ignore
            try:
                if len(self.cbis_msg) < 1:
                    break
                for cbi in self.cbis_msg:
                    if cbi[2]:
                        coro = cbi[0](topic, msg, retained, udata=cbi[2])
                    else:
                        coro = cbi[0](topic, msg, retained)
                    if cbi[1]:
                        asyncio.create_task(coro)
                    else:
                        await coro
            except Exception as ex:
                print("cb_on_msg:", topic, msg, retained, cbi)
                print("cb_on_event error:", ex.args, type(ex))
                raise

    def set_cb_on_event(self, evt, cb, new_task=False, udata=None):
        #This is one time job. don't reg dup callbacks, no checks here.
        if evt == "up":
            asyncio.create_task(self._loop_for_event("up", (cb, new_task, udata)))
        elif evt == "down":
            asyncio.create_task(self._loop_for_event("down", (cb, new_task, udata)))
        elif evt == "msg":
            asyncio.create_task(self._loop_for_msg((cb, new_task, udata)))


