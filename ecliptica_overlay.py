"""VRChatワールド「ECLIPTICA」のログファイルをリアルタイム追跡し、
ステージ／ボス戦単位でDPS・被ダメージ・履歴を常時最前面ウィンドウに表示する。

ステージの区切りは「Nダメージ判定なし」のような時間ベースの推測ではなく、
ECLIPTICAが実際に出す `now in stage` / `now fighting boss` / `now in lobby` ログを
そのまま使って判定する。

設定（監視するログフォルダ・ファイル名パターン・ダメージ行の正規表現など）は
同じフォルダの config.json に保存され、右クリックメニューの「設定」からいつでも変更できる。
"""

import glob
import io
import json
import math
import os
import re
import struct
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.error
import urllib.parse
import urllib.request
import wave
import winsound
from collections import deque
from tkinter import filedialog, messagebox

if getattr(sys, "frozen", False):
    # PyInstallerでexe化した場合、__file__は一時展開フォルダを指すため、
    # 代わりにexe自体の場所を設定ファイルの保存先にする。
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
STATE_PATH = os.path.join(SCRIPT_DIR, "state.json")
APP_LOG_PATH = os.path.join(SCRIPT_DIR, "app.log")
WARNING_SOUND_WAV_PATH = os.path.join(SCRIPT_DIR, "警告音.wav")

DEFAULT_CONFIG = {
    "log_dir": os.path.join(os.environ["USERPROFILE"], "AppData", "LocalLow", "VRChat", "VRChat"),
    "log_glob": "output_log_*.txt",
    "damage_pattern": r"Dealing (\d+) (STRIKE|NON-STRIKE) damage",
    "taken_pattern": r"damage has been taken: (\d+), from source: (\S+)",
    "history_size": 15,
    "party_share_enabled": False,
    "backend_url": "http://localhost:8000",
    "report_interval_seconds": 3,
    "party_sort_mode": "no",
    "history_collapsed": False,
    "window_opacity_percent": 90,
    "target_warning_sound_enabled": False,
    "target_warning_sound_volume_percent": 80,
}


def _build_warning_beep_wav(volume_percent, freq=880, duration_ms=350, sample_rate=44100):
    """指定した音量(0〜100)でビープ音のWAVバイト列を生成する。"""
    volume = max(0.0, min(1.0, volume_percent / 100))
    amplitude = int(32767 * volume)
    n_samples = int(sample_rate * duration_ms / 1000)
    fade_samples = max(1, int(sample_rate * 0.01))  # クリック音防止の10msフェード

    frames = bytearray()
    for i in range(n_samples):
        if i < fade_samples:
            envelope = i / fade_samples
        elif i > n_samples - fade_samples:
            envelope = (n_samples - i) / fade_samples
        else:
            envelope = 1.0
        value = int(amplitude * envelope * math.sin(2 * math.pi * freq * i / sample_rate))
        frames += struct.pack("<h", value)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _load_wav_samples(path):
    """WAVファイルを読み込み、(チャンネル数, サンプル幅, サンプルレート, フレームデータ)を返す。"""
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    return n_channels, sampwidth, framerate, frames


def _scale_wav_volume(n_channels, sampwidth, framerate, frames, volume_percent):
    """16bit PCMのWAVフレームを指定音量(0〜100)にスケールしたWAVバイト列を返す。"""
    volume = max(0.0, min(1.0, volume_percent / 100))
    if sampwidth == 2:
        count = len(frames) // 2
        samples = struct.unpack(f"<{count}h", frames)
        scaled_frames = struct.pack(f"<{count}h", *(int(s * volume) for s in samples))
    else:
        # 16bit以外はそのまま（音量調整なし）で再生する
        scaled_frames = frames

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(scaled_frames)
    return buf.getvalue()

# (内部キー, ドロップダウンに出す表示名)
PARTY_SORT_MODES = [
    ("no", "No順"),
    ("name", "名前順"),
    ("dps", "DPS順"),
    ("total", "トータルダメージ順"),
]
PARTY_SORT_LABELS = [label for _, label in PARTY_SORT_MODES]
PARTY_SORT_KEY_BY_LABEL = {label: key for key, label in PARTY_SORT_MODES}
PARTY_SORT_LABEL_BY_KEY = {key: label for key, label in PARTY_SORT_MODES}

# インスタンスID・自分の表示名・ステージ遷移はVRChat/ECLIPTICA固有のログ形式に
# 依存する内部処理なので、ワールドごとに変わるdamage_pattern等とは違い
# 設定画面には出さずここに固定しておく。
INSTANCE_RE = re.compile(r"Joining (wrld_[\w-]+:\S+)")
LOCAL_PLAYER_RE = re.compile(r'Initialized PlayerAPI "(.+?)" is local')
STAGE_RE = re.compile(r"ECLIPTICA - now in stage: (.+?) on phase: ([\d.]+) as class: (\S+)")
BOSS_RE = re.compile(r"ECLIPTICA - now fighting boss: (.+?)\(Clone\) on phase: [\d.]+")
BOSS_DEFEATED_RE = re.compile(r"Tracking boss as defeated in-run\.")
LOBBY_RE = re.compile(r"ECLIPTICA - now in lobby")
INTERMISSION_RE = re.compile(r"ECLIPTICA - now in intermission")
SESSION_ID_RE = re.compile(r"ECLIPTICA (?:MASTER Setting|saving|loaded) SESSION ID(?: to)? (\d+)")
# ボス撃破時、ゲーム自身が出す「本当にそのボスに与えた合計ダメージ」の答え合わせ用
BOSS_DEAD_RE = re.compile(r"Boss \S+ dead, personal damage dealt:")
STRIKE_DMG_RE = re.compile(r"(?<!NON-)STRIKE DMG: (\d+)")
NON_STRIKE_DMG_RE = re.compile(r"NON-STRIKE DMG: (\d+)")
# VRChatのネットワークオブジェクト所有権(Ownership)移動ログ。雑魚・ボス問わず
# 「今そのオブジェクトを狙っている（ターゲットしている）プレイヤー」に転送されるらしいので、
# 自分に転送された瞬間＝自分が敵に狙われた瞬間の通知に使う。
OWNERSHIP_RE = re.compile(r"ownership of (.+?) transferred to (.+)$")

