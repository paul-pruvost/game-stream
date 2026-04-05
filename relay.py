#!/usr/bin/env python3
"""
GameStream Internet Relay Server — dual-mode.

Port TCP 9950 : binary relay for host.py <-> client.py
Port HTTP 9951: HTTP + WebSocket relay for gateway.py <-> mobile phone

Usage:
    python relay.py [--port 9950] [--http-port 9951]

host.py / client.py connect with:
    python host.py   --relay relay_server:9950 --room XXXX
    python client.py --relay relay_server:9950 --room XXXX

gateway.py connects with:
    python gateway.py --relay relay_server:9951 --room XXXX

Mobile phone opens:
    http://relay_server:9951/{room}/?token=XXXX

TCP Protocol:
    1. Connect to relay TCP
    2. Send one-line JSON header:
       {"room": "XXXX", "role": "host|client", "channel": "control|video|audio"}\\n
    3. Once host + client are both connected for the same room+channel,
       the relay forwards all data bidirectionally.
    4. Data framing: [4B big-endian length][data]

HTTP/WS Protocol:
    GET  /{room}/              -> serves index.html with RELAY_ROOM injected
    GET  /static/{file}        -> serves mobile/static/ files
    WS   /uplink/{room}?token= -> gateway.py uplink (outbound)
    WS   /ws/{room}?token=     -> phone client, bridged to uplink
"""

import argparse
import asyncio
import json
import os
import secrets
import struct
import sys
from typing import Dict, Tuple, Optional

try:
    from aiohttp import web
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

# ── TCP binary relay state ────────────────────────────────────────────────────

# rooms[room_id][channel] = {"host": (reader, writer), "client": (reader, writer)}
rooms: Dict[str, Dict[str, Dict[str, Tuple]]] = {}
rooms_lock: asyncio.Lock = None  # initialized in main

CHANNELS = {"control", "video", "audio"}
ROLES = {"host", "client"}
FRAME_HEADER = struct.Struct("!I")

# ── HTTP/WS relay state ───────────────────────────────────────────────────────

# uplinks[room_id] = {"ws": aiohttp.WebSocketResponse, "token": str}
uplinks: Dict[str, Dict] = {}
uplinks_lock: asyncio.Lock = None  # initialized in main

# Path to mobile/static relative to this file
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "mobile", "static")
_INDEX_HTML  = os.path.join(_STATIC_DIR, "index.html")


# ══════════════════════════════════════════════════════════════════════════════
#  TCP binary relay (existing logic — unchanged)
# ══════════════════════════════════════════════════════════════════════════════

async def read_framed(reader: asyncio.StreamReader) -> Optional[bytes]:
    """Read one length-prefixed frame. Returns None on EOF/error."""
    try:
        header = await reader.readexactly(4)
    except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
        return None
    (length,) = FRAME_HEADER.unpack(header)
    if length == 0:
        return b""
    if length > 16 * 1024 * 1024:  # 16 MB sanity limit
        return None
    try:
        data = await reader.readexactly(length)
        return data
    except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
        return None


async def write_framed(writer: asyncio.StreamWriter, data: bytes):
    """Write one length-prefixed frame."""
    try:
        writer.write(FRAME_HEADER.pack(len(data)) + data)
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass


