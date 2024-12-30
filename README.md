# mqtt_as

This micropython MQTT client is a fork of the asynchronous [micropython-mqtt/mqtt_as.py](https://github.com/peterhinch/micropython-mqtt) lib. A stripped-down version tested on both micropython ESP32, ESP32-S2, unix port and CPython3. Mostly adapted for 

1. Drop wifi and connectivity management
2. Runnability on both cpython and micropython

## Difference

This version reports down on network read,write error and stops working. 
User needs to recover link and try `connect()`, then wait for `up`/`down` event or check internal flag `_has_connected`==True.

This version targets long up-time mqtt client, which keeps running listening for command or reporting data. Since e.g. with an occasionally roaming network setup or in poor connectivity environment, recovering connectivty with minium downtime is non-trival and worth a task of its own. User should create his own async connectivity task to check and recover link (WiFi/IP/DNS/MQTT). If can't be bothered, simply reset wifi and wait for wifiup works in most cases.

The following public interfaces are the same as original `mqtt_as`, 
everything else is either removed or dimmed as internal:

## Methods

`connect(clean:bool=True)` / `disconnect()` -> awaitable
- `disconnect()` is unnecessary before *re-* connect, socket is closed on `connect`.

`subscribe(topic:str, qos=0)` / `unsubscribe(topic:str)` -> awaitable

`publish(topic:str, msg:bytes, retain=False, qos=0, oneshot=False)` -> awaitable
- msg must be bytes/bytearray, otherwise error on cpython
- oneshot: construct full pkt in buffer, prone to OOM under constraint RAM, mainly used in cpython for performance.

`broker_up()` / `_has_connected`
- `broker_up()` blocks for a few seconds, to actively wait for underlaying ping response. Inherited from orignal `mqtt_as`.
- Use `_has_connected` for immediate check on most recent status.

## Events

`up` / `down` / `queue`
- up: on connect
- down: on disconnect
- queue: async message queue

## Callbacks

`set_cb_on_event(event:str, cb:awaitable, udata=None, new_task=False)`
- Create loops for above events (`msg` for message queue) to call cbs.
- udata is like user_data in paho_mqtt.
- Do not register same cb twice, no check for duplicated cb.
- Neither unregistering cbs nor stopping the loops is supported.

## Basic Example

    from mqtt_as import MQTT_base, config

    async def cb_on_message(tpc, msg, retained):
        print(tpc, msg, retained)

    # override defaults
    config['server'] = 'test.mosquitto.org'  # Change to suit
    config["client_id"] = "client202311abc"

    async def main():
        client = MQTT_base(config)
        #client.DEBUG = True
        await client.connect()
        await client.up.wait()
        client.up.clear()
        print("mqtt connected")
        await client.subscribe("new_topic")
        client.set_cb_on_event("msg", cb_on_message)
        await client.publish("new_topic", b"hello world")
        await asyncio.sleep(1)
        await client.disconnect()
    
    try:
        asyncio.run(main())
    finally:
        asyncio.new_event_loop()

## On CPython

- `micropython.py` is a shim for mqtt_as.py to work in CPython.
- `MQTT_base` initializes async lock/queue/event on instance creation. 
It must be created from the same loop that awaits other `mqtt_as` coros e.g. 
connect/sub/unsub() etc. This will be frustrating on cpython, if not paying attention to.

## Credit

All credit goes to the original author @peterhinch. Thanks for always answering my questions.
