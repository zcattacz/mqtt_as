# print("this is a shim for cpython")

def const(v):
    return v

def native(fn):
    return fn

def patch_socket():
    import socket
    def write(s, buf):
        return s.send(buf)
    def read(s, n):
        return s.recv(n)
    def readinto(s, buf, n):
        return s.recv_into(buf, n)
    socket.socket.write = write
    socket.socket.read = read
    socket.socket.readinto = readinto
    return socket

def patch_asyncio():
    import asyncio

    async def sleep_ms(t):
        #print("sleep_ms,",t)
        return await asyncio.sleep(t/1000)
    
    async def wait_for_ms(co, t):
        return await asyncio.wait_for(co, t/1000)
    
    def new_event_loop():
        #print("not applicable on cpython")
        pass
    
    asyncio.sleep_ms = sleep_ms
    asyncio.wait_for_ms = wait_for_ms
    asyncio.new_event_loop = new_event_loop
    return asyncio

def patch_time():
    class Time:
        def __init__(self) -> None:
            import time
            self._time = time
            self._init_on = time.time()
        def time(self):
            return int((self._time.time()-self._init_on))
        def ticks_ms(self):
            return int((self._time.time()-self._init_on)*1000)
        def ticks_us(self):
            return int((self._time.time()-self._init_on)*1000*1000)
        def ticks_diff(self, a, b):
            return a-b
    return Time()
    
def patch_gc():
    import gc, resource
    gc._patch_pgsz = resource.getpagesize()
    
    def mem_free() -> int:
        with open("/proc/meminfo", "r") as fp:
            fmem = fp.read().split("\n")
            for m in fmem:
                if m.startswith("MemFree"):
                    fm = int(m.split()[1])*1024
                    return int(fm)
    def mem_alloc() -> int:
        with open("/proc/self/statm") as fp:
            _ = fp.read().split()
            if len(_)==7:
                pmem = int(_[1])
                am = pmem*gc._patch_pgsz
            return int(am)
    gc.mem_free = mem_free
    gc.mem_alloc = mem_alloc
    return gc

import os
def dummy_machine():
    class Machine():
        def __init__(self) -> None:
            pass
        def reset_cause(self):
            return -9
        def freq(self):
            return -9
        def reset(self):
            exit()
        def _find_free_ser(self, dev_patt="ttyUSB"):
            for d in os.listdir("/dev"):
                fp = f"/dev/{d}"
                if d.startswith(dev_patt):
                    try:
                        with open(fp, "rb") as s:
                            pass
                        return fp
                    except Exception as ex:
                        print("trying to open:", fp, "error:", ex.args, type(ex))
                        continue
        def UART(self, fp, baudrate, **kwargs):
            print(" - Make sure pyserial is installed")
            print(" - Returning first free ttyUSB by defaut")
            # check path
            if not fp:
                fp_full = self._find_free_ser()
            else:
                fp_full = fp if fp[0] != "/" else "/dev/" + fp
            if fp_full:
                # patch kwargs
                d = {"stop": "stopbits", "bits": "bytesize"}
                for mka, cka in d.items():
                    if mka in kwargs:
                        kwargs[cka] = kwargs[mka]
                        del kwargs[mka]
                import serial
                ser = serial.serial_for_url(url =fp_full, baudrate=baudrate, **kwargs)
                # patch any()
                def any(self):
                    return self.in_waiting
                ser.__class__.any = any
                return ser
            else:
                raise Exception("can't find available device")
    return Machine()

import subprocess as subp
import socket
import fcntl
import struct