async def forward(
    src_reader: asyncio.StreamReader,
    dst_writer: asyncio.StreamWriter,
    label: str,
):
    """Forward frames from src to dst until EOF."""
    while True:
        data = await read_framed(src_reader)
        if data is None:
            break
        await write_framed(dst_writer, data)
    try:
        dst_writer.close()
        await dst_writer.wait_closed()
    except Exception:
        pass


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername", ("?", 0))
    peer_str = f"{peer[0]}:{peer[1]}"
    room_id = None
    role = None
    channel = None

    try:
        # Read the JSON header line (up to 1KB)
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
        except asyncio.TimeoutError:
            print(f"  [relay] {peer_str} timeout reading header")
            writer.close()
            return

        line = line.strip()
        if not line:
            writer.close()
            return

        try:
            header = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            print(f"  [relay] {peer_str} bad JSON header: {e}")
            writer.close()
            return

        room_id = str(header.get("room", "")).strip().upper()
        role = str(header.get("role", "")).lower()
        channel = str(header.get("channel", "control")).lower()

        if not room_id:
            print(f"  [relay] {peer_str} missing room")
            writer.close()
            return
        if role not in ROLES:
            print(f"  [relay] {peer_str} invalid role: {role!r}")
            writer.close()
            return
        if channel not in CHANNELS:
            print(f"  [relay] {peer_str} invalid channel: {channel!r}")
            writer.close()
            return

        print(f"  [relay] {peer_str} → room={room_id} role={role} channel={channel}")

        async with rooms_lock:
            if room_id not in rooms:
                rooms[room_id] = {}
            room = rooms[room_id]
            if channel not in room:
                room[channel] = {}
            ch = room[channel]

            if role in ch:
                # Reconnection: replace the stale connection
                old_reader, old_writer = ch[role]
                print(f"  [relay] {peer_str} replacing stale {role} for room={room_id}/{channel}")
                try:
                    old_writer.close()
                except Exception:
                    pass

            ch[role] = (reader, writer)

            # If a host connects and there's no room code printed yet, print it
            if role == "host" and channel == "control":
                print(f"\n  [relay] Room {room_id} created — share this with client:\n"
                      f"          --relay <this_server>:<port> --room {room_id}\n")

            # Check if both sides are now present
            if "host" in ch and "client" in ch:
                host_reader, host_writer = ch["host"]
                client_reader, client_writer = ch["client"]
            else:
                # Wait for the other side
                pass

        # Wait until both sides present (poll with lock)
        # Only the "client" task will do the actual forwarding;
        # the "host" task waits on a done_event so there's only one forwarder.
        done_event = None
        for _ in range(600):  # up to 60s
            await asyncio.sleep(0.1)
            async with rooms_lock:
                ch = rooms.get(room_id, {}).get(channel, {})
                # If we've been replaced by a newer connection, exit quietly
                if ch.get(role) != (reader, writer):
                    print(f"  [relay] {peer_str} replaced — old {role} task exiting (room={room_id}/{channel})")
                    return
                if "host" in ch and "client" in ch:
                    host_reader, host_writer = ch["host"]
                    client_reader, client_writer = ch["client"]
                    # Create a done_event if not already present
                    if "_done" not in ch:
                        ch["_done"] = asyncio.Event()
                    done_event = ch["_done"]
                    break
        else:
            print(f"  [relay] {peer_str} room={room_id}/{channel} timed out waiting for peer")
            async with rooms_lock:
                ch = rooms.get(room_id, {}).get(channel, {})
                ch.pop(role, None)
            writer.close()
            return

        # Final check: make sure we haven't been replaced between the last poll and here
        async with rooms_lock:
            ch = rooms.get(room_id, {}).get(channel, {})
            if ch.get(role) != (reader, writer):
                print(f"  [relay] {peer_str} replaced — old {role} task exiting before forward (room={room_id}/{channel})")
                return

        # Only the "client" task does forwarding; the "host" task waits.
        # This prevents two tasks from competing on the same readers/writers.
        if role == "host":
            print(f"  [relay] room={room_id}/{channel} paired — host waiting for forwarder")
            # Wait until forwarding is done (client task sets the event
            # and also sends the pairing signal to both sides)
            await done_event.wait()
            return

        # role == "client" — this task does the actual forwarding
        print(f"  [relay] room={room_id}/{channel} paired — forwarding")

        # Notify both sides they are paired (zero-length frame as signal)
        try:
            host_writer.write(FRAME_HEADER.pack(0))
            client_writer.write(FRAME_HEADER.pack(0))
            await host_writer.drain()
            await client_writer.drain()
        except Exception:
            pass

        # Forward bidirectionally
        try:
            await asyncio.gather(
                forward(host_reader, client_writer, f"room={room_id}/{channel} host→client"),
                forward(client_reader, host_writer, f"room={room_id}/{channel} client→host"),
                return_exceptions=True,
            )
        finally:
            # Signal the host task that forwarding is done
            if done_event:
                done_event.set()
            # Clean up the done event
            async with rooms_lock:
                ch = rooms.get(room_id, {}).get(channel, {})
                ch.pop("_done", None)

    except Exception as e:
        print(f"  [relay] {peer_str} error: {e}")
    finally:
        # Clean up room entry so reconnections are not rejected as duplicates
        if room_id and role and channel:
            try:
                async with rooms_lock:
                    ch = rooms.get(room_id, {}).get(channel, {})
                    stored = ch.get(role)
                    if stored and stored == (reader, writer):
                        del ch[role]
                    # Prune empty structures
                    if room_id in rooms:
                        if channel in rooms[room_id] and not rooms[room_id][channel]:
                            del rooms[room_id][channel]
                        if not rooms[room_id]:
                            del rooms[room_id]
                print(f"  [relay] {peer_str} disconnected (room={room_id}/{channel}/{role})")
            except Exception:
                pass
        try:
            writer.close()
        except Exception:
            pass