# クラス名 -> (2文字略語, 表示色)
ROLE_ICON = {"attack": "⚔", "tank": "🛡", "support": "❤"}

# クラス名 -> (2文字略語, 表示色, ロール)
CLASS_INFO = {
    "Spellsword": ("SS", "#ff5555", "attack"),
    "Twinmage": ("TM", "#4caf50", "attack"),
    "Gunmancer": ("GM", "#ffffff", "attack"),
    "Fistmage": ("FM", "#ffd700", "tank"),
    "Spellhammer": ("SH", "#ff9800", "tank"),
    "Shieldmage": ("SM", "#4dd0e1", "tank"),
    "Thaumaturge": ("TH", "#f48fb1", "support"),
    "Nekomancer": ("NM", "#ab47bc", "support"),
}

POLL_MS = 300  # ログファイルを読みに行く間隔
UPDATE_MS = 200  # 表示を更新する間隔


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        config = dict(DEFAULT_CONFIG)
        config.update(data)
        return config
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_state():
    """同じセッションでアプリを再起動した時に、HISTORYを引き継ぐための保存データ。"""
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(session_id, stage_history):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {"session_id": session_id, "stage_history": [s.to_dict() for s in stage_history]},
                f, ensure_ascii=False,
            )
    except OSError:
        pass


def format_duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, config, on_save):
        super().__init__(parent)
        self.title("設定")
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.on_save = on_save
        self.config_data = config

        fields = [
            ("log_dir", "ログフォルダ", True),
            ("log_glob", "ログファイル名パターン (glob)", False),
            ("damage_pattern", "与ダメージ行の正規表現（1つ目の括弧=ダメージ量）", False),
            ("taken_pattern", "被ダメージ行の正規表現（1つ目の括弧=ダメージ量）", False),
            ("history_size", "履歴に残すステージ数", False),
            ("backend_url", "パーティ共有バックエンドのURL", False),
            ("report_interval_seconds", "パーティ共有の送受信間隔（秒）", False),
        ]
        self.vars = {}
        row = 0
        for key, label, has_browse in fields:
            tk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=(8, 2), columnspan=2)
            row += 1
            var = tk.StringVar(value=str(config[key]))
            self.vars[key] = var
            tk.Entry(self, textvariable=var, width=45).grid(row=row, column=0, padx=6)
            if has_browse:
                tk.Button(self, text="参照...", command=lambda v=var: self._browse_dir(v)).grid(
                    row=row, column=1, padx=6
                )
            row += 1

        tk.Label(self, text="ウィンドウの透過率（0〜100、大きいほど不透明）").grid(
            row=row, column=0, sticky="w", padx=6, pady=(8, 2), columnspan=2
        )
        row += 1
        self.opacity_var = tk.IntVar(value=int(config["window_opacity_percent"]))
        tk.Scale(
            self, from_=0, to=100, orient="horizontal", variable=self.opacity_var, length=280,
        ).grid(row=row, column=0, padx=6, columnspan=2, sticky="w")
        row += 1

        self.party_share_var = tk.BooleanVar(value=bool(config["party_share_enabled"]))
        tk.Checkbutton(
            self, text="パーティ共有を有効化（同じVRChatインスタンスの仲間とDPSを共有）",
            variable=self.party_share_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=6, pady=(8, 4))
        row += 1

        self.target_warning_sound_var = tk.BooleanVar(value=bool(config["target_warning_sound_enabled"]))
        tk.Checkbutton(
            self, text="ボスに狙われたら警告音を鳴らす",
            variable=self.target_warning_sound_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4))
        row += 1

        tk.Label(self, text="警告音の音量（0〜100）").grid(
            row=row, column=0, sticky="w", padx=6, pady=(0, 2), columnspan=2
        )
        row += 1
        self.target_warning_sound_volume_var = tk.IntVar(
            value=int(config["target_warning_sound_volume_percent"])
        )
        tk.Scale(
            self, from_=0, to=100, orient="horizontal",
            variable=self.target_warning_sound_volume_var, length=280,
        ).grid(row=row, column=0, padx=6, columnspan=2, sticky="w")
        row += 1

        tk.Button(self, text="保存", command=self._save).grid(row=row, column=0, pady=10, padx=6, sticky="w")
        tk.Button(self, text="キャンセル", command=self.destroy).grid(row=row, column=1, pady=10, padx=6)

    def _browse_dir(self, var):
        chosen = filedialog.askdirectory(initialdir=var.get())
        if chosen:
            var.set(chosen)

    def _save(self):
        try:
            re.compile(self.vars["damage_pattern"].get())
            re.compile(self.vars["taken_pattern"].get())
            history_size = int(self.vars["history_size"].get())
            report_interval = float(self.vars["report_interval_seconds"].get())
        except re.error as exc:
            messagebox.showerror("設定エラー", f"正規表現が不正です:\n{exc}")
            return
        except ValueError:
            messagebox.showerror("設定エラー", "履歴数・送受信間隔は数値で入力してください")
            return

        window_opacity_percent = self.opacity_var.get()

        self.config_data["log_dir"] = self.vars["log_dir"].get()
        self.config_data["log_glob"] = self.vars["log_glob"].get()
        self.config_data["damage_pattern"] = self.vars["damage_pattern"].get()
        self.config_data["taken_pattern"] = self.vars["taken_pattern"].get()
        self.config_data["history_size"] = history_size
        self.config_data["window_opacity_percent"] = window_opacity_percent
        self.config_data["backend_url"] = self.vars["backend_url"].get().rstrip("/")
        self.config_data["report_interval_seconds"] = report_interval
        self.config_data["party_share_enabled"] = self.party_share_var.get()
        self.config_data["target_warning_sound_enabled"] = self.target_warning_sound_var.get()
        self.config_data["target_warning_sound_volume_percent"] = self.target_warning_sound_volume_var.get()
        save_config(self.config_data)
        self.on_save(self.config_data)
        self.destroy()


