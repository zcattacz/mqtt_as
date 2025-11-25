from mqtt_as import MQTT_base as _MQTT_base, VERSION, config, MQTTException, asyncio

class MQTT_base(_MQTT_base):
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