class GW_NIC():
    # this mimic class is for monitoring NIC that connects to the server 
    # (not necessarily via default route and not necessarily a WI).
    # will add ifup/ifdown/scan mgmt caps later to mimic mpy
    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._SIOCGIFADDR = 0x8915
        self._SIOCGIFNETMASK = 0x891b
        self._SIOCGIFHWADDR = 0x8927
        # set this ip if target server is on a non-default route.
        # and we only care about this route related status
        self.dest_ip = "0.0.0.0"

        #It's impossible to determine the relevant wan iface when all are down.
        #This is user speficied wan iface when all interface down.
        self._id = ""
        # scan ifaces dynamically, on each call to ifconfig etc.
        self.id, self._gw = "", "" # self._get_wan_info()
        self._vifs = None
        
    def _run(self, cmd):
        with subp.Popen(cmd, stdout=subp.PIPE) as p:
            return p.stdout.readlines()

    def _chk_route(self, routes, dest):
        #print("gw for:", dest)
        host = bytearray(socket.inet_aton(dest))
        host.reverse() # ip bytes in route table are reversed
        routes.reverse() # defaut to last
        for _ in routes:
            r = _.split()
            _dest = bytearray.fromhex(r[1])
            mask = bytes.fromhex(r[7])
            h = host.copy()
            for i in range(0,4):
                h[i] &= mask[i]
            if _dest == h:
                return r
        
    def _get_vifaces(self):
        vifs = []; dnet="/sys/devices/virtual/net"
        for v in os.listdir(dnet):
            vifs.append(v)
        return vifs
    
    def _get_wifaces(self):
        wifs = []; dnet = "/sys/class/net"
        for v in os.listdir(dnet):
            if os.path.exists(os.path.join(dnet, v, "wireless")):
                wifs.append(v)
        return wifs
    
    def set_default_wan_iface(self, dev):
        self._id = dev

    def _get_route_info(self, dest):
        # note: lo is not in route, can't test with loopback iface.
        with open("/proc/net/route") as fp:
            routes = fp.readlines()[1:]
        if len(routes) == 0:
            return "", ""

        if self.dest_ip == "0.0.0.0":
            r = routes[0].split()
            # what if can we miss a default route?
            # this is returning the first iface
        else:
            r = self._chk_route(routes, dest)
        #print(r)
        iface = r[0]
        gw = bytearray.fromhex(r[2])
        gw.reverse()
        gw = socket.inet_ntoa(gw)
        return iface, gw
    
    def _get_wan_info(self):
        iface, gw = self._get_route_info(self.dest_ip)
        if iface:
            return iface, gw
        else:
            # no route is available, no active interface presented
            # return user sepecified interface
            return self._id, ""
    
    def config(self, s):
        self.id, self._gw = self._get_wan_info()
        if s == "mac":
            #alternatively  /sys/class/net/{wan_if}/address
            #https://www.kernel.org/doc/Documentation/ABI/testing/sysfs-class-net
            return self._ireq_addr(self._SIOCGIFHWADDR)
        
    def status(self, s):
        if s == "rssi":
            with open("/proc/net/wireless", "r") as fp:
                stat = fp.read()
            lns = stat.splitlines()
            for l in lns[2:]:
                if l.startswith(self.id):
                    # only return the WI for default route
                    return int(l.split()[3].strip("."))
    
    def isconnected(self):
        self.id, self._gw = self._get_wan_info()
        ops = ""
        if self.id:
            with open(f"/sys/class/net/{self.id}/operstate") as fp:
                ops = fp.read().strip()
                print(f"inetface state: '{ops}'")
        return ops == "up"
    
    def _ireq_addr(self, ireq):
        # skip ioctl request if interface is n/a.
        if not self.id:
            if ireq == self._SIOCGIFHWADDR:
                return b''
            else:
                return ""
        #Reference Info:
        #https://man7.org/linux/man-pages/man7/netdevice.7.html
        #https://github.com/rlisagor/pynetlinux/blob/master/pynetlinux/ifconfig.py
        iparam = struct.pack('16sH14s',
                             bytes(self.id[:15],"utf8"),
                             socket.AF_INET,
                             b'\0'*14)
        try:
            _ = fcntl.ioctl(self._sock.fileno(), ireq, iparam)
            if ireq == self._SIOCGIFHWADDR:
                return struct.unpack('16sH6s8x', _)[2]
            else:
                _ = struct.unpack('16sH2x4s8x',_)[2]
                return socket.inet_ntoa(_)
        except:
            return ""

    def ifconfig(self):
        self.id, self._gw = self._get_wan_info()
        ip = self._ireq_addr(self._SIOCGIFADDR)
        mask = self._ireq_addr(self._SIOCGIFNETMASK)
        dns = ""
        with open("/etc/resolv.conf") as fp:
            for l in fp.readlines():
                if l.startswith("nameserver"):
                    dns = l.split()[1]
                    break
        return (ip, mask, self._gw, dns)
    
class WIFI:
    def reset(self):
        return GW_NIC(), None

def dummy_wifi():
    return WIFI()