DPS_WINDOW_SECONDS = 10  # DPS/DTPSは「戦闘全体の平均」ではなく直近この秒数の実測値にする


class StageSegment:
    """1つのステージ（または休憩所）に滞在している間の集計。"""

    def __init__(self, name, phase, class_name, start_time):
        self.name = name
        self.phase = phase
        self.class_name = class_name
        self.is_hub = name.strip() == "Stage_VRCHub"
        self.start_time = start_time
        self.last_event_time = start_time
        self.damage_total = 0
        self.hit_count = 0
        self.taken_total = 0
        self.boss_name = None
        self.boss_defeated = False
        self.outcome = None  # "cleared" / "defeated" / None
        self.official_boss_damage = 0  # ゲーム自身が報告するボス単体への合計ダメージ（答え合わせ用）
        self.recent_hits = deque()  # (timestamp, amount) 直近DPS計算用
        self.recent_taken = deque()  # (timestamp, amount) 直近DTPS計算用

    @property
    def display_name(self):
        return self.name[len("Stage_"):] if self.name.startswith("Stage_") else self.name

    def add_damage(self, amount, now):
        self.damage_total += amount
        self.hit_count += 1
        self.last_event_time = now
        self.recent_hits.append((now, amount))

    def add_taken(self, amount, now):
        self.taken_total += amount
        self.last_event_time = now
        self.recent_taken.append((now, amount))

    def duration(self, at_time=None):
        end = at_time if at_time is not None else self.last_event_time
        return max(0.0, end - self.start_time)

    def dps(self, at_time=None):
        now = at_time if at_time is not None else self.last_event_time
        while self.recent_hits and now - self.recent_hits[0][0] > DPS_WINDOW_SECONDS:
            self.recent_hits.popleft()
        if not self.recent_hits:
            return 0.0
        total = sum(amount for _, amount in self.recent_hits)
        span = max(now - self.recent_hits[0][0], 1e-6)
        return total / min(DPS_WINDOW_SECONDS, span)

    def dtps(self, at_time=None):
        now = at_time if at_time is not None else self.last_event_time
        while self.recent_taken and now - self.recent_taken[0][0] > DPS_WINDOW_SECONDS:
            self.recent_taken.popleft()
        if not self.recent_taken:
            return 0.0
        total = sum(amount for _, amount in self.recent_taken)
        span = max(now - self.recent_taken[0][0], 1e-6)
        return total / min(DPS_WINDOW_SECONDS, span)

    def avg_hit(self):
        return self.damage_total / self.hit_count if self.hit_count else 0.0

    def final_dps(self):
        """終了したステージ用: ローリングウィンドウではなく、ステージ全体の平均。"""
        d = self.duration()
        return self.damage_total / d if d > 0 else 0.0

    def final_dtps(self):
        d = self.duration()
        return self.taken_total / d if d > 0 else 0.0

    def to_dict(self):
        """HISTORYの保存用。ローリングDPS計算用のdequeは復元不要なので含めない。"""
        return {
            "name": self.name,
            "phase": self.phase,
            "class_name": self.class_name,
            "start_time": self.start_time,
            "last_event_time": self.last_event_time,
            "damage_total": self.damage_total,
            "hit_count": self.hit_count,
            "taken_total": self.taken_total,
            "outcome": self.outcome,
            "official_boss_damage": self.official_boss_damage,
        }

    @classmethod
    def from_dict(cls, d):
        stage = cls(d["name"], d["phase"], d["class_name"], d["start_time"])
        stage.last_event_time = d["last_event_time"]
        stage.damage_total = d["damage_total"]
        stage.hit_count = d["hit_count"]
        stage.taken_total = d["taken_total"]
        stage.outcome = d["outcome"]
        stage.official_boss_damage = d.get("official_boss_damage", 0)
        return stage


MAX_PARTY_RESPONSE_BYTES = 64 * 1024
MAX_PARTY_MEMBERS = 40


def _coerce_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value if math.isfinite(value) else 0


def _sanitize_party_members(raw):
    """バックエンドの応答を信用せず、UIに渡す前に型と件数を正規化する。

    描画は_update_displayの中で行われ、そこで例外が漏れると次回のroot.afterに
    到達せずオーバーレイ全体が永久に止まる。壊れた要素は捨てて描画を守る。
    """
    if not isinstance(raw, list):
        return []
    members = []
    for item in raw[:MAX_PARTY_MEMBERS]:
        if not isinstance(item, dict):
            continue
        name = item.get("player_name")
        if not isinstance(name, str) or not name:
            continue
        class_name = item.get("class_name")
        members.append({
            "player_name": name[:64],
            "dps": _coerce_number(item.get("dps")),
            "total_damage": _coerce_number(item.get("total_damage")),
            "official_boss_damage": _coerce_number(item.get("official_boss_damage")),
            "class_name": class_name if isinstance(class_name, str) else "",
        })
    return members


