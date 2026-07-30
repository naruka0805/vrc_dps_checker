"""同じVRChatインスタンスにいる仲間同士でDPS状況を共有するための、最小構成のバックエンド。

- 各クライアントは自分の instance_id (ワールド+インスタンス識別子。ログから自動抽出) と
  player_name・dps・total_damage を数秒おきに POST /report で送る
- 各クライアントは GET /room/{instance_id} で、同じインスタンスにいる全員の最新値を取得する
- データはメモリ上に保持するだけ（DB不要）。一定時間更新が無いメンバーは自動的に消える

起動方法:
    python server.py
    (デフォルトで http://0.0.0.0:8000 で待ち受ける)
"""

import time

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

STALE_SECONDS = 20  # この秒数更新が無いメンバーは退出したとみなして一覧から消す

app = FastAPI()

# instance_id -> {player_name: {"dps":.., "total_damage":.., "last_seen":..}}
rooms: dict[str, dict[str, dict]] = {}


class Report(BaseModel):
    instance_id: str
    player_name: str
    dps: float
    total_damage: int
    official_boss_damage: int = 0
    class_name: str = ""


@app.post("/report")
def report(r: Report):
    room = rooms.setdefault(r.instance_id, {})
    existing = room.get(r.player_name)
    # 既にいるメンバーなら、DPSの上下に関係なく最初に参加した時刻を保持し続ける
    # （クライアントの「No順」表示を、DPS変動で毎回入れ替わらない安定した順序にするため）
    first_seen = existing["first_seen"] if existing else time.time()
    room[r.player_name] = {
        "dps": r.dps,
        "total_damage": r.total_damage,
        "official_boss_damage": r.official_boss_damage,
        "class_name": r.class_name,
        "last_seen": time.time(),
        "first_seen": first_seen,
    }
    return {"ok": True}


@app.get("/room/{instance_id}")
def get_room(instance_id: str):
    room = rooms.get(instance_id, {})
    now = time.time()
    active = {name: data for name, data in room.items() if now - data["last_seen"] <= STALE_SECONDS}
    rooms[instance_id] = active

    members = sorted(
        ({"player_name": name, **data} for name, data in active.items()),
        key=lambda m: m["first_seen"],
    )
    for m in members:
        del m["last_seen"]
        del m["first_seen"]
    return {"members": members}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
