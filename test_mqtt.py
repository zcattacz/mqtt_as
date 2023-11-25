# range.py Test of asynchronous mqtt client with clean session False.

from mqtt_as import MQTT_base, config
#from mqtt_sr import MQTT_base, config
import random, struct

try:
    import wifi
    wifi.reset()
    print("waiting for wifi")
    wifi.connect("ssid","pass")
    icfg = wifi.sta.ifconfig()
    print(icfg)
except:
    pass

try:
    import uasyncio as asyncio
    import gc, time
except:
    import micropython
    asyncio = micropython.patch_asyncio()
    gc = micropython.patch_gc()
    time = micropython.patch_time()

# pkt goes up to 2Mb, be nice...
#config['server'] = 'test.mosquitto.org'  # Change to suit
# mosquitto -c /etc/mosquitto/conf.d/local_p1883.conf -v -v
config['server'] = '127.0.0.1'  # Change to suit
#config['port'] = 1885  # Change to suit

config["client_id"] = "esp_test123x%s" % random.randint(1, 1000)
# when use_uid_as_client_id is set on mosquitto, if uid/cid mismatch, 
# client will be kicked out seconds~mininutes later after login with RST pkt
config["user"] = config["client_id"] 

client = None
pkt_last_id_snd = 0
pkt_last_id_rcv = 0


max_buf_size = -1 # -1=autodetect
pkt_incr_sz = 1024

use_oneshot = True

async def on_message():
    # receive self sent message from broker, check validity and sequence.
    print("-- message loop started")
    last_pl_size = 0; i = 0
    t0 = 0
    global pkt_last_id_rcv
    async for topic, msg, retained in client.queue:
        pkt_last_id_rcv = int.from_bytes(msg[:4], "little")
        if len(msg) == 0:
            t0 = time.ticks_ms()
        elif len(msg) == pkt_last_id_snd:
            print("total time (RTT): ", time.ticks_ms() - t0)
        else:
            try:
                assert len(msg), pkt_last_id_rcv
            except:
                print("bad pkt ?", len(msg), pkt_last_id_rcv, retained)
            try:
                assert len(msg), last_pl_size+pkt_incr_sz
            except:
                print("missing pkt ?:", len(msg))
        last_pl_size = len(msg)
        i += 1
        print("rcvd echo #", i, "\r", end="")

# for cb test
async def cb_on_message(tpc, msg, retained, udata=None):
    global pkt_last_id_rcv
    pkt_last_id_rcv = int.from_bytes(msg[:4], "little")
    if len(msg) == 0:
        udata["t0"] = time.ticks_ms()
    elif len(msg) == pkt_last_id_snd:
        print("total time (RTT): ", time.ticks_ms() - udata["t0"])
    else:
        try:
            assert len(msg), pkt_last_id_rcv
        except:
            print("bad pkt ?", len(msg), pkt_last_id_rcv, retained)
        try:
            assert len(msg), udata["last_pl_size"]+pkt_incr_sz
        except:
            print("missing pkt ?:", len(msg))
    udata["last_pl_size"] = len(msg)
    udata["i"] += 1
    print("echo #", udata["i"], "\r", end="")

async def _test_alloc(max_size, decr_size):
    for i in range(max_size, 1024, -decr_size):
        try:
            buf = bytearray(b'\0'*i)
            return i
        except Exception as ex:
            if "memory" in ex.args[0]:
                gc.collect()
                #print(gc.mem_free(), end="")
                await asyncio.sleep_ms(100)
                continue

async def get_max_alloc_buf_sz(max_buf_size):
    print("-- testing for max allocatable buffer")
    for decr_size in [500, 100, 50, 10]: #kb
        decr_size *= 1024
        #print(max_buf_size, decr_size)
        max_buf_size = await _test_alloc(max_buf_size, decr_size)
    gc.collect()
    print("max allocatable buffer size (byte): ", max_buf_size)
    return max_buf_size

async def main():
    global client, pkt_incr_sz, pkt_last_id_snd, t0_ms
    client = MQTT_base(config)
    asyncio.create_task(on_message())
    #udata = {"last_pl_size": 0, "i": 0, "t0": 0}
    #client.set_cb_on_event("msg", cb_on_message, udata=udata)
    
    #client.DEBUG = True
    await client.connect(True)
    await client.up.wait()
    client.up.clear()
    
    topic = "stress_test/cln"
    await client.subscribe(topic, qos=0)

    try:
        if max_buf_size == -1:
            # try the biggest possible payload size (lib hard limit 2Mb)
            print("available men (byte):", gc.mem_free())
            #max_mem_prob_sz = 1024*1024*2
            max_mem_prob_sz = min(1024*1024*2, gc.mem_free())
            buf_size = await get_max_alloc_buf_sz(max_mem_prob_sz)
        else:
            buf_size = max_buf_size
        
        # avoid too many pkt
        if pkt_incr_sz < buf_size/50:
            pkt_incr_sz = int(buf_size/50)
            print("pub increment size raised to:", pkt_incr_sz)

        gc.collect()
        buf = bytearray(b'\0'*buf_size)
        print(f"-- sending pubs, payload ranging from {0} to {buf_size} bytes in size")
        while client._has_connected:
            bufwr = memoryview(buf)
            step = pkt_incr_sz
            for i in range(0, len(buf), step):
                if i == 0:
                    t0 = time.ticks_ms()
                if i - step > 0:
                    for j in range(i-step,i):
                        bufwr[j] = i%255
                
                struct.pack_into("I", bufwr, 0, i) # first byte = pl size
                try:
                    await client.publish("stress_test/cln", bufwr[:i], oneshot=use_oneshot)
                    pkt_last_id_snd = i
                except Exception as ex:
                    gc.collect()
                    print("Error in stress test:", ex.args, type(ex))
                    break
            print("sent", pkt_last_id_snd/pkt_incr_sz , "pubs at", pkt_incr_sz, "bytes increament")
            print("total time (pub): ", time.ticks_ms() - t0)
            break
        print("max payload size sent (byte): ", pkt_last_id_snd)
        while pkt_last_id_rcv < pkt_last_id_snd:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("closing client")
        await client.disconnect()
# Define configuration
config['keepalive'] = 60
config["queue_len"] = 2

try:
    asyncio.run(main())
finally:
    asyncio.new_event_loop()
