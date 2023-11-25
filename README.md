# mqtt_as.compat
A stripped-down version of mqtt_as without connection management, 
tested on both micropython ESP32, ESP32-S2 and CPython3.

Mostly an adaption of mqtt_as for 
1. without connection management
2. runnability on cpython and micropython
Due to target 2, it's not suitable to run on esp82666 without adaption.

The following public interface is the same as original mqtt_as, 
everything else is either removed or supposed to be internal:

## Methods
connect(clean:bool) / disconnect() -> awaitable

subscribe(topic:str, qos) / unsubscribe(topic:str) -> awaitable

publish(topic:str, msg:bytes, retain=False, qos=0, oneshot=False) -> awaitable
- msg must be bytes/bytearray
- oneshot is for testing bulk transfer on cpython

broker_up() / _has_connected
- broker_up() blocks for a few seconds, for ping response
- the internal flag is for testing of most recent status

## Events
up / down / queue
- up: connected
- down: disconnected
- queue: message queue

## Callbacks
set_cb_on_event(event:str, cb:awfn, new_task=False)
- Create loops for above events to call cbs
- Do not register same cb twice, this does not check for duplicated cb
- Neither unregistering a cb nor stopping the loops is possible yet.
  (rarely useful I suppose)
~~unset_cb_on_event(event:str, cb:awfn)~~
~~clear_cb_on_event(event:str, cb:awfn)~~

## Basic Example
    from mqtt_as import MQTT_base, config
    try:
        import uasyncio as asyncio
    except:
        import micropython
        asyncio = micropython.patch_asyncio()

    async def cb_on_message(tpc, msg, retained):
        print(tpc, msg, retained)

    # override defaults
    config['server'] = 'test.mosquitto.org'  # Change to suit
    config["client_id"] = "client202311abc"

    async def main():
        client = MQTT_base(config)
        client.DEBUG = True
        await client.connect(True)
        await client.up.wait()
        client.up.clear()
        print("mqtt connected")
        await client.subscribe("new_topic", qos=0)
        client.set_cb_on_event("msg", cb_on_message)
        await client.publish("new_topic", b"hello world")
        await asyncio.sleep(1)
        await client.disconnect()
    
    try:
        asyncio.run(main())
    finally:
        asyncio.new_event_loop()


## Versions
mqtt_sr.py is a Stream based version (shares a bit more code between micropython and cpython), worked fine for continuous sending and minor message reception (e.g. a few short rpc cmd). The lock between read/write has been removed, the send throughput improved greatly. It crashes in test when sending+receving both are under load. There is some fundimental issue to solve.

## On CPython
- micropython.py is a shim for cpython lacks time.ticks_ms() etc.
- MQTT_base initializes async lock/queue/event on instance creation. 
It must be created from the same loop that awaits other mqtt_as coros e.g. 
connect/sub/unsub() etc. This will be frustrating on cpython, if not paying attention to.

## Credit
All credit goes to the original author @peterhinch. Thanks for always answering my questions.
