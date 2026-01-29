import json
from dataclasses import dataclass, field
from typing import Set

from fastapi import WebSocket

from backend.room import Room


async def send_json(ws: WebSocket, payload: dict):
    await ws.send_text(json.dumps(payload, ensure_ascii=False))


async def broadcast_room(room: Room, payload: dict):
    dead: Set[str] = set()

    # Iterate over a snapshot of current connections (we may prune while broadcasting)
    for pid, ws in list(room.connections.items()):
        try:
            await send_json(ws, payload)
        except Exception:
            dead.add(pid)

    for pid in dead:
        room.connections.pop(pid, None)
        room.state.ready.pop(pid, None)
        room.state.pending.pop(pid, None)

    # If the proposal owner disconnected mid-lock, auto-cancel to avoid a deadlocked room.
    if room.state.lock.active and room.state.lock.owner in dead:
        room.state.lock.active = False
        room.state.lock.owner = None
        room.state.lock.proposal_id = None
        room.state.lock.reason = None
        room.state.version += 1

        # Notify remaining players and refresh personalized snapshots.
        for _pid, _ws in list(room.connections.items()):
            await send_json(_ws, {
                "type": "CANCELED",
                "by": "SERVER",
                "reason": "OWNER_DISCONNECTED",
                "version": room.state.version,
            })
        await broadcast_snapshots(room)


async def broadcast_snapshots(room: Room):
    # 각 플레이어별로 개인화 스냅샷 전송
    for pid, ws in list(room.connections.items()):
        await send_json(ws, {
            "type": "STATE_SNAPSHOT",
            "gameId": room.state.room_code,
            "state": room.snapshot_for(pid),
        })