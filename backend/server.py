# server.py
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
from backend.transport import *


BASE_DIR = Path(__file__).resolve().parents[1]  # 프로젝트 루트


app = FastAPI()

# 지인용 로컬 개발 편의(CORS). 배포할 때는 tighten 권장.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

rooms: Dict[str, Room] = {}


def get_or_create_room(code: str) -> Room:
    if code not in rooms:
        rooms[code] = Room(code)
    return rooms[code]


def new_player_id() -> str:
    return "P_" + secrets.token_hex(3)  # 6 hex chars


def new_proposal_id() -> str:
    return "p_" + secrets.token_hex(4)


@app.get("/")
async def root():
    # 편의상 서버가 index.html을 그냥 제공하게도 할 수 있음.
    # 여기선 최소한 안내만.
    return HTMLResponse("<h3>Server is running. Open /client for the test UI.</h3>")


@app.get("/client")
def client():
    return FileResponse(BASE_DIR / "resource" / "index.html")


@app.websocket("/ws/{room_code}")
async def ws_endpoint(ws: WebSocket, room_code: str):
    await ws.accept()
    room = get_or_create_room(room_code)
    player_id = new_player_id()

    # register
    room.connections[player_id] = ws
    room.state.ready[player_id] = False
    room.state.pending[player_id] = None

    # announce join + snapshot
    await send_json(ws, {"type": "WELCOME", "playerId": player_id, "roomCode": room_code})
    await broadcast_room(room, {"type": "INFO", "msg": f"{player_id} joined"})
    await broadcast_snapshots(room)

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")
            seq = msg.get("seq")

            # ---- RULE: global lock while pendingChoice exists ----
            # allow only: PROPOSE (if no lock), COMMIT/CANCEL (only owner), READY (allow? -> MVP에선 lock 중 READY도 막음)
            async with room.state.mutex:
                # quick helpers
                def reject(reason: str):
                    return {"type": "REJECT", "seq": seq, "reason": reason, "version": room.state.version}

                # LOCK ACTIVE
                if room.state.lock.active:
                    if mtype in ("COMMIT", "CANCEL") and room.state.lock.owner == player_id:
                        pass
                    else:
                        await send_json(ws, reject("LOCKED"))
                        continue

                if mtype == "PROPOSE":
                    # lock must be free at this point
                    proposal_id = new_proposal_id()
                    room.state.lock.active = True
                    room.state.lock.owner = player_id
                    room.state.lock.proposal_id = proposal_id
                    room.state.lock.reason = "PENDING_CONFIRM"

                    room.state.pending[player_id] = proposal_id

                    # any action resets readies (안전하게)
                    for pid in room.state.ready.keys():
                        room.state.ready[pid] = False

                    room.state.version += 1
                    await broadcast_room(room, {
                        "type": "PENDING",
                        "seq": seq,
                        "owner": player_id,
                        "proposalId": proposal_id,
                        "prompt": msg.get("prompt", "Confirm this action?"),
                        "version": room.state.version,
                    })
                    await broadcast_snapshots(room)

                elif mtype == "COMMIT":
                    if room.state.lock.owner != player_id:
                        await send_json(ws, reject("NOT_OWNER"))
                        continue

                    # 여기서부터가 나중에 "카드 효과 적용" 자리
                    room.state.lock.active = False
                    room.state.lock.owner = None
                    room.state.lock.proposal_id = None
                    room.state.lock.reason = None
                    room.state.pending[player_id] = None

                    room.state.version += 1
                    await broadcast_room(room, {"type": "COMMITTED", "seq": seq, "by": player_id, "version": room.state.version})
                    await broadcast_snapshots(room)

                elif mtype == "CANCEL":
                    if room.state.lock.owner != player_id:
                        await send_json(ws, reject("NOT_OWNER"))
                        continue

                    room.state.lock.active = False
                    room.state.lock.owner = None
                    room.state.lock.proposal_id = None
                    room.state.lock.reason = None
                    room.state.pending[player_id] = None

                    room.state.version += 1
                    await broadcast_room(room, {"type": "CANCELED", "seq": seq, "by": player_id, "version": room.state.version})
                    await broadcast_snapshots(room)

                elif mtype == "READY":
                    # lock이 없는 상태에서만 READY 가능(위에서 LOCKED 처리됨)
                    ready = bool(msg.get("ready", True))
                    room.state.ready[player_id] = ready

                    room.state.version += 1
                    await broadcast_snapshots(room)

                    # all ready? -> enemy phase mock
                    if room.state.ready and all(room.state.ready.values()):
                        room.state.phase = "ENEMY"
                        room.state.version += 1
                        await broadcast_room(room, {"type": "ENEMY_PHASE", "msg": "Enemy acts (mock)", "version": room.state.version})

                        # back to next round
                        room.state.round += 1
                        room.state.phase = "PLAYER"
                        # reset ready
                        for pid in room.state.ready.keys():
                            room.state.ready[pid] = False
                        room.state.version += 1
                        await broadcast_snapshots(room)

                else:
                    await send_json(ws, reject("UNKNOWN_TYPE"))

    except WebSocketDisconnect:
        pass
    finally:
        # cleanup
        room.connections.pop(player_id, None)
        room.state.ready.pop(player_id, None)
        room.state.pending.pop(player_id, None)

        await broadcast_room(room, {"type": "INFO", "msg": f"{player_id} left"})
        await broadcast_snapshots(room)

        # room empty -> delete
        if not room.connections:
            rooms.pop(room_code, None)