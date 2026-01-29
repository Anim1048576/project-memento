import asyncio
import json
import secrets
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import FileResponse
from pathlib import Path

from backend.room import Room


async def send_json(ws: WebSocket, payload: dict):
    await ws.send_text(json.dumps(payload, ensure_ascii=False))


async def broadcast_room(room: Room, payload: dict):
    dead: Set[str] = set()
    for pid, ws in room.connections.items():
        try:
            await send_json(ws, payload)
        except Exception:
            dead.add(pid)
    for pid in dead:
        room.connections.pop(pid, None)
        room.state.ready.pop(pid, None)
        room.state.pending.pop(pid, None)


async def broadcast_snapshots(room: Room):
    # 각 플레이어별로 개인화 스냅샷 전송
    for pid, ws in list(room.connections.items()):
        await send_json(ws, {
            "type": "STATE_SNAPSHOT",
            "gameId": room.state.room_code,
            "state": room.snapshot_for(pid),
        })