from typing import Dict

from fastapi import WebSocket

import backend.models as models


class Room:
    def __init__(self, code: str):
        self.state = models.RoomState(room_code=code)
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