"""同じVRChatインスタンスにいる仲間同士でDPS状況を共有するための、最小構成のバックエンド。

- 各クライアントは自分の instance_id (ワールド+インスタンス識別子。ログから自動抽出) と
  player_name・dps・total_damage を数秒おきに POST /report で送る
- 各クライアントは GET /room/{instance_id} で、同じインスタンスにいる全員の最新値を取得する
- データはメモリ上に保持するだけ（DB不要）。一定時間更新が無いメンバーは自動的に消える


起動方法:
    python server.py
    (デフォルトで http://0.0.0.0:8000 で待ち受ける。PORT環境変数で変更可)
"""

import json
import os
import threading
import time

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

STALE_SECONDS = 20  # この秒数更新が無いメンバーは退出したとみなして一覧から消す

# roomsは無制限に増やせてしまうので、上限を設けてメモリ使用量を有限に固定する
MAX_INSTANCE_ID_LEN = 200
MAX_ROOMS = 500
MAX_MEMBERS_PER_ROOM = 40
SWEEP_INTERVAL_SECONDS = 30

# クライアントは1秒に2リクエストなので既定値は緩め（NATやプロキシ経由で同一IPに見える分の余裕）
RATE_LIMIT_WINDOW_SECONDS = 10
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("PARTY_RATE_LIMIT", "300"))
MAX_RATE_LIMIT_ENTRIES = 10_000

# 正規のreportは日本語名をエスケープしても1KB弱。4KBあれば十分な余裕がある
MAX_REQUEST_BODY_BYTES = 4096

