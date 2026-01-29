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


BASE_DIR = Path(__file__).resolve().parents[1]  # 프로젝트 루트

@dataclass
class LockState:
    active: bool = False
    owner: Optional[str] = None
    proposal_id: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class RoomState:
    room_code: str
    version: int = 0
    phase: str = "PLAYER"
    round: int = 1
    # player_id -> ready bool
    ready: Dict[str, bool] = field(default_factory=dict)
    lock: LockState = field(default_factory=LockState)
    # player_id -> pending proposal_id (for UI)
    pending: Dict[str, Optional[str]] = field(default_factory=dict)
    # room-level mutex for serializing "state-changing" operations
    mutex: asyncio.Lock = field(default_factory=asyncio.Lock)


class Room:
    def __init__(self, code: str):
        self.state = RoomState(room_code=code)
        self.connections: Dict[str, WebSocket] = {}  # player_id -> socket

    def players(self):
        return list(self.connections.keys())

    def public_view(self):
        return {
            "roomCode": self.state.room_code,
            "version": self.state.version,
            "phase": self.state.phase,
            "round": self.state.round,
            "players": [{"id": pid, "ready": self.state.ready.get(pid, False)} for pid in self.players()],
            "ready": dict(self.state.ready),
            "lock": {
                "active": self.state.lock.active,
                "owner": self.state.lock.owner,
                "proposalId": self.state.lock.proposal_id,
                "reason": self.state.lock.reason,
            },
        }

    def snapshot_for(self, player_id: str):
        # 협동/지인용 MVP라서 상대 손패 숨김 같은 건 아직 안 함.
        # "나에게 필요한 정보"만 pending 포함해서 준다.
        base = self.public_view()
        base["you"] = {
            "id": player_id,
            "pendingChoice": self.state.pending.get(player_id),
        }
        return base


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