class DPSOverlay:
    def __init__(self):
        self.config = load_config()
        self._compile_patterns()

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.config.get("window_opacity_percent", 90) / 100)
        self.root.configure(bg="#14141a")
        self.root.geometry("320x260+40+40")

        pad = dict(padx=10)

        self._extra_drag_widgets = []

        tk.Label(
            self.root, text="STAGE", fg="#8a8a99", bg="#14141a", font=("Consolas", 9, "bold")
        ).pack(anchor="w", pady=(10, 0), **pad)

        self.stage_name_label = tk.Label(
            self.root, text="-", fg="#cfcfd8", bg="#14141a", font=("Consolas", 12, "bold")
        )
        self.stage_name_label.pack(anchor="w", **pad)

        tk.Label(
            self.root, text=f"DPS（直近{DPS_WINDOW_SECONDS}秒）", fg="#8a8a99", bg="#14141a",
            font=("Consolas", 8),
        ).pack(anchor="w", pady=(4, 0), **pad)

        dps_row = tk.Frame(self.root, bg="#14141a")
        dps_row.pack(fill="x", **pad)
        self._extra_drag_widgets.append(dps_row)

        self.dps_label = tk.Label(
            dps_row, text="0", fg="#ffcc4d", bg="#14141a", font=("Consolas", 30, "bold")
        )
        self.dps_label.pack(side="left")

        self.target_alert_label = tk.Label(
            dps_row, text="", fg="#14141a", bg="#14141a", font=("Consolas", 16, "bold"), anchor="e",
        )
        self.target_alert_label.pack(side="right")
        self._extra_drag_widgets.append(self.target_alert_label)

        self.status_label = tk.Label(
            self.root, text="待機中  00:00", fg="#8a8a99", bg="#14141a", font=("Consolas", 10)
        )
        self.status_label.pack(anchor="w", **pad)

        stats_frame = tk.Frame(self.root, bg="#14141a")
        stats_frame.pack(anchor="w", pady=(6, 0), **pad)
        self._extra_drag_widgets.append(stats_frame)

        def add_stat(row, col, label_text, color):
            tk.Label(
                stats_frame, text=label_text, fg="#8a8a99", bg="#14141a",
                font=("Consolas", 10), anchor="w", width=6,
            ).grid(row=row, column=col * 2, sticky="w")
            value = tk.Label(
                stats_frame, text="0", fg=color, bg="#14141a",
                font=("Consolas", 10), anchor="w", width=7,
            )
            value.grid(row=row, column=col * 2 + 1, sticky="w")
            self._extra_drag_widgets.append(value)
            return value

        self.total_value = add_stat(0, 0, "Total:", "#e0e0e8")
        self.hits_value = add_stat(0, 1, "Hits:", "#e0e0e8")
        self.avg_value = add_stat(0, 2, "Avg:", "#e0e0e8")
        self.dtps_value = add_stat(1, 0, "DTPS:", "#ff6b6b")
        self.taken_value = add_stat(1, 1, "Taken:", "#ff6b6b")

        tk.Frame(self.root, bg="#33333d", height=1).pack(fill="x", pady=8, **pad)

        self.history_collapsed = bool(self.config.get("history_collapsed", False))

        history_header = tk.Frame(self.root, bg="#14141a", cursor="hand2")
        history_header.pack(fill="x", **pad)

        self.history_toggle_label = tk.Label(
            history_header, text="HISTORY", fg="#8a8a99", bg="#14141a", font=("Consolas", 9, "bold"),
            cursor="hand2",
        )
        self.history_toggle_label.pack(side="left")

        for w in (history_header, self.history_toggle_label):
            w.bind("<Button-1>", self._toggle_history)
            # <B1-Motion>は<Button-1>とは別イベントなので、押下時のbreakだけでは
            # ドラッグ移動(_on_move)がトップレベル経由で発火してしまう。ここでも止める。
            w.bind("<B1-Motion>", lambda event: "break")

        self.history_frame = tk.Frame(self.root, bg="#14141a")
        self.history_frame.pack(anchor="w", fill="x", pady=(2, 10), **pad)
        self._extra_drag_widgets.append(self.history_frame)

        self.history_empty_label = tk.Label(
            self.history_frame, text="(まだ記録なし)", fg="#cfcfd8", bg="#14141a", font=("Consolas", 9),
        )
        self.history_empty_label.grid(row=0, column=0, sticky="w")
        self._extra_drag_widgets.append(self.history_empty_label)

        self.history_rows = []
        self._build_history_rows()
        self._apply_history_visibility()

        tk.Frame(self.root, bg="#33333d", height=1).pack(fill="x", pady=(0, 8), **pad)

        party_header = tk.Frame(self.root, bg="#14141a")
        party_header.pack(fill="x", **pad)

        tk.Label(
            party_header, text="PARTY", fg="#8a8a99", bg="#14141a", font=("Consolas", 9, "bold")
        ).pack(side="left")

        self.party_sort_var = tk.StringVar(
            value=PARTY_SORT_LABEL_BY_KEY.get(self.config.get("party_sort_mode", "no"), "No順")
        )
        party_sort_menu = tk.OptionMenu(
            party_header, self.party_sort_var, *PARTY_SORT_LABELS, command=self._on_party_sort_change
        )
        party_sort_menu.config(
            fg="#cfcfd8", bg="#14141a", activebackground="#26262f", activeforeground="#ffffff",
            highlightthickness=0, bd=0, font=("Consolas", 8), indicatoron=True,
        )
        party_sort_menu["menu"].config(bg="#14141a", fg="#cfcfd8", font=("Consolas", 8))
        party_sort_menu.pack(side="right")

        self.party_label = tk.Label(
            self.root, text="(パーティ共有はオフです)", fg="#cfcfd8", bg="#14141a",
            font=("Consolas", 9), justify="left"
        )
        self.party_label.pack(anchor="w", pady=(2, 4), **pad)

        self.party_rows_frame = tk.Frame(self.root, bg="#14141a")
        self.party_rows_frame.pack(anchor="w", fill="x", pady=(0, 10), **pad)
        self.party_rows = []
        for i in range(10):  # ワールドの上限人数分だけ先に作っておき、使う分だけ表示する
            row_widgets = {}
            for col, (key, width) in enumerate(self.PARTY_COL_WIDTHS.items()):
                w = tk.Label(
                    self.party_rows_frame, text="", fg="#e0e0e8", bg="#14141a",
                    font=("Consolas", 9), anchor=self.PARTY_COL_ANCHOR.get(key, "w"), width=width,
                )
                w.grid(row=i, column=col, sticky="w", padx=(0, 4))
                w.grid_remove()
                row_widgets[key] = w
            self.party_rows.append(row_widgets)

        for widget in self._draggable_widgets():
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._on_move)
            widget.bind("<Button-3>", self._show_menu)

        self._drag_x = 0
        self._drag_y = 0

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="設定", command=self._open_settings)
        self.menu.add_command(label="リセット", command=self._reset_all)
        self.menu.add_separator()
        self.menu.add_command(label="終了", command=self.root.destroy)

        self.current_stage = None
        self.stage_history = deque(maxlen=self.config["history_size"])

        self.current_log_path = None
        self.log_file = None

        self.instance_id = None
        self.player_name = None
        self.session_id = None
        self.in_lobby = False
        self.current_boss_target = None  # 現在ボスのownershipを持っている（＝狙われている）プレイヤー名
        self._pending_strike_dmg = None
        self.party_members = []

        self._warning_wav = None
        self._last_warning_wav_bytes = None
        if os.path.exists(WARNING_SOUND_WAV_PATH):
            try:
                self._warning_wav = _load_wav_samples(WARNING_SOUND_WAV_PATH)
            except (wave.Error, EOFError):
                pass

        self._switch_log_if_needed()
        self._restore_state_if_matching()
        if self.log_file is None:
            self.root.after(300, self._warn_log_not_found)
        self.root.after(POLL_MS, self._poll_log)
        self.root.after(UPDATE_MS, self._update_display)

        self._network_thread = threading.Thread(target=self._network_worker, daemon=True)
        self._network_thread.start()

    def _play_target_warning_sound(self):
        volume_percent = self.config.get("target_warning_sound_volume_percent", 80)
        if self._warning_wav is not None:
            n_channels, sampwidth, framerate, frames = self._warning_wav
            wav_bytes = _scale_wav_volume(n_channels, sampwidth, framerate, frames, volume_percent)
        else:
            wav_bytes = _build_warning_beep_wav(volume_percent)
        # SND_ASYNC + SND_MEMORYは再生完了までバッファを生かしておく必要があるため、
        # ローカル変数のままにせずselfに保持してGCで解放されるのを防ぐ
        self._last_warning_wav_bytes = wav_bytes
        try:
            winsound.PlaySound(self._last_warning_wav_bytes, winsound.SND_MEMORY | winsound.SND_ASYNC)
        except RuntimeError:
            pass

    def _warn_log_not_found(self):
        messagebox.showwarning(
            "ログが見つかりません",
            "ログフォルダが推定できませんでした。\n"
            f"推定した場所: {self.config['log_dir']}\n\n"
            "続けて開く「設定」画面で、正しいログフォルダを指定してください。",
        )
        self._open_settings()

    def _draggable_widgets(self):
        return [
            self.root, self.stage_name_label, self.dps_label, self.status_label,
            self.party_label,
            *self._extra_drag_widgets,
            *(w for row in self.party_rows for w in row.values()),
        ]

    def _compile_patterns(self):
        self.damage_re = re.compile(self.config["damage_pattern"])
        self.taken_re = re.compile(self.config["taken_pattern"])

    def _start_move(self, event):
        # event.x/event.yはクリックされたウィジェット自身からの相対座標なので、
        # ネストした子ウィジェットで押された場合はウィンドウ全体での位置とズレる。
        # 絶対座標(x_root/y_root)を使い、ウィンドウ左上からのオフセットとして保持する。
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _on_move(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _show_menu(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)

    def _reset_all(self):
        self.current_stage = None
        self.stage_history.clear()
        self._refresh_history_display()
        save_state(self.session_id, self.stage_history)

    def _on_party_sort_change(self, selected_label):
        self.config["party_sort_mode"] = PARTY_SORT_KEY_BY_LABEL.get(selected_label, "no")
        save_config(self.config)
        self._refresh_party_display()

    def _open_settings(self):
        SettingsDialog(self.root, dict(self.config), self._apply_new_config)

    def _apply_new_config(self, new_config):
        self.config = new_config
        self._compile_patterns()
        self.root.attributes("-alpha", self.config.get("window_opacity_percent", 90) / 100)
        self.stage_history = deque(self.stage_history, maxlen=self.config["history_size"])
        self._build_history_rows()
        self._refresh_history_display()
        if self.log_file:
            self.log_file.close()
        self.current_log_path = None
        self.log_file = None
        self._switch_log_if_needed()

    def _find_latest_log(self):
        files = glob.glob(os.path.join(self.config["log_dir"], self.config["log_glob"]))
        if not files:
            return None
        return max(files, key=os.path.getmtime)

    def _switch_log_if_needed(self):
        latest = self._find_latest_log()
        if latest and latest != self.current_log_path:
            if self.log_file:
                self.log_file.close()
            self.current_log_path = latest
            self._catch_up_identity(latest)
            self.log_file = open(latest, "r", encoding="utf-8", errors="ignore")
            self.log_file.seek(0, os.SEEK_END)

    def _catch_up_identity(self, path):
        """ダメージ等のイベントは起動後の分だけ集計するが、インスタンスID・自分の
        表示名・現在のステージ/ボスの文脈は「その時点の最新状態」を復元しないと、
        アプリを再起動しただけでダメージが一切どこにも紐付かなくなってしまう。
        ダメージ自体はここでは加算しない（起動後の分だけ集計する設計は保つ）。"""
        stage_name = stage_phase = stage_class = None
        boss_name = None
        boss_defeated = False
        run_ended = False
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = INSTANCE_RE.search(line)
                    if m:
                        self.instance_id = m.group(1)
                        continue
                    m = LOCAL_PLAYER_RE.search(line)
                    if m:
                        self.player_name = m.group(1)
                        continue
                    m = STAGE_RE.search(line)
                    if m:
                        stage_name = m.group(1).strip()
                        stage_phase = float(m.group(2))
                        stage_class = m.group(3)
                        boss_name = None
                        boss_defeated = False
                        run_ended = False
                        continue
                    m = BOSS_RE.search(line)
                    if m:
                        boss_name = m.group(1).strip()
                        boss_defeated = False
                        continue
                    if BOSS_DEFEATED_RE.search(line):
                        if boss_name is not None:
                            boss_defeated = True
                        continue
                    if LOBBY_RE.search(line):
                        run_ended = True
                        continue
                    if INTERMISSION_RE.search(line):
                        # 休憩に入った時点で前のステージは確定済み(HISTORY行済み)なので、
                        # 「今アクティブなステージは無い」ものとして扱う。
                        stage_name = None
                        continue
                    m = SESSION_ID_RE.search(line)
                    if m:
                        self.session_id = m.group(1)
        except OSError:
            return

        self.in_lobby = run_ended
        if stage_name and not run_ended:
            # 起動時点を新たな開始時刻とする（過去分のダメージは遡って数えない）
            stage = StageSegment(stage_name, stage_phase, stage_class, time.time())
            stage.boss_name = boss_name
            stage.boss_defeated = boss_defeated
            self.current_stage = stage

    def _restore_state_if_matching(self):
        """同じセッションでアプリを再起動した場合、保存済みのHISTORYを復元する。"""
        state = load_state()
        if not state or state.get("session_id") != self.session_id or self.session_id is None:
            return
        try:
            restored = [StageSegment.from_dict(d) for d in state.get("stage_history", [])]
        except (KeyError, TypeError, ValueError):
            return
        self.stage_history = deque(restored, maxlen=self.config["history_size"])
        self._refresh_history_display()

    def _poll_log(self):
        self._switch_log_if_needed()
        if self.log_file:
            for line in self.log_file:
                self._handle_line(line)
        self.root.after(POLL_MS, self._poll_log)

    def _handle_line(self, line):
        now = time.time()
        m = self.damage_re.search(line)
        if m:
            amount = int(m.group(1))
            # ボス撃破後、次の交戦(BOSS_RE)が始まるまでの間はダミー攻撃等の
            # 練習用ダメージとみなし、そのステージの集計には加算しない。
            if self.current_stage and not self.current_stage.boss_defeated:
                self.current_stage.add_damage(amount, now)
            return
        m = self.taken_re.search(line)
        if m:
            amount = int(m.group(1))
            if self.current_stage and not self.current_stage.boss_defeated:
                self.current_stage.add_taken(amount, now)
            return
        m = STAGE_RE.search(line)
        if m:
            self._end_current_stage(now)
            name, phase, class_name = m.group(1).strip(), float(m.group(2)), m.group(3)
            self.current_stage = StageSegment(name, phase, class_name, now)
            self.in_lobby = False
            self.current_boss_target = None
            return
        m = BOSS_RE.search(line)
        if m:
            if self.current_stage:
                self.current_stage.boss_name = m.group(1).strip()
                self.current_stage.boss_defeated = False
                self.current_stage.last_event_time = now
            return
        if BOSS_DEFEATED_RE.search(line):
            # このログは撃破後もしばらく（前のボスの分が）繰り返し届くことがあるため、
            # 「今のステージでまだボス戦が始まっていない」場合は誤って受け付けない。
            if self.current_stage and self.current_stage.boss_name and not self.current_stage.boss_defeated:
                self.current_stage.boss_defeated = True
                self.current_stage.last_event_time = now
            return
        if LOBBY_RE.search(line):
            self._end_current_stage(now, run_ended=True)
            self.in_lobby = True
            self.current_boss_target = None
            return
        if INTERMISSION_RE.search(line):
            # ボス撃破後の小休止に入った時点でHISTORYを確定させる
            # （次のステージ読み込みまで待たない）
            self._end_current_stage(now)
            self.current_boss_target = None
            return
        m = INSTANCE_RE.search(line)
        if m:
            self.instance_id = m.group(1)
            return
        m = LOCAL_PLAYER_RE.search(line)
        if m:
            self.player_name = m.group(1)
            return
        m = OWNERSHIP_RE.search(line)
        if m:
            enemy_name, new_owner = m.group(1).strip(), m.group(2).strip()
            # 雑魚は無視し、現在のステージのボスに対する所有権移動だけを見る
            if self.current_stage and enemy_name == self.current_stage.boss_name:
                was_targeted = self.current_boss_target == self.player_name
                self.current_boss_target = new_owner
                is_targeted = self.current_boss_target == self.player_name
                if is_targeted and not was_targeted and self.config.get("target_warning_sound_enabled"):
                    self._play_target_warning_sound()
            return
        m = SESSION_ID_RE.search(line)
        if m:
            new_session_id = m.group(1)
            if self.session_id is not None and new_session_id != self.session_id:
                # 新しいランが始まった合図なので、前のランの履歴を持ち越さない
                self.stage_history.clear()
                self._refresh_history_display()
            self.session_id = new_session_id
            save_state(self.session_id, self.stage_history)
            return
        if BOSS_DEAD_RE.search(line):
            self._pending_strike_dmg = None
            return
        m = STRIKE_DMG_RE.search(line)
        if m:
            self._pending_strike_dmg = int(m.group(1))
            return
        m = NON_STRIKE_DMG_RE.search(line)
        if m:
            if self._pending_strike_dmg is not None and self.current_stage:
                self.current_stage.official_boss_damage += self._pending_strike_dmg + int(m.group(1))
            self._pending_strike_dmg = None

    def _end_current_stage(self, now, run_ended=False):
        stage = self.current_stage
        if stage is None:
            return
        if not stage.is_hub:
            if run_ended and not stage.boss_defeated:
                stage.outcome = "defeated"
            elif stage.boss_defeated:
                stage.outcome = "cleared"
        self.stage_history.append(stage)
        self._refresh_history_display()
        self.current_stage = None
        save_state(self.session_id, self.stage_history)

    def _describe_stage_status(self, stage, now):
        if stage.is_hub:
            return f"休憩所  {format_duration(stage.duration(now))}"
        if stage.boss_name and not stage.boss_defeated:
            return f"ボス戦: {stage.boss_name}  {format_duration(stage.duration(now))}"
        if stage.boss_defeated:
            return f"ボス撃破  {format_duration(stage.duration(now))}"
        return f"探索中  {format_duration(stage.duration(now))}"

    HISTORY_COL_WIDTHS = {"rank": 3, "name": 12, "duration": 6, "dps": 8, "damage": 14}
    HISTORY_COL_ANCHOR = {"damage": "e", "dps": "e"}
    PARTY_COL_WIDTHS = {"rank": 3, "name": 11, "class": 5, "dps": 7, "total": 16}
    PARTY_COL_ANCHOR = {"dps": "e", "total": "e"}

    def _toggle_history(self, event=None):
        self.history_collapsed = not self.history_collapsed
        self.config["history_collapsed"] = self.history_collapsed
        save_config(self.config)
        self._apply_history_visibility()
        return "break"  # トップレベル(ウィンドウ移動)側へのイベント伝播を止める

    def _apply_history_visibility(self):
        """history_frame自体は常に同じ位置に置いたまま、中身だけ出し入れする
        （pack_forget/packで枠を出し入れすると、他ウィジェットとの並び順が崩れるため）。
        行はgrid_remove()だけだと枠の高さ計算が縮まないことがあるため、
        折りたたむ時は行ウィジェットごと破棄し、開く時に作り直す。"""
        arrow = "▶" if self.history_collapsed else "▼"
        self.history_toggle_label.config(text=f"{arrow} HISTORY")
        if self.history_collapsed:
            self.history_empty_label.grid_remove()
            for row in self.history_rows:
                for w in row.values():
                    w.destroy()
            self.history_rows = []
            # grid_remove/destroy後もgridの要求サイズがすぐには縮まないことがあるため、
            # 高さを強制的に上書きする。
            self.history_frame.grid_propagate(False)
            self.history_frame.config(height=1)
        else:
            self.history_frame.grid_propagate(True)
            self._build_history_rows()
            if hasattr(self, "stage_history"):
                self._refresh_history_display()
            else:
                self.history_empty_label.grid()

    def _build_history_rows(self):
        """history_sizeに合わせて履歴行のウィジェットを作り直す。"""
        for row in self.history_rows:
            for w in row.values():
                w.destroy()
        self.history_rows = []
        for i in range(self.config["history_size"]):
            row_widgets = {}
            for col, (key, width) in enumerate(self.HISTORY_COL_WIDTHS.items()):
                w = tk.Label(
                    self.history_frame, text="", fg="#cfcfd8", bg="#14141a", font=("Consolas", 9),
                    width=width, anchor=self.HISTORY_COL_ANCHOR.get(key, "w"),
                )
                w.grid(row=i, column=col, sticky="w", padx=(0, 6))
                w.grid_remove()
                w.bind("<ButtonPress-1>", self._start_move)
                w.bind("<B1-Motion>", self._on_move)
                w.bind("<Button-3>", self._show_menu)
                row_widgets[key] = w
            self.history_rows.append(row_widgets)

    def _refresh_history_display(self):
        if self.history_collapsed:
            return
        if not self.stage_history:
            for row in self.history_rows:
                for w in row.values():
                    w.grid_remove()
            self.history_empty_label.grid()
            return
        self.history_empty_label.grid_remove()

        for i, row in enumerate(self.history_rows):
            if i >= len(self.stage_history):
                for w in row.values():
                    w.grid_remove()
                continue
            stage = self.stage_history[i]
            color = "#ff6b6b" if stage.outcome == "defeated" else "#cfcfd8"
            row["rank"].config(text=f"{i + 1})", fg=color)
            row["name"].config(text=stage.display_name[:12], fg=color)
            row["duration"].config(text=format_duration(stage.duration()), fg=color)
            damage_text = str(stage.damage_total)
            if stage.official_boss_damage:
                damage_text += f"({stage.official_boss_damage})"
            row["damage"].config(text=damage_text, fg=color)
            row["dps"].config(text=f"{stage.final_dps():.0f}dps", fg=color)
            for w in row.values():
                w.grid()

    def _set_stats(self, damage_total, hit_count, avg_hit, dtps, taken_total):
        self.total_value.config(text=str(damage_total))
        self.hits_value.config(text=str(hit_count))
        self.avg_value.config(text=f"{avg_hit:.0f}")
        self.dtps_value.config(text=f"{dtps:.0f}")
        self.taken_value.config(text=str(taken_total))

    def _update_display(self):
        now = time.time()

        if self.current_boss_target:
            is_self = self.current_boss_target == self.player_name
            color = "#ff6b6b" if is_self else "#8a8a99"
            self.target_alert_label.config(
                text=f"⚠ {self.current_boss_target[:10]}", fg=color, bg="#14141a",
            )
        else:
            self.target_alert_label.config(text="", fg="#14141a", bg="#14141a")

        stage = self.current_stage
        if stage is not None:
            self.stage_name_label.config(text=f"{stage.display_name}  {stage.phase * 100:.0f}%")
            self.status_label.config(text=self._describe_stage_status(stage, now), fg="#8a8a99")
            self.dps_label.config(text=f"{stage.dps(now):.0f}")
            self._set_stats(
                stage.damage_total, stage.hit_count, stage.avg_hit(), stage.dtps(now), stage.taken_total
            )
        elif self.stage_history:
            last = self.stage_history[-1]
            self.stage_name_label.config(text=f"{last.display_name}  {last.phase * 100:.0f}%")
            self.status_label.config(text=f"待機中（前回 {format_duration(last.duration())}）", fg="#8a8a99")
            self.dps_label.config(text=f"{last.final_dps():.0f}")
            self._set_stats(
                last.damage_total, last.hit_count, last.avg_hit(), last.final_dtps(), last.taken_total
            )
        else:
            self.stage_name_label.config(text="-")
            if self.log_file is None:
                self.status_label.config(text="⚠ ログ未検出（右クリック→設定）", fg="#ff6b6b")
            else:
                self.status_label.config(text="待機中  00:00", fg="#8a8a99")
            self.dps_label.config(text="0")
            self._set_stats(0, 0, 0, 0, 0)

        try:
            self._refresh_party_display()
            self._fit_window_to_content()
        finally:
            # 再スケジュールに到達しないとオーバーレイ全体が二度と更新されない
            self.root.after(UPDATE_MS, self._update_display)

    def _fit_window_to_content(self):
        """内容量に合わせて幅・高さを自動調整する（位置は保つ）。"""
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        self.root.geometry(f"{width}x{height}")

    def _refresh_party_display(self):
        if not self.config.get("party_share_enabled"):
            self._set_party_message("(パーティ共有はオフです)")
            return
        if not self.party_members:
            self._set_party_message("(まだ他のメンバーがいません)")
            return

        self.party_label.pack_forget()

        members = list(self.party_members)
        mode = self.config.get("party_sort_mode", "no")
        if mode == "name":
            members.sort(key=lambda m: m.get("player_name", ""))
        elif mode == "dps":
            members.sort(key=lambda m: m.get("dps", 0), reverse=True)
        elif mode == "total":
            members.sort(key=lambda m: m.get("total_damage", 0), reverse=True)
        # "no" の場合はサーバーから届いた順のまま並べ替えしない

        for i, row in enumerate(self.party_rows):
            if i >= len(members):
                for w in row.values():
                    w.grid_remove()
                continue
            member = members[i]
            name = member.get("player_name", "?")
            is_self = name == self.player_name
            is_targeted = self.current_boss_target is not None and name == self.current_boss_target
            row_bg = "#3a1f22" if is_targeted else "#14141a"
            color = "#e8b4b8" if is_targeted else ("#ffd166" if is_self else "#e0e0e8")
            class_color_fallback = "#8a8a99"

            total_text = str(member.get("total_damage", 0))
            official_boss_damage = member.get("official_boss_damage", 0)
            if official_boss_damage:
                total_text += f"({official_boss_damage})"

            class_name = member.get("class_name", "")
            class_abbr, class_color, role = CLASS_INFO.get(class_name, ("", class_color_fallback, None))
            icon = ROLE_ICON.get(role, "")
            if is_targeted:
                class_color = "#e8b4b8"

            row["rank"].config(text=f"{i + 1})", fg=color, bg=row_bg)
            row["name"].config(text=name[:11], fg=color, bg=row_bg)
            row["class"].config(text=f"{icon}{class_abbr}", fg=class_color, bg=row_bg)
            row["dps"].config(text=f"{member.get('dps', 0):.0f}dps", fg=color, bg=row_bg)
            row["total"].config(text=total_text, fg=color, bg=row_bg)
            for w in row.values():
                w.grid()

    def _set_party_message(self, text):
        for row in self.party_rows:
            for w in row.values():
                w.grid_remove()
        self.party_label.config(text=text)
        if not self.party_label.winfo_ismapped():
            self.party_label.pack(anchor="w", pady=(2, 4), padx=10)

    def _current_class_name(self):
        """ロビー（次のクラス選択中）は分からないので空にする。"""
        if self.in_lobby:
            return ""
        if self.current_stage is not None:
            return self.current_stage.class_name
        if self.stage_history:
            return self.stage_history[-1].class_name
        return ""

    def _current_dps_snapshot(self):
        """今のDPS/合計ダメージ/ボス単体ダメージを、UIスレッド外からも読める形で計算する。"""
        now = time.time()
        stage = self.current_stage
        if stage is not None:
            return stage.dps(now), stage.damage_total, stage.official_boss_damage
        if self.stage_history:
            last = self.stage_history[-1]
            return last.final_dps(), last.damage_total, last.official_boss_damage
        return 0.0, 0, 0

    def _network_worker(self):
        while True:
            try:
                interval = self.config.get("report_interval_seconds", 3)
                if (
                    self.config.get("party_share_enabled")
                    and self.instance_id
                    and self.player_name
                ):
                    dps, total, official_boss_damage = self._current_dps_snapshot()
                    class_name = self._current_class_name()
                    self._report_and_fetch(dps, total, official_boss_damage, class_name)
                else:
                    self.party_members = []
            except Exception:
                # ここで例外を握りつぶさずに投げると、このバックグラウンドスレッドが
                # 静かに死んでパーティ共有だけ二度と復帰しなくなる（exeは--noconsoleなので
                # エラーにも気づけない）。何が起きたかは後で追えるようログにだけ残す。
                self._log_network_error(traceback.format_exc())
            time.sleep(max(1.0, self.config.get("report_interval_seconds", 3)))

    def _log_network_error(self, message):
        try:
            with open(APP_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
        except OSError:
            pass

    _last_http_error_log_at = 0.0

    def _report_and_fetch(self, dps, total, official_boss_damage, class_name):
        base_url = self.config.get("backend_url", "").rstrip("/")
        # urllibはfile://等も開けてしまうのでhttp(s)以外は使わない
        if not base_url.startswith(("http://", "https://")):
            return
        payload = {
            "instance_id": self.instance_id,
            "player_name": self.player_name,
            "dps": dps,
            "total_damage": total,
            "official_boss_damage": official_boss_damage,
            "class_name": class_name,
        }
        try:
            req = urllib.request.Request(
                f"{base_url}/report",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            # ゼロスケール構成だと初回がコールドスタート待ちになるので長めに取る
            urllib.request.urlopen(req, timeout=10).close()

            req = urllib.request.Request(
                f"{base_url}/room/{urllib.parse.quote(self.instance_id, safe='')}"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                # 際限なく読むと巨大な応答でメモリを食い潰される
                body = resp.read(MAX_PARTY_RESPONSE_BYTES)
            data = json.loads(body.decode("utf-8"))
            self.party_members = _sanitize_party_members(
                data.get("members") if isinstance(data, dict) else None
            )
        except urllib.error.HTTPError as exc:
            # HTTPErrorはURLErrorのサブクラスなので、下のexceptに任せると429/503が無言で消える。
            # 毎周回書くとログが膨れるので間隔を空ける。
            now = time.time()
            if now - self._last_http_error_log_at > 60:
                self._last_http_error_log_at = now
                self._log_network_error(f"backend returned HTTP {exc.code} {exc.reason}")
        except (urllib.error.URLError, OSError, ValueError):
            # バックエンドに繋がらない場合は静かに諦めて次の間隔で再試行する
            pass

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    DPSOverlay().run()