# APIの仕様書を不特定多数に配る必要はないので /docs /redoc /openapi.json は閉じる
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class LimitBodySize:
    """ボディを読む前にサイズで弾くASGIミドルウェア。

    FastAPIはボディを読み終えてから依存関係を解決するので、レート制限は巨大な
    ボディに対して無力になる。Content-Lengthを必須にして、chunked転送で
    上限を迂回されるのも防ぐ。
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH"):
            length = dict(scope.get("headers") or []).get(b"content-length")
            if length is None or not length.isdigit() or int(length) > self.max_bytes:
                await send({
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": b'{"detail":"payload too large"}'})
                return
        await self.app(scope, receive, send)




app.add_middleware(LimitBodySize, max_bytes=MAX_REQUEST_BODY_BYTES)


@app.exception_handler(RequestValidationError)
def on_validation_error(request: Request, exc: RequestValidationError):
    """不正だったフィールド名だけ返す。既定ハンドラは弾いた入力値を載せ返すため、
    Infinity/NaNを送られるとjson.dumpsが落ちて500になり、攻撃者の値を反射してしまう。"""
    fields = sorted({".".join(str(p) for p in e["loc"][1:]) or "body" for e in exc.errors()})
    return JSONResponse(status_code=422, content={"detail": "invalid request", "fields": fields})

# instance_id -> {player_name: {"dps":.., "total_damage":.., "last_seen":..}}
rooms: dict[str, dict[str, dict]] = {}

# 同期のdefエンドポイントはuvicornのスレッドプールで並行に走るのでroomsをlockで守る
_lock = threading.Lock()
_last_sweep = 0.0

# client_ip -> (窓の開始時刻, その窓で受けたリクエスト数)
_rate_buckets: dict[str, tuple[float, int]] = {}


# ロックを持ったまま標準出力へ書くと、書き込みが詰まった時に全リクエストが止まる。
# ロック内では溜めるだけにして、解放後にまとめて出す。
_pending_events: list[str] = []


def _queue_event_locked(event: str, instance_id: str, player_name: str, **extra) -> None:
    _pending_events.append(json.dumps({
        "severity": "INFO",
        "event": event,
        "instance_id": instance_id,
        "player_name": player_name,
        **extra,
    }, ensure_ascii=False))


def _flush_events() -> None:
    """溜めたイベントを出力する。ロックを持っていない状態で呼ぶこと。"""
    if not _pending_events:
        return
    with _lock:
        events, _pending_events[:] = list(_pending_events), []
    for line in events:
        print(line, flush=True)


def _drop_stale_locked(instance_id: str, room: dict, now: float) -> None:
    """期限切れのメンバーを取り除き、退出として記録する（要 _lock 保持）。"""
    for name in [n for n, d in room.items() if now - d["last_seen"] > STALE_SECONDS]:
        data = room.pop(name)
        _queue_event_locked(
            "player_left", instance_id, name,
            class_name=data["class_name"],
            stayed_seconds=round(data["last_seen"] - data["first_seen"], 1),
        )


def _sweep_locked(now: float) -> None:
    """期限切れメンバー・空room・古いレート制限を捨てる（要_lock保持）。

    GETの来ないroomは掃除されず残り続けるので、ここで全体を定期的に走査する。
    """
    global _last_sweep
    if now - _last_sweep < SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep = now

    for instance_id in list(rooms):
        room = rooms[instance_id]
        _drop_stale_locked(instance_id, room, now)
        if not room:
            del rooms[instance_id]

    for ip in [
        i for i, (start, _) in _rate_buckets.items() if now - start > RATE_LIMIT_WINDOW_SECONDS
    ]:
        del _rate_buckets[ip]


def _members_snapshot_locked(instance_id: str, now: float) -> list[dict]:
    """期限切れを除いたメンバー一覧を参加順で返す（要 _lock 保持）。"""
    room = rooms.get(instance_id)
    if room is None:
        return []
    _drop_stale_locked(instance_id, room, now)
    if not room:
        rooms.pop(instance_id, None)

    members = sorted(
        ({"player_name": name, **data} for name, data in room.items()),
        key=lambda m: m["first_seen"],
    )
    for m in members:
        del m["last_seen"]
        del m["first_seen"]
    return members


def rate_limit(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    with _lock:
        start, count = _rate_buckets.get(client_ip, (now, 0))
        if now - start >= RATE_LIMIT_WINDOW_SECONDS:
            start, count = now, 0
        # バケット自体も無数のIPから叩かれると増え続けるので頭を打つ
        if client_ip not in _rate_buckets and len(_rate_buckets) >= MAX_RATE_LIMIT_ENTRIES:
            _sweep_locked(now)
            if len(_rate_buckets) >= MAX_RATE_LIMIT_ENTRIES:
                raise HTTPException(status_code=503, detail="server busy")
        count += 1
        _rate_buckets[client_ip] = (start, count)

    _flush_events()
    if count > RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="too many requests")


class Report(BaseModel):
    # 上限が無いと巨大な文字列やNaN/Infinityを送り込める
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(min_length=1, max_length=MAX_INSTANCE_ID_LEN)
    player_name: str = Field(min_length=1, max_length=64)
    dps: float = Field(ge=0, le=1e9, allow_inf_nan=False)
    total_damage: int = Field(ge=0, le=10**12)
    official_boss_damage: int = Field(default=0, ge=0, le=10**12)
    class_name: str = Field(default="", max_length=32)


@app.post("/report", dependencies=[Depends(rate_limit)])
def report(r: Report):
    now = time.time()
    with _lock:
        _sweep_locked(now)

        room = rooms.get(r.instance_id)
        if room is None:
            if len(rooms) >= MAX_ROOMS:
                raise HTTPException(status_code=503, detail="too many active instances")
            room = rooms[r.instance_id] = {}

        existing = room.get(r.player_name)
        if existing is None and len(room) >= MAX_MEMBERS_PER_ROOM:
            raise HTTPException(status_code=503, detail="instance is full")
        joined = existing is None

        # 既にいるメンバーなら、DPSの上下に関係なく最初に参加した時刻を保持し続ける
        # （クライアントの「No順」表示を、DPS変動で毎回入れ替わらない安定した順序にするため）
        first_seen = existing["first_seen"] if existing else now
        room[r.player_name] = {
            "dps": r.dps,
            "total_damage": r.total_damage,
            "official_boss_damage": r.official_boss_damage,
            "class_name": r.class_name,
            "last_seen": now,
            "first_seen": first_seen,
        }
        # 利用状況の集計用。毎リクエストではなく参加した瞬間だけ記録する
        if joined:
            _queue_event_locked("player_joined", r.instance_id, r.player_name,
                                class_name=r.class_name)
        # クライアントは報告と取得を毎周期セットで行うので、ここで一覧も返して往復を1回で済ませる
        members = _members_snapshot_locked(r.instance_id, now)

    _flush_events()
    return {"ok": True, "members": members}


@app.get("/room/{instance_id}", dependencies=[Depends(rate_limit)])
def get_room(instance_id: str = Path(min_length=1, max_length=MAX_INSTANCE_ID_LEN)):
    now = time.time()
    with _lock:
        _sweep_locked(now)
        members = _members_snapshot_locked(instance_id, now)
    _flush_events()
    return {"members": members}


if __name__ == "__main__":
    # Cloud Run 等のPaaSは待ち受けポートを環境変数 PORT で指定してくる
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
