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

pkt_incr_sz = 1024*100
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

async def main():
    global client
    client = MQTT_base(config)
    #asyncio.create_task(on_message())
    udata = {"last_pl_size": 0, "i": 0, "t0": 0}
    client.set_cb_on_event("msg", cb_on_message, udata=udata)
    
    #client.DEBUG = True
    await client.connect(True)
    await client.up.wait()
    client.up.clear()
    
    topic = "stress_test/cln"
    await client.subscribe(topic, qos=0)

    try:
        print("available men (byte):", gc.mem_free())
        print("-- testing for max allocatable buffer")
        max_pkt_size = 0
        max_buf_size = 0
        buf = None
        for i in range(1024*1024*2, 1024, -1024):
            try:
                buf = bytearray(b'\0'*i)
                max_buf_size = i
                break
            except Exception as ex:
                if "memory" in ex.args[0]:
                    gc.collect()
                    #print(gc.mem_free(), end="")
                    await asyncio.sleep_ms(100)
                    continue
        gc.collect()
        print("max allocatable buffer size (byte): ", max_buf_size)
        t0 = 0
        print("-- sending pubs ")
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
                except Exception as ex:
                    gc.collect()
                    print("Error in stress test:", ex.args, type(ex))
                    max_pkt_size = i
                    break
            global pkt_last_id_snd
            pkt_last_id_snd = i
            print("sent", i/pkt_incr_sz , "pubs at", pkt_incr_sz, "bytes increament")
            print("total time (pub): ", time.ticks_ms() - t0)
            max_pkt_size = i
            break
        print("max payload size sent (byte): ", max_pkt_size)
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
