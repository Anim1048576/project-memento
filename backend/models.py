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