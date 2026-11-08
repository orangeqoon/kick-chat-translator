"""Kick website realtime receiver. Guest credentials stay in memory only."""
from __future__ import annotations
import asyncio
import json
import uuid
from urllib.parse import urlsplit
import aiohttp
from .pipeline import ChatIn


def valid_socket_url(value):
    try:
        u = urlsplit(value)
        return (u.scheme == 'wss' and bool(u.hostname) and u.hostname.startswith('realtime.')
                and u.hostname.endswith('.platform.kick.com') and u.port in (None, 443)
                and not u.username and not u.password and u.path == '/connection/websocket'
                and not u.fragment and not u.query)
    except (ValueError, TypeError):
        return False


def packets(text):
    if not isinstance(text, str) or len(text) > 1048576:
        return []
    result = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def chat_message(packet, channel):
    push = packet.get('push')
    if not isinstance(push, dict) or push.get('channel') != channel:
        return None
    data = (push.get('pub') or {}).get('data')
    if not isinstance(data, dict) or data.get('event') != 'App\\Events\\ChatMessageEvent':
        return None
    body = data.get('data')
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    if not isinstance(body, dict) or body.get('type', 'message') != 'message':
        return None
    sender = body.get('sender')
    if not isinstance(sender, dict):
        return None
    mid, content, name = body.get('id'), body.get('content'), sender.get('username')
    if not isinstance(mid, str) or not mid or not isinstance(content, str) or not isinstance(name, str):
        return None
    uid = sender.get('id')
    return ChatIn(mid[:200], uid if type(uid) is int else None, name[:50], content[:2000])


async def _json(session, method, url, **kwargs):
    async with session.request(method, url, timeout=aiohttp.ClientTimeout(total=15),
                               headers={'Origin': 'https://kick.com', 'Referer': 'https://kick.com/'},
                               **kwargs) as response:
        if response.status != 200:
            raise RuntimeError('Kick realtime negotiation failed')
        return await response.json(content_type=None)


async def _receive(ws, stop, timeout):
    read = asyncio.create_task(ws.receive())
    stopped = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait((read, stopped), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if stopped in done:
            return None
        if read not in done:
            raise TimeoutError('Kick realtime response timeout')
        frame = read.result()
        if frame.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError('Kick realtime disconnected')
        return frame.data
    finally:
        for task in (read, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(read, stopped, return_exceptions=True)


async def _connection(session, slug, handle, stop, state):
    channel = await _json(session, 'GET', f'https://kick.com/api/v2/channels/{slug}')
    channel_id, room_id = int(channel['id']), int(channel['chatroom']['id'])
    client_id = str(uuid.uuid4())
    negotiation = await _json(session, 'POST',
        f'https://web.kick.com/api/v1/realtime/channels/{channel_id}/chat/connection',
        json={'client': {'id': client_id, 'type': 'web'},
              'capabilities': {'accepted_providers': [{'provider': 'centrifugo'}]}})
    connections = negotiation['data']['connections']
    url = next(c['credentials']['url'] for c in connections if c.get('provider') == 'centrifugo')
    if not valid_socket_url(url):
        raise RuntimeError('Kick realtime endpoint rejected')
    auth = await _json(session, 'POST', 'https://web.kick.com/api/v1/realtime/auth/connection',
                       json={'client_id': client_id})
    token = auth['data']['token']
    if not isinstance(token, str) or not token:
        raise RuntimeError('Kick realtime guest credential missing')
    if stop.is_set():
        return
    subscription = f'chatrooms.{room_id}.v2'
    async with session.ws_connect(url, origin='https://kick.com', heartbeat=20,
                                  timeout=10, max_msg_size=1048576) as ws:
        await ws.send_json({'id': 1, 'connect': {'token': token, 'name': 'js'}})
        connected = False
        deadline = asyncio.get_running_loop().time() + 20
        renew = asyncio.get_running_loop().time() + 2400
        while not stop.is_set():
            now = asyncio.get_running_loop().time()
            if now >= renew:
                return
            if not connected and now >= deadline:
                raise TimeoutError('Kick realtime subscription timeout')
            frame = await _receive(ws, stop, 35 if connected else max(.1, deadline-now))
            if frame is None:
                return
            for packet in packets(frame):
                if not packet:
                    await ws.send_json({})
                elif packet.get('error') or 'disconnect' in (packet.get('push') or {}):
                    raise ConnectionError('Kick realtime subscription ended')
                elif packet.get('id') == 1 and isinstance(packet.get('connect'), dict):
                    ttl = packet['connect'].get('ttl', 2700)
                    if isinstance(ttl, (int, float)) and ttl > 120:
                        renew = asyncio.get_running_loop().time() + min(ttl-60, 2400)
                    await ws.send_json({'id': 2, 'subscribe': {'channel': subscription}})
                elif packet.get('id') == 2 and 'subscribe' in packet:
                    connected = True
                    state('connected', 'Kickチャットに接続しました')
                elif connected:
                    msg = chat_message(packet, subscription)
                    if msg:
                        state('receiving', 'チャットを受信中')
                        await handle(msg)


async def receive_chat(session, channel_slug, handle, stop_event, on_state=None):
    state = on_state or (lambda *_: None)
    delay = 1
    while not stop_event.is_set():
        state('connecting' if delay == 1 else 'reconnecting', 'Kickチャットへ接続中')
        try:
            await _connection(session, channel_slug, handle, stop_event, state)
            delay = 1
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Never expose provider payloads or transient guest credentials.
            state('reconnecting', f'再接続します ({type(error).__name__})')
            delay = min(delay * 2, 30)
        if not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass