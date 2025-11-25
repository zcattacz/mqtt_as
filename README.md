# mqtt_as

This micropython MQTT client is a fork of the asynchronous [micropython-mqtt/mqtt_as.py](https://github.com/peterhinch/micropython-mqtt) lib. A stripped-down version tested on both micropython ESP32, ESP32-S2, unix port and CPython3. Mostly adapted for 

1. Drops wifi and connectivity management
2. Works on both cpython and micropython

Why strip out the Network feature ? There're scenarios where you want to manage network yourself:

- With network that is not WiFi based, e.g. Ethernet/GSM/ppp etc
- In a non-trivial project that has other parts relying on WiFi radio or UDP etc, that shouldn't be interrupted.
- In a portable script that needs to run on Linux(RaspberryPi, OrangePi etc), Windows etc, where network is managed by system.

In short, whenever the network device can't or shouldn't be directly manipulated by a mqtt client lib.

## Difference

This version reports down on network read/write error and stops working.
User needs to recover link and try `connect()`, then wait for `up`/`down` event or check internal flag `_has_connected`==True.

This version targets long up-time mqtt client, which keeps running listening for command or reporting data.
User should create his own async connectivity task to check and recover link (NI/IP/DNS/MQTT). If can't be bothered, simply reset wifi and wait for wifiup works in most cases.

The following public interfaces are the same as original `mqtt_as`, 
everything else is either removed or dimmed as internal:

## Methods

`connect(clean:bool=True)` / `disconnect()` -> awaitable
- `disconnect()` is unnecessary before *re-* connect, socket is closed on `connect`.

`subscribe(topic:str, qos=0)` / `unsubscribe(topic:str)` -> awaitable

`publish(topic:str, msg:bytes, retain=False, qos=0, oneshot=False)` -> awaitable
- msg must be bytes/bytearray, otherwise error on cpython
- oneshot: construct full pkt in buffer, prone to OOM under constraint RAM, used for performance with bulk data.

`broker_up()` -> bool / `_has_connected`:bool
- `broker_up()` blocks for a few seconds, to actively wait for underlying ping response. Inherited from original `mqtt_as`.
- Use `_has_connected` for immediate check on most recent status.

## Events

`up` / `down` / `queue`
- up: on connect
- down: on disconnect
- queue: async message queue

## Callback Style

`set_cb_on_event(event:str, cb:awaitable, udata=None, new_task=False)`
- Provide callbacks for above events (`up`, `down`, `msg` for message).
- `udata` is like `user_data` in paho_mqtt.

## Basic Example

    from mqtt_as import MQTT_base, config
    # Use callbacks,
    # from mqtt_as_cb import MQTT_base, config

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

        # event style
        async for tpc, msg, retained in client.queue:
            print(tpc, msg, retained)
        await client.publish("new_topic", b"hello world to event")

        # Use callbacks,
        # async def cb_on_message(tpc, msg, retained):
        #    print(tpc, msg, retained)
        # client.set_cb_on_event("msg", cb_on_message)
        # await client.publish("new_topic", b"hello world to callback")

        await asyncio.sleep(1)
        await client.disconnect()
    
    try:
        asyncio.run(main())
    finally:
        asyncio.new_event_loop()

## Notes

### cpython
- `micropython.py` is the shim required to work in CPython.
- `MQTT_base` initializes async lock/queue/event on instance creation. 
It must be created from the same loop that awaits other `mqtt_as` coros (connect/sub/unsub etc).
This might lead to frustration if unnoticed.

### Older micropython
- socket.connect() as a synchronous call, in some older version upy, sometimes locks up for minutes. This lead to issues in my application.
- `patch_poll_socket` is the work around with select.poll() to avoid that on 1.20 or sth.

## Credit

All credit goes to the original author @peterhinch. Thanks for always answering my questions.
