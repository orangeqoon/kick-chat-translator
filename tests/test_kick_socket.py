import asyncio
import json
import pytest
from kicktrans.kick_socket import valid_socket_url, packets, chat_message, _receive, receive_chat
from kicktrans.pipeline import Pipeline, ChatIn
from kicktrans.config import Config
from kicktrans.translator import MockTranslator

@pytest.mark.parametrize('url',[
 'wss://evil.com/connection/websocket',
 'wss://realtime.evil.platform.kick.com.evil.com/connection/websocket',
 'ws://realtime.us.platform.kick.com/connection/websocket',
 'wss://user@realtime.us.platform.kick.com/connection/websocket',
 'wss://realtime.us.platform.kick.com:444/connection/websocket',
 'wss://realtime.us.platform.kick.com/connection/websocket?token=x'])
def test_reject_untrusted_endpoint(url):
    assert not valid_socket_url(url)

def test_endpoint_and_multiline_frames():
    assert valid_socket_url('wss://realtime.us-east-1.platform.kick.com/connection/websocket')
    assert packets('{}\n{"id":2,"subscribe":{}}\nbad\n[]') == [{},{'id':2,'subscribe':{}}]

def publication(channel='chatrooms.42.v2', data=None):
    body=data if data is not None else {'id':'message-1','content':'hello','type':'message','sender':{'id':7,'username':'viewer'}}
    return {'push':{'channel':channel,'pub':{'data':{'event':'App\\Events\\ChatMessageEvent','data':json.dumps(body)}}}}

def test_publication_mapping_and_channel_isolation():
    msg=chat_message(publication(),'chatrooms.42.v2')
    assert (msg.message_id,msg.sender_id,msg.sender_name,msg.content)==('message-1',7,'viewer','hello')
    assert chat_message(publication(),'chatrooms.43.v2') is None
    assert chat_message(publication(data={'id':1}),'chatrooms.42.v2') is None
    packet=publication()
    packet['push']['pub']['data']['event']='other'
    assert chat_message(packet,'chatrooms.42.v2') is None

def test_stop_cancels_pending_receive():
    async def run():
        stop=asyncio.Event()
        class Socket:
            cancelled=False
            async def receive(self):
                try: await asyncio.Event().wait()
                finally: self.cancelled=True
        socket=Socket()
        task=asyncio.create_task(_receive(socket,stop,10))
        await asyncio.sleep(0)
        stop.set()
        assert await asyncio.wait_for(task,.5) is None
        assert socket.cancelled
    asyncio.run(run())

def test_reconnect_is_stop_responsive(monkeypatch):
    import kicktrans.kick_socket as socket
    async def fail(*args): raise ConnectionError('sensitive payload')
    monkeypatch.setattr(socket,'_connection',fail)
    async def run():
        stop=asyncio.Event()
        states=[]
        def state(name,detail):
            states.append((name,detail))
            if name=='reconnecting': stop.set()
        await asyncio.wait_for(receive_chat(None,'channel',None,stop,state),.5)
        assert states[-1][0]=='reconnecting'
        assert all('sensitive' not in detail for _,detail in states)
    asyncio.run(run())

def test_owner_manual_message_and_echo():
    async def run():
        pipe=Pipeline(Config(),MockTranslator())
        result=await pipe.process_detailed(ChatIn('original',42,'owner','こんにちは'))
        assert result.source_lang=='ja' and result.target_lang=='en'
        assert await pipe.process(ChatIn('echo',42,'owner',result.output)) is None
        assert await pipe.process(ChatIn('original',42,'owner','こんにちは')) is None
    asyncio.run(run())