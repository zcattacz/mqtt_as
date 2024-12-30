# print("this is a shim for cpython")

def const(v):
    return v

def native(fn):
    return fn

def patch_socket():
    import socket
    from errno import ECONNRESET
    def write(s, buf):
        try:
            return s.send(buf)
        except BrokenPipeError:
            # catch as linkdown in upy
            raise OSError(ECONNRESET)
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