def generate_room_code() -> str:
    """Generate a 4-char hex room code (suitable for manual entry)."""
    return secrets.token_hex(2).upper()


async def run_relay(host: str, port: int):
    global rooms_lock
    rooms_lock = asyncio.Lock()

    server = await asyncio.start_server(handle_connection, host, port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)

    print(f"  [relay/tcp]  Listening on {addrs}")

    async with server:
        await server.serve_forever()


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP/WS relay for gateway.py <-> phone (port 9951)
# ══════════════════════════════════════════════════════════════════════════════

async def _http_index(request: "web.Request") -> "web.Response":
    """
    GET /{room}/  — serve index.html with RELAY_ROOM injected into <head>.
    """
    room_id = request.match_info.get("room", "").strip().upper()
    if not os.path.isfile(_INDEX_HTML):
        return web.Response(status=404, text="index.html not found")

    with open(_INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    inject = f'<script>const RELAY_ROOM="{room_id}";</script>'
    html = html.replace("</head>", inject + "</head>", 1)
    return web.Response(content_type="text/html", text=html)


async def _http_static(request: "web.Request") -> "web.Response":
    """GET /static/{filename}"""
    filename = request.match_info.get("filename", "")
    path = os.path.join(_STATIC_DIR, filename)
    if not os.path.isfile(path) or not os.path.abspath(path).startswith(
        os.path.abspath(_STATIC_DIR)
    ):
        return web.Response(status=404, text="Not found")
    return web.FileResponse(path)


async def _ws_uplink(request: "web.Request") -> "web.WebSocketResponse":
    """
    WS /uplink/{room}?token={token}
    gateway.py connects here; registers itself as the uplink for the room.

    This handler is the ONLY reader of the uplink websocket. It forwards
    all incoming messages (video frames, config, etc.) to every connected
    phone in the room. Phones are registered/unregistered by _ws_phone.
    """
    room_id = request.match_info.get("room", "").strip().upper()
    token   = request.rel_url.query.get("token", "")

    if not room_id:
        raise web.HTTPBadRequest(reason="Missing room")

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    print(f"  [relay/http] gateway connected: room={room_id}")

    async with uplinks_lock:
        uplinks[room_id] = {"ws": ws, "token": token, "phones": set()}

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                # Forward video/audio binary to all phones
                async with uplinks_lock:
                    phones = set(uplinks.get(room_id, {}).get("phones", set()))
                for phone_ws in phones:
                    try:
                        await phone_ws.send_bytes(msg.data)
                    except Exception:
                        pass
            elif msg.type == aiohttp.WSMsgType.TEXT:
                # Forward config/events to all phones
                async with uplinks_lock:
                    phones = set(uplinks.get(room_id, {}).get("phones", set()))
                for phone_ws in phones:
                    try:
                        await phone_ws.send_str(msg.data)
                    except Exception:
                        pass
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
    finally:
        async with uplinks_lock:
            if uplinks.get(room_id, {}).get("ws") is ws:
                del uplinks[room_id]
        print(f"  [relay/http] gateway disconnected: room={room_id}")

    return ws


async def _ws_phone(request: "web.Request") -> "web.WebSocketResponse":
    """
    WS /ws/{room}?token={token}
    Phone connects here. It sends input events to the gateway uplink.
    Video/audio from the gateway is pushed by _ws_uplink (hub model).
    """
    room_id = request.match_info.get("room", "").strip().upper()
    token   = request.rel_url.query.get("token", "")

    if not room_id:
        raise web.HTTPBadRequest(reason="Missing room")

    # Check uplink is registered
    async with uplinks_lock:
        entry = uplinks.get(room_id)

    if entry is None:
        return web.Response(
            status=503,
            content_type="application/json",
            text=json.dumps({"error": "gateway not connected", "room": room_id}),
        )

    # Validate token against the uplink token
    if entry["token"] and token != entry["token"]:
        raise web.HTTPForbidden(reason="Invalid token")

    uplink_ws: "web.WebSocketResponse" = entry["ws"]

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    client_ip = request.remote
    print(f"  [relay/http] phone connected: room={room_id} client={client_ip}")

    # Register this phone so _ws_uplink forwards to it
    async with uplinks_lock:
        entry = uplinks.get(room_id)
        if entry:
            entry["phones"].add(ws)

    try:
        # Only direction here: phone → gateway (input events)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    await uplink_ws.send_str(msg.data)
                except Exception:
                    break
            elif msg.type == aiohttp.WSMsgType.BINARY:
                try:
                    await uplink_ws.send_bytes(msg.data)
                except Exception:
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
    finally:
        async with uplinks_lock:
            entry = uplinks.get(room_id)
            if entry:
                entry["phones"].discard(ws)
        print(f"  [relay/http] phone disconnected: room={room_id} client={client_ip}")

    return ws


async def run_http_relay(host: str, port: int):
    global uplinks_lock
    if uplinks_lock is None:
        uplinks_lock = asyncio.Lock()

    if not HAS_AIOHTTP:
        print("  [relay/http] aiohttp not installed — HTTP relay disabled")
        return

    app = web.Application()
    app.router.add_get("/uplink/{room}", _ws_uplink)
    app.router.add_get("/ws/{room}",     _ws_phone)
    app.router.add_get("/static/{filename}", _http_static)
    app.router.add_get("/{room}/",       _http_index)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    print(f"  [relay/http] Listening on {host}:{port}")

    try:
        # Keep running until cancelled
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

async def _run_all(bind_host: str, tcp_port: int, http_port: int):
    global rooms_lock, uplinks_lock
    rooms_lock   = asyncio.Lock()
    uplinks_lock = asyncio.Lock()

    # Suppress the "socket.send() raised exception." spam that the Windows
    # ProactorEventLoop emits when it tries to write to an already-closed
    # client socket (normal during relay client disconnection).
    def _exception_handler(loop, context):
        msg = context.get("message", "")
        if "socket.send() raised exception" in msg:
            return
        loop.default_exception_handler(context)
    asyncio.get_event_loop().set_exception_handler(_exception_handler)

    tcp_server = await asyncio.start_server(handle_connection, bind_host, tcp_port)
    tcp_addrs  = ", ".join(str(s.getsockname()) for s in tcp_server.sockets)

    print()
    print(f"╔══════════════════════════════════════════════════════╗")
    print(f"║       GameStream Relay Server (dual-mode)           ║")
    print(f"╠══════════════════════════════════════════════════════╣")
    print(f"║  TCP  (host/client) : {tcp_addrs:<31}║")
    print(f"║  HTTP (gateway/web) : {bind_host}:{http_port:<27}║")
    print(f"║  Protocol           : TCP frames + HTTP/WS          ║")
    print(f"╚══════════════════════════════════════════════════════╝")
    print(f"\n  host.py   : --relay <ip>:{tcp_port} --room XXXX")
    print(f"  client.py : --relay <ip>:{tcp_port} --room XXXX")
    print(f"  gateway.py: --relay <ip>:{http_port} --room XXXX")
    print(f"\n  Waiting for connections...\n")

    if HAS_AIOHTTP:
        http_task = asyncio.create_task(run_http_relay(bind_host, http_port))
    else:
        print("  [relay/http] WARNING: aiohttp not installed — HTTP relay disabled")
        http_task = None

    try:
        async with tcp_server:
            await tcp_server.serve_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if http_task:
            http_task.cancel()
            try:
                await http_task
            except asyncio.CancelledError:
                pass


def main():
    p = argparse.ArgumentParser(description="GameStream Internet Relay Server")
    p.add_argument("--port",      type=int, default=9950,  help="TCP port for host/client relay")
    p.add_argument("--http-port", type=int, default=9951,  help="HTTP/WS port for gateway/phone relay")
    p.add_argument("--host",      type=str, default="0.0.0.0", help="Bind address")
    args = p.parse_args()

    try:
        asyncio.run(_run_all(args.host, args.port, args.http_port))
    except KeyboardInterrupt:
        print("\n[relay] Shutting down.")


if __name__ == "__main__":
    main()
