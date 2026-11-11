#!/usr/bin/env python3
"""VoxelLive dashboard + Minecraft Bedrock bridge + TikTok LIVE gift listener."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import mimetypes
import re
import threading
import time
import uuid
import os
import shutil
import socket
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
BASE_DATA_DIR = Path(os.environ.get("VOXELLIVE_DATA_DIR", str(ROOT / "data")))
DATA_DIR = BASE_DATA_DIR
GIFTS_FILE = DATA_DIR / "gifts.json"
PRESETS_FILE = DATA_DIR / "presets.json"
ACTIVE_USER_ID: str | None = None
AUTH_CACHE: dict[str, tuple[float, dict]] = {}
CLOUD_CONFIG_FILE = ROOT / "cloud_api_url.txt"


def cloud_api_base_url() -> str:
    value = os.environ.get("VOXELLIVE_CLOUD_URL", "").strip()
    if not value and CLOUD_CONFIG_FILE.exists():
        value = CLOUD_CONFIG_FILE.read_text(encoding="utf-8").strip()
    value = value.rstrip("/")
    if not value.startswith("https://"):
        raise RuntimeError("Cloud API belum diatur. Isi cloud_api_url.txt dengan URL HTTPS backend akun VoxelLive.")
    return value


def cloud_api_call(path: str, payload: dict | None = None, token: str | None = None) -> dict:
    url = cloud_api_base_url() + path
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0 VoxelLive/2.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            result = {"ok": False, "message": f"Cloud API HTTP {exc.code}."}
        result.setdefault("http_status", exc.code)
        return result
    except Exception as exc:
        raise RuntimeError(f"Tidak bisa menghubungi Cloud API: {exc}") from exc


def activate_local_user(user_id: str) -> None:
    """Select this installation's local profile; the cloud never hosts Minecraft sockets."""
    global ACTIVE_USER_ID, DATA_DIR, GIFTS_FILE, PRESETS_FILE, preset_store, last_command_monotonic
    user_id = re.sub(r"[^A-Za-z0-9_-]", "", str(user_id))[:80]
    if not user_id:
        raise ValueError("ID akun tidak valid.")
    if ACTIVE_USER_ID == user_id:
        return
    old_user = ACTIVE_USER_ID
    if old_user and ws_loop is not None and ws_loop.is_running():
        try:
            _run_async(stop_tiktok_connection(), timeout=5)
            _run_async(close_minecraft_socket(), timeout=5)
        except Exception:
            log.exception("Could not reset old local user's connections")
    user_dir = BASE_DATA_DIR / "users" / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    marker = BASE_DATA_DIR / ".legacy_profile_migrated"
    if not marker.exists():
        for source_name in ("presets.json", "gifts.json"):
            source = BASE_DATA_DIR / source_name
            target = user_dir / source_name
            if source.exists() and not target.exists():
                shutil.copy2(source, target)
        BASE_DATA_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(user_id, encoding="utf-8")
        log.info("Migrated existing local V1.4 settings to the first approved account")
    ACTIVE_USER_ID = user_id
    DATA_DIR = user_dir
    GIFTS_FILE = DATA_DIR / "gifts.json"
    PRESETS_FILE = DATA_DIR / "presets.json"
    preset_store = None
    last_command_monotonic = 0.0
    ensure_preset_store()
    with state_lock:
        state.update({
            "minecraft_connected": False, "minecraft_connected_at": None,
            "minecraft_last_event": None, "minecraft_last_command": None,
            "minecraft_last_error": None, "tiktok_status": "disconnected",
            "tiktok_username": "", "tiktok_detail": "Akun belum dihubungkan",
            "tiktok_last_error": None, "tiktok_room_id": None,
            "tiktok_recent_gifts": [], "tiktok_gift_catalog": [],
            "gift_catalog_status": "not_loaded",
            "gift_catalog_message": "Hubungkan TikTok LIVE untuk memuat katalog gift.",
        })



def deactivate_local_user(user_id: str | None = None) -> None:
    global ACTIVE_USER_ID, DATA_DIR, GIFTS_FILE, PRESETS_FILE, preset_store, last_command_monotonic
    if user_id and ACTIVE_USER_ID != user_id:
        return
    if ACTIVE_USER_ID and ws_loop is not None and ws_loop.is_running():
        try:
            _run_async(stop_tiktok_connection(), timeout=5)
            _run_async(close_minecraft_socket(), timeout=5)
        except Exception:
            log.exception("Could not close local connections during logout")
    ACTIVE_USER_ID = None
    DATA_DIR = BASE_DATA_DIR
    GIFTS_FILE = DATA_DIR / "gifts.json"
    PRESETS_FILE = DATA_DIR / "presets.json"
    preset_store = None
    last_command_monotonic = 0.0
    with state_lock:
        state.update({
            "minecraft_connected": False, "minecraft_connected_at": None,
            "minecraft_last_event": None, "minecraft_last_command": None,
            "minecraft_last_error": None, "tiktok_status": "disconnected",
            "tiktok_username": "", "tiktok_detail": "Login untuk mulai menggunakan VoxelLive",
            "tiktok_last_error": None, "tiktok_room_id": None,
            "tiktok_recent_gifts": [], "tiktok_gift_catalog": [],
            "gift_catalog_status": "not_loaded",
            "gift_catalog_message": "Hubungkan TikTok LIVE untuk memuat katalog gift.",
        })


def validate_local_token(token: str) -> dict | None:
    if not token:
        return None
    cached = AUTH_CACHE.get(token)
    if cached and time.time() - cached[0] < 21600:
        return cached[1]
    try:
        result = cloud_api_call("/api/auth/validate", {}, token)
    except Exception as exc:
        log.warning("Cloud auth validation unavailable: %s", exc)
        return None
    user = result.get("user") if result.get("ok") else None
    if isinstance(user, dict) and user.get("id"):
        AUTH_CACHE[token] = (time.time(), user)
        return user
    AUTH_CACHE.pop(token, None)
    return None
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8766
MC_HOST = "0.0.0.0"
MC_PORT = 6464
DEFAULT_COMMAND_COOLDOWN_MS = 500
MIN_COMMAND_COOLDOWN_MS = 100
MAX_COMMAND_COOLDOWN_MS = 10_000
MAX_COMMANDS_PER_GIFT_EVENT = 10
MAX_COMMANDS_PER_GIFT_RULE = 10
MAX_RECENT_EVENTS = 30
MAX_PRESETS = 50

# Each TikTok viewer may trigger Follow Mapping only once per creator account
# on this local VoxelLive profile. The ledger survives reconnects and restarts.
_follow_seen_lock = threading.RLock()

def get_lan_ip() -> str:
    """Return the LAN address other devices should use to reach this server."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        sock.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return "127.0.0.1"

def claim_follow_once(creator: str, viewer_key: str) -> bool:
    """Atomically remember a viewer follow; False means already processed."""
    if not viewer_key:
        return True
    path = DATA_DIR / "followed_users.json"
    key = f"{creator.strip().lower()}:{viewer_key.strip().lower()}"
    with _follow_seen_lock:
        try:
            records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            if not isinstance(records, list):
                records = []
        except Exception:
            records = []
        known = {str(item) for item in records}
        if key in known:
            return False
        records.append(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        return True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("voxellive")

DEFAULT_GIFTS = [
    {"id": "rose", "name": "Rose", "emoji": "🌹", "command": "/summon tnt ~ ~ ~", "enabled": True},
    {"id": "finger-heart", "name": "Finger Heart", "emoji": "💝", "command": "/summon cow ~ ~ ~", "enabled": True},
]

state_lock = threading.RLock()
preset_store: dict | None = None
command_dispatch_lock: asyncio.Lock | None = None
last_command_monotonic = 0.0
ws_loop: asyncio.AbstractEventLoop | None = None
mc_socket = None
tiktok_client = None
tiktok_task: asyncio.Task | None = None
state = {
    "minecraft_connected": False,
    "minecraft_connected_at": None,
    "minecraft_last_event": None,
    "minecraft_last_command": None,
    "minecraft_last_error": None,
    "tiktok_status": "disconnected",
    "tiktok_username": "",
    "tiktok_detail": "Akun belum dihubungkan",
    "tiktok_last_error": None,
    "tiktok_room_id": None,
    "tiktok_recent_gifts": [],
    "tiktok_gift_catalog": [],
    "gift_catalog_status": "not_loaded",
    "gift_catalog_message": "Hubungkan TikTok LIVE untuk memuat katalog gift.",
    "started_at": time.time(),
}


def load_legacy_gifts() -> list[dict]:
    """Read the V1.2 gift file once when creating the first preset store."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not GIFTS_FILE.exists():
        return copy.deepcopy(DEFAULT_GIFTS)
    try:
        data = json.loads(GIFTS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read gifts.json during preset migration; using defaults")
    return copy.deepcopy(DEFAULT_GIFTS)


def _write_json_atomic(path: Path, data) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _clean_preset(raw: dict, fallback_name: str = "Preset") -> dict:
    gifts = raw.get("gifts", [])
    if not isinstance(gifts, list):
        gifts = []
    cleaned_gifts = []
    for gift in gifts[:100]:
        if not isinstance(gift, dict):
            continue
        name = str(gift.get("name", "")).strip()[:50]
        command_text = str(gift.get("command", "")).replace("\r", "").strip()[:5000]
        lines = [line.strip() for line in command_text.split("\n") if line.strip()]
        if not name or not lines or len(lines) > MAX_COMMANDS_PER_GIFT_RULE:
            continue
        if any(len(line) > 500 or any(ord(ch) < 32 for ch in line) for line in lines):
            continue
        command_text = "\n".join(line if line.startswith("/") else "/" + line for line in lines)
        cleaned_gifts.append({
            "id": str(gift.get("id") or uuid.uuid4().hex[:12])[:80],
            "gift_id": str(gift.get("gift_id") or "")[:40],
            "name": name,
            "emoji": str(gift.get("emoji") or "🎁")[:8] or "🎁",
            "command": command_text,
            "enabled": bool(gift.get("enabled", True)),
        })
    follow = raw.get("follow", {})
    if not isinstance(follow, dict):
        follow = {}
    try:
        cooldown = int(raw.get("cooldown_ms", DEFAULT_COMMAND_COOLDOWN_MS))
    except (TypeError, ValueError):
        cooldown = DEFAULT_COMMAND_COOLDOWN_MS
    cooldown = max(MIN_COMMAND_COOLDOWN_MS, min(MAX_COMMAND_COOLDOWN_MS, cooldown))
    command = str(follow.get("command", "")).strip()[:500]
    if command and not command.startswith("/"):
        command = "/" + command
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:12])[:80],
        "name": str(raw.get("name") or fallback_name).strip()[:40] or fallback_name,
        "gifts": cleaned_gifts,
        "follow": {"enabled": bool(follow.get("enabled", False)), "command": command},
        "cooldown_ms": cooldown,
    }


def ensure_preset_store() -> None:
    """Load or safely migrate V1.2 gifts.json into the new preset store."""
    global preset_store
    with state_lock:
        if preset_store is not None:
            return
        loaded = None
        if PRESETS_FILE.exists():
            try:
                loaded = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.exception("Could not read presets.json; rebuilding from the existing gifts.json")
        if isinstance(loaded, dict) and isinstance(loaded.get("presets"), list):
            presets = [_clean_preset(item) for item in loaded["presets"] if isinstance(item, dict)][:MAX_PRESETS]
            if presets:
                active_id = str(loaded.get("active_preset_id") or presets[0]["id"])
                if not any(item["id"] == active_id for item in presets):
                    active_id = presets[0]["id"]
                preset_store = {"active_preset_id": active_id, "presets": presets}
                return
        # First run after V1.2: keep all existing gift mappings in a Default preset.
        preset_store = {
            "active_preset_id": "default",
            "presets": [{
                "id": "default",
                "name": "Default",
                "gifts": load_legacy_gifts(),
                "follow": {"enabled": False, "command": ""},
                "cooldown_ms": DEFAULT_COMMAND_COOLDOWN_MS,
            }],
        }
        _write_json_atomic(PRESETS_FILE, preset_store)
        log.info("Created preset store; existing V1.2 gift mappings were preserved")


def _active_preset_locked() -> dict:
    ensure_preset_store()
    active_id = preset_store["active_preset_id"]
    return next((item for item in preset_store["presets"] if item["id"] == active_id), preset_store["presets"][0])


def _persist_preset_store_locked() -> None:
    _write_json_atomic(PRESETS_FILE, preset_store)
    # Keep gifts.json as a compatibility mirror for V1.2-era tools and backups.
    _write_json_atomic(GIFTS_FILE, _active_preset_locked()["gifts"])


def preset_snapshot() -> dict:
    ensure_preset_store()
    with state_lock:
        active = copy.deepcopy(_active_preset_locked())
        return {
            "active_preset_id": preset_store["active_preset_id"],
            "presets": [{"id": item["id"], "name": item["name"]} for item in preset_store["presets"]],
            "active": active,
            "max_presets": MAX_PRESETS,
        }


def get_gifts() -> list[dict]:
    ensure_preset_store()
    with state_lock:
        return copy.deepcopy(_active_preset_locked()["gifts"])


def save_gifts(gifts: list[dict]) -> None:
    ensure_preset_store()
    with state_lock:
        _active_preset_locked()["gifts"] = copy.deepcopy(gifts)
        _persist_preset_store_locked()


def get_follow_rule() -> dict:
    ensure_preset_store()
    with state_lock:
        return copy.deepcopy(_active_preset_locked()["follow"])


def get_cooldown_seconds() -> float:
    ensure_preset_store()
    with state_lock:
        return int(_active_preset_locked()["cooldown_ms"]) / 1000.0


def save_active_config(follow: dict, cooldown_ms: int) -> None:
    ensure_preset_store()
    with state_lock:
        active = _active_preset_locked()
        active["follow"] = copy.deepcopy(follow)
        active["cooldown_ms"] = cooldown_ms
        _persist_preset_store_locked()

def normalize_gift_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def command_payload(command: str) -> str:
    return json.dumps({
        "header": {
            "version": 1,
            "requestId": str(uuid.uuid4()),
            "messageType": "commandRequest",
            "messagePurpose": "commandRequest",
        },
        "body": {
            "version": 1,
            "origin": {"type": "player"},
            "overworld": "default",
            "commandLine": command,
        },
    }, ensure_ascii=False)


def subscribe_payload(event_name: str) -> str:
    return json.dumps({
        "header": {
            "version": 1,
            "requestId": str(uuid.uuid4()),
            "messageType": "commandRequest",
            "messagePurpose": "subscribe",
        },
        "body": {"eventName": event_name},
    })


async def send_minecraft_command_async(command: str) -> tuple[bool, str]:
    sock = mc_socket
    if sock is None:
        return False, f"Minecraft belum terhubung. Jalankan /connect ws://{get_lan_ip()}:{MC_PORT} di world."
    try:
        await sock.send(command_payload(command))
        return True, "Command dikirim ke koneksi Minecraft. Periksa world untuk memastikan hasilnya."
    except Exception as exc:
        return False, f"Gagal mengirim command: {exc}"


def send_minecraft_command(command: str) -> tuple[bool, str]:
    if ws_loop is None or not ws_loop.is_running():
        return False, "WebSocket server belum siap."
    future = asyncio.run_coroutine_threadsafe(send_minecraft_command_async(command), ws_loop)
    try:
        return future.result(timeout=5)
    except Exception as exc:
        return False, f"Bridge tidak merespons: {exc}"


def gift_command_lines(value: str) -> list[str]:
    """Return validated Minecraft commands from a newline-separated gift rule."""
    raw = str(value or "").replace("\r", "").strip()
    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    if not lines or len(lines) > MAX_COMMANDS_PER_GIFT_RULE:
        return []
    cleaned = []
    for line in lines:
        if len(line) > 500 or any(ord(ch) < 32 for ch in line):
            return []
        cleaned.append(line if line.startswith("/") else "/" + line)
    return cleaned


def expand_gift_placeholders(command: str, player_name: str, gift_name: str) -> str:
    """Replace the TikTok sender and gift placeholders without allowing newline injection."""
    def safe_value(value: str) -> str:
        return re.sub(r"[\r\n\x00-\x1f\x7f]", " ", str(value or "")).strip()[:80]
    return str(command).replace("PlayerName", safe_value(player_name)).replace("NameGift", safe_value(gift_name))


async def dispatch_live_commands(commands: list[str], event_name: str, sender: str) -> tuple[bool, str]:
    """Send a command group from one gift trigger, applying cooldown once per group."""
    global last_command_monotonic
    lock = command_dispatch_lock
    if lock is None:
        for command in commands:
            ok, message = await send_minecraft_command_async(command)
            if not ok:
                return False, message
        return True, f"{len(commands)} command dikirim."
    async with lock:
        cooldown = get_cooldown_seconds()
        now = time.monotonic()
        wait_for = cooldown - (now - last_command_monotonic)
        if last_command_monotonic and wait_for > 0:
            await asyncio.sleep(wait_for)
        sent_commands = []
        for command in commands:
            ok, message = await send_minecraft_command_async(command)
            if not ok:
                return False, message
            sent_commands.append(command)
        last_command_monotonic = time.monotonic()
        with state_lock:
            state["minecraft_last_command"] = {
                "event": event_name,
                "sender": sender,
                "command": "\n".join(sent_commands),
                "commands": sent_commands,
                "sent_at": time.time(),
                "ok": True,
            }
        return True, f"{len(sent_commands)} command dikirim dalam satu trigger."


async def dispatch_live_command(command: str, event_name: str, sender: str) -> tuple[bool, str]:
    """Serialize LIVE-triggered commands and apply the active preset cooldown."""
    global last_command_monotonic
    lock = command_dispatch_lock
    if lock is None:
        return await send_minecraft_command_async(command)
    async with lock:
        cooldown = get_cooldown_seconds()
        now = time.monotonic()
        wait_for = cooldown - (now - last_command_monotonic)
        if last_command_monotonic and wait_for > 0:
            await asyncio.sleep(wait_for)
        ok, message = await send_minecraft_command_async(command)
        last_command_monotonic = time.monotonic()
        if ok:
            with state_lock:
                state["minecraft_last_command"] = {
                    "event": event_name,
                    "sender": sender,
                    "command": command,
                    "sent_at": time.time(),
                    "ok": True,
                }
        return ok, message


async def minecraft_handler(socket):
    global mc_socket
    if ACTIVE_USER_ID is None:
        try:
            await socket.close(code=4001, reason="Login required")
        except Exception:
            pass
        return
    with state_lock:
        mc_socket = socket
        state["minecraft_connected"] = True
        state["minecraft_connected_at"] = time.time()
        state["minecraft_last_error"] = None
    log.info("Minecraft WebSocket connected")
    try:
        await socket.send(subscribe_payload("PlayerMessage"))
        async for message in socket:
            with state_lock:
                state["minecraft_last_event"] = time.time()
            if isinstance(message, bytes):
                continue
    except Exception as exc:
        log.warning("Minecraft WebSocket ended: %s", exc)
    finally:
        with state_lock:
            if mc_socket is socket:
                mc_socket = None
                state["minecraft_connected"] = False
        log.info("Minecraft WebSocket disconnected")


def add_recent_gift(entry: dict) -> None:
    with state_lock:
        events = state["tiktok_recent_gifts"]
        events.insert(0, entry)
        del events[MAX_RECENT_EVENTS:]


def update_recent_gift(event_id: str, **fields) -> None:
    with state_lock:
        for item in state["tiktok_recent_gifts"]:
            if item.get("id") == event_id:
                item.update(fields)
                return


async def process_tiktok_gift(event) -> None:
    gift_obj = getattr(event, "gift", None)
    if gift_obj is None:
        return

    # A streak produces intermediate messages. Only process the final frame.
    if bool(getattr(event, "streaking", False)):
        return

    gift_name = str(getattr(gift_obj, "name", "") or "Unknown gift").strip()
    user_obj = getattr(event, "user", None)
    sender = str(getattr(user_obj, "nickname", "Viewer") or "Viewer")
    username = str(getattr(user_obj, "unique_id", "") or "")
    try:
        quantity = max(1, int(getattr(event, "repeat_count", 1) or 1))
    except (TypeError, ValueError):
        quantity = 1

    event_gift_id = str(getattr(event, "gift_id", "") or getattr(gift_obj, "id", "") or "").strip()
    matching = next(
        (g for g in get_gifts() if g.get("enabled", True)
         and ((event_gift_id and str(g.get("gift_id", "")) == event_gift_id)
              or normalize_gift_name(g.get("name", "")) == normalize_gift_name(gift_name))),
        None,
    )
    event_id = str(uuid.uuid4())
    raw_commands = gift_command_lines(matching.get("command", "")) if matching else []
    record = {
        "id": event_id,
        "type": "gift",
        "time": time.time(),
        "gift": gift_name,
        "sender": sender,
        "username": username,
        "quantity": quantity,
        "status": "unmapped" if matching is None else ("received" if raw_commands else "failed"),
        "command": matching.get("command") if matching else None,
        "commands": raw_commands,
        "message": "Belum ada mapping untuk gift ini." if matching is None else "Gift diterima; memproses command.",
    }
    add_recent_gift(record)
    log.info("TikTok gift: %s sent %sx %s", username or sender, quantity, gift_name)

    if matching is None:
        return
    if not raw_commands:
        update_recent_gift(event_id, status="failed", message="Mapping gift tidak memiliki command yang valid.")
        return
    if not state.get("minecraft_connected"):
        update_recent_gift(event_id, status="minecraft_offline", message="Gift terdeteksi, tetapi Minecraft belum terhubung.")
        return

    execute_count = min(quantity, MAX_COMMANDS_PER_GIFT_EVENT)
    sent_groups = 0
    total_sent = 0
    for _ in range(execute_count):
        expanded = [expand_gift_placeholders(cmd, sender, gift_name) for cmd in raw_commands]
        ok, message = await dispatch_live_commands(expanded, f"Gift: {gift_name}", sender)
        if not ok:
            update_recent_gift(event_id, status="failed", message=message)
            with state_lock:
                state["minecraft_last_error"] = message
            return
        sent_groups += 1
        total_sent += len(expanded)

    note = f"{total_sent} command dikirim dalam {sent_groups} trigger gift."
    if quantity > execute_count:
        note += f" Dibatasi {MAX_COMMANDS_PER_GIFT_EVENT} repeat per event untuk mencegah spam."
    update_recent_gift(event_id, status="sent", message=note)


async def process_tiktok_follow(event) -> None:
    user_obj = getattr(event, "user", None)
    sender = str(getattr(user_obj, "nickname", "Viewer") or "Viewer")
    username = str(getattr(user_obj, "unique_id", "") or "")
    raw_id = (getattr(user_obj, "user_id", None) or getattr(user_obj, "id", None)
              or getattr(user_obj, "sec_uid", None) or username)
    viewer_key = str(raw_id or "").strip()
    creator = str(state.get("tiktok_username", "") or "").strip()

    # Persist before executing commands: unfollow/refollow, reconnects, and
    # server restarts must not cause the same viewer to trigger the mapping again.
    if not claim_follow_once(creator, viewer_key):
        log.info("TikTok follow ignored (already processed): @%s", username or sender)
        return

    follow = get_follow_rule()
    enabled = bool(follow.get("enabled")) and bool(str(follow.get("command", "")).strip())
    command = str(follow.get("command", "")).strip()
    event_id = str(uuid.uuid4())
    record = {
        "id": event_id, "type": "follow", "time": time.time(),
        "gift": "Follow", "sender": sender, "username": username,
        "quantity": 1,
        "status": "received" if enabled else "unmapped",
        "command": command or None,
        "message": "Follow pertama diterima; memproses command." if enabled else "Follow pertama terdeteksi, tetapi Follow → Command belum diaktifkan.",
    }
    add_recent_gift(record)
    log.info("TikTok first follow: %s", username or sender)

    if not enabled:
        return
    if not state.get("minecraft_connected"):
        update_recent_gift(event_id, status="minecraft_offline", message="Follow pertama diterima, tetapi Minecraft belum terhubung.")
        return

    ok, message = await dispatch_live_command(command, "Follow", sender)
    if ok:
        update_recent_gift(event_id, status="sent", message="Follow command dikirim ke Minecraft. Follow ulang dari pengguna ini tidak akan memicu mapping lagi.")
    else:
        update_recent_gift(event_id, status="failed", message=message)
        with state_lock:
            state["minecraft_last_error"] = message


def _clean_gift_catalog(payload) -> list[dict]:
    """Convert TikTokLive gift metadata into a small, safe UI catalog."""
    if isinstance(payload, dict):
        entries = payload.get("gifts", [])
    else:
        entries = []
    cleaned = []
    seen = set()
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, dict):
            continue
        gift_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        key = (gift_id, name.casefold())
        if key in seen:
            continue
        seen.add(key)
        try:
            diamonds = int(item.get("diamond_count", 0) or 0)
        except (TypeError, ValueError):
            diamonds = 0
        cleaned.append({"id": gift_id, "name": name[:80], "diamond_count": max(0, diamonds)})
    cleaned.sort(key=lambda item: item["name"].casefold())
    return cleaned


def _store_gift_catalog(payload, source: str = "TikTok LIVE") -> int:
    catalog = _clean_gift_catalog(payload)
    if catalog:
        with state_lock:
            state["tiktok_gift_catalog"] = catalog
            state["gift_catalog_status"] = "ready"
            state["gift_catalog_message"] = f"{len(catalog)} gift dimuat dari {source}."
        log.info("Loaded %s TikTok gifts from %s", len(catalog), source)
    return len(catalog)


async def _fetch_default_gift_catalog(client):
    """Fetch TikTok's current gift list even before the creator goes LIVE."""
    try:
        payload = await client.web.fetch_gift_list()
        count = _store_gift_catalog(payload, "katalog TikTok")
        if not count:
            with state_lock:
                state["gift_catalog_status"] = "empty"
                state["gift_catalog_message"] = "TikTok tidak mengembalikan daftar gift. Coba refresh lagi."
    except Exception as exc:
        log.warning("Could not fetch TikTok gift catalog: %s", exc)
        with state_lock:
            # Do not erase an already loaded catalog if a refresh fails.
            if not state.get("tiktok_gift_catalog"):
                state["gift_catalog_status"] = "error"
                state["gift_catalog_message"] = "Katalog gagal dimuat. Pastikan internet aktif lalu coba refresh."


async def run_tiktok_client(client, username: str):
    global tiktok_client, tiktok_task
    try:
        await client.connect(fetch_gift_info=True)
        with state_lock:
            if tiktok_client is client and state["tiktok_status"] != "stopping":
                state["tiktok_status"] = "disconnected"
                state["tiktok_detail"] = "Siaran LIVE berakhir atau koneksi ditutup."
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        log.warning("TikTok connection failed for @%s: %s", username, message)
        with state_lock:
            if tiktok_client is client:
                state["tiktok_status"] = "error"
                state["tiktok_detail"] = "Gagal terhubung ke TikTok LIVE."
                state["tiktok_last_error"] = message[:500]
    finally:
        with state_lock:
            if tiktok_client is client:
                tiktok_client = None
                tiktok_task = None
                if state["tiktok_status"] == "stopping":
                    state["tiktok_status"] = "disconnected"
                    state["tiktok_detail"] = "Koneksi TikTok dihentikan."


async def start_tiktok_connection(username: str) -> tuple[bool, str]:
    global tiktok_client, tiktok_task
    if tiktok_task is not None and not tiktok_task.done():
        return False, "Koneksi TikTok sedang aktif atau masih dalam proses. Putuskan dulu sebelum mengganti akun."

    try:
        from TikTokLive import TikTokLiveClient
        from TikTokLive.events import ConnectEvent, DisconnectEvent, FollowEvent, GiftEvent
    except Exception as exc:
        with state_lock:
            state["tiktok_status"] = "error"
            state["tiktok_detail"] = "Library TikTokLive belum siap."
            state["tiktok_last_error"] = f"{exc.__class__.__name__}: {exc}"
        return False, "Library TikTokLive belum terpasang. Di Termux jalankan: python -m pip install -r requirements.txt"

    try:
        client = TikTokLiveClient(unique_id=username)

        @client.on(ConnectEvent)
        async def _on_connect(event):
            with state_lock:
                if tiktok_client is client:
                    state["tiktok_status"] = "connected"
                    state["tiktok_detail"] = f"Terhubung ke LIVE @{username}"
                    state["tiktok_last_error"] = None
                    state["tiktok_room_id"] = getattr(client, "room_id", None)
            room_catalog = getattr(client, "gift_info", None)
            if isinstance(room_catalog, dict):
                _store_gift_catalog(room_catalog, "gift panel LIVE akun ini")
            log.info("TikTok LIVE connected: @%s", username)

        @client.on(DisconnectEvent)
        async def _on_disconnect(event):
            with state_lock:
                if tiktok_client is client and state["tiktok_status"] != "stopping":
                    state["tiktok_status"] = "disconnected"
                    state["tiktok_detail"] = "Koneksi TikTok terputus."

        @client.on(GiftEvent)
        async def _on_gift(event):
            await process_tiktok_gift(event)

        @client.on(FollowEvent)
        async def _on_follow(event):
            await process_tiktok_follow(event)

        tiktok_client = client
        with state_lock:
            state["gift_catalog_status"] = "loading"
            state["gift_catalog_message"] = "Sedang mengambil daftar gift TikTok…"
            state["tiktok_username"] = username
            state["tiktok_status"] = "connecting"
            state["tiktok_detail"] = f"Mencari siaran LIVE @{username}…"
            state["tiktok_last_error"] = None
            state["tiktok_room_id"] = None
        asyncio.create_task(_fetch_default_gift_catalog(client), name="VoxelLive-GiftCatalog")
        tiktok_task = asyncio.create_task(run_tiktok_client(client, username), name=f"TikTokLive-{username}")
        log.info("Starting TikTok LIVE listener for @%s", username)
        return True, f"Sedang mencari siaran LIVE @{username}. Status akan berubah jika koneksi berhasil."
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        with state_lock:
            state["tiktok_status"] = "error"
            state["tiktok_detail"] = "Gagal memulai konektor TikTok."
            state["tiktok_last_error"] = message[:500]
        return False, f"Gagal memulai konektor TikTok: {message}"


async def stop_tiktok_connection() -> tuple[bool, str]:
    global tiktok_client, tiktok_task
    client = tiktok_client
    task = tiktok_task
    if client is None and (task is None or task.done()):
        with state_lock:
            state["tiktok_status"] = "disconnected"
            state["tiktok_detail"] = "Akun belum dihubungkan."
        return True, "Koneksi TikTok sudah tidak aktif."

    with state_lock:
        state["tiktok_status"] = "stopping"
        state["tiktok_detail"] = "Memutuskan koneksi TikTok…"
    if client is not None:
        try:
            await client.disconnect()
        except Exception as exc:
            log.debug("TikTok disconnect note: %s", exc)
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(task, timeout=3)
        except (asyncio.TimeoutError, Exception):
            task.cancel()
    with state_lock:
        if tiktok_client is client:
            tiktok_client = None
            tiktok_task = None
        state["tiktok_status"] = "disconnected"
        state["tiktok_detail"] = "Koneksi TikTok dihentikan."
    return True, "Koneksi TikTok berhasil dihentikan."


async def close_minecraft_socket():
    global mc_socket
    sock = mc_socket
    if sock is not None:
        try:
            await sock.close()
        except Exception:
            pass
        if mc_socket is sock:
            mc_socket = None
    with state_lock:
        state["minecraft_connected"] = False


async def websocket_main():
    global ws_loop, command_dispatch_lock
    ws_loop = asyncio.get_running_loop()
    command_dispatch_lock = asyncio.Lock()
    log.info("Minecraft WebSocket listening at ws://%s:%s", MC_HOST, MC_PORT)
    async with serve(minecraft_handler, MC_HOST, MC_PORT, ping_interval=20, ping_timeout=20, max_size=1_000_000):
        await asyncio.Future()


class Handler(BaseHTTPRequestHandler):
    server_version = "VoxelLive/2.0"

    def log_message(self, fmt, *args):
        log.info("HTTP %s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, payload: dict | list):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 100_000:
            raise ValueError("Request terlalu besar")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Format JSON harus berupa object")
        return data

    def _run_async(self, coroutine, timeout=10):
        if ws_loop is None or not ws_loop.is_running():
            return False, "Event loop VoxelLive belum siap. Restart server.py lalu coba lagi."
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, ws_loop).result(timeout=timeout)
        except Exception as exc:
            return False, f"Permintaan tidak selesai: {exc}"

    def _token(self) -> str:
        value = self.headers.get("Authorization", "")
        return value[7:].strip() if value.lower().startswith("bearer ") else ""

    def _require_user(self, allow_activate: bool = False):
        user = validate_local_token(self._token())
        if not user:
            self._send_json(401, {"ok": False, "message": "Login diperlukan atau sesi sudah berakhir."})
            return None
        if ACTIVE_USER_ID is None and allow_activate:
            try:
                activate_local_user(user["id"])
            except Exception as exc:
                self._send_json(500, {"ok": False, "message": f"Gagal membuka profil lokal: {exc}"})
                return None
        if ACTIVE_USER_ID != user["id"]:
            self._send_json(409, {"ok": False, "message": "Akun lain sedang aktif di instalasi VoxelLive ini. Logout dari akun tersebut, lalu login kembali."})
            return None
        return user

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/me":
            user = self._require_user(allow_activate=True)
            if user:
                self._send_json(200, {"ok": True, "user": user})
            return
        if parsed.path.startswith("/api/") and parsed.path not in ("/api/health",):
            if not self._require_user():
                return
        if parsed.path == "/api/status":
            with state_lock:
                snapshot = dict(state)
                snapshot["tiktok_recent_gifts"] = list(state["tiktok_recent_gifts"])
            snapshot["uptime_seconds"] = int(time.time() - state["started_at"])
            snapshot["gift_count"] = len(get_gifts())
            snapshot["version"] = "2.0.0"
            snapshot["follow_enabled"] = bool(get_follow_rule().get("enabled"))
            lan_ip = get_lan_ip()
            snapshot["websocket_url"] = f"ws://{lan_ip}:{MC_PORT}"
            snapshot["minecraft_connect_command"] = f"/connect ws://{lan_ip}:{MC_PORT}"
            self._send_json(200, snapshot)
            return
        if parsed.path == "/api/config":
            self._send_json(200, preset_snapshot())
            return
        if parsed.path == "/api/gifts":
            self._send_json(200, get_gifts())
            return
        if parsed.path == "/api/gift-catalog":
            with state_lock:
                payload = {
                    "gifts": list(state.get("tiktok_gift_catalog", [])),
                    "status": state.get("gift_catalog_status", "not_loaded"),
                    "message": state.get("gift_catalog_message", "Katalog belum dimuat."),
                }
            self._send_json(200, payload)
            return
        if parsed.path == "/api/health":
            self._send_json(200, {"ok": True, "name": "VoxelLive", "version": "2.0.0"})
            return

        relative = "index.html" if parsed.path in ("/", "") else unquote(parsed.path.lstrip("/"))
        candidate = (WEB_DIR / relative).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or candidate.suffix in (".js", ".json", ".svg"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"ok": False, "message": str(exc)})
            return

        if parsed.path == "/api/access/request":
            try:
                result = cloud_api_call("/api/access/request", data)
                code = int(result.pop("http_status", 202 if result.get("ok") else 400))
                self._send_json(code, result)
            except Exception as exc:
                self._send_json(503, {"ok": False, "message": str(exc)})
            return

        if parsed.path == "/api/access/status":
            try:
                result = cloud_api_call("/api/access/status", data)
                code = int(result.pop("http_status", 200 if result.get("ok") else 400))
                if result.get("ok") and result.get("status") == "approved" and result.get("user"):
                    activate_local_user(result["user"]["id"])
                self._send_json(code, result)
            except Exception as exc:
                self._send_json(503, {"ok": False, "message": str(exc)})
            return

        if parsed.path == "/api/auth/register":
            try:
                result = cloud_api_call("/api/auth/register", data)
                code = int(result.pop("http_status", 202 if result.get("ok") else 400))
                self._send_json(code, result)
            except Exception as exc:
                self._send_json(503, {"ok": False, "message": str(exc)})
            return

        if parsed.path == "/api/auth/login":
            try:
                result = cloud_api_call("/api/auth/login", data)
                code = int(result.pop("http_status", 200 if result.get("ok") else 401))
                if result.get("ok") and isinstance(result.get("user"), dict):
                    activate_local_user(result["user"]["id"])
                    # Only the browser stores the opaque access token; never persist it in this project folder.
                self._send_json(code, result)
            except Exception as exc:
                self._send_json(503, {"ok": False, "message": str(exc)})
            return

        user = None
        if parsed.path.startswith("/api/") and parsed.path not in ("/api/health",):
            user = self._require_user()
            if not user:
                return

        if parsed.path == "/api/auth/logout":
            token = self._token()
            try:
                cloud_api_call("/api/auth/logout", {}, token)
            except Exception:
                pass
            AUTH_CACHE.pop(token, None)
            deactivate_local_user(user["id"] if user else None)
            self._send_json(200, {"ok": True, "message": "Berhasil logout."})
            return

        if parsed.path == "/api/presets/share":
            try:
                preset = copy.deepcopy(_active_preset_locked())
                result = cloud_api_call("/api/presets/share", {"preset": preset}, self._token())
                code = int(result.pop("http_status", 201 if result.get("ok") else 400))
                self._send_json(code, result)
            except Exception as exc:
                self._send_json(503, {"ok": False, "message": str(exc)})
            return

        if parsed.path == "/api/presets/import":
            code_value = str(data.get("code", "")).strip().upper()
            if not re.fullmatch(r"VXL-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", code_value):
                self._send_json(400, {"ok": False, "message": "Format kode preset tidak valid."})
                return
            try:
                result = cloud_api_call("/api/presets/import", {"code": code_value}, self._token())
                status_code = int(result.pop("http_status", 200 if result.get("ok") else 404))
                if not result.get("ok"):
                    self._send_json(status_code, result)
                    return
                incoming = _clean_preset(result.get("preset", {}), "Preset Import")
                ensure_preset_store()
                with state_lock:
                    presets = preset_store["presets"]
                    if len(presets) >= MAX_PRESETS:
                        self._send_json(400, {"ok": False, "message": f"Maksimal {MAX_PRESETS} preset."})
                        return
                    incoming["id"] = uuid.uuid4().hex[:12]
                    incoming["name"] = incoming["name"][:31] + " (Import)"
                    presets.append(incoming)
                    preset_store["active_preset_id"] = incoming["id"]
                    _persist_preset_store_locked()
                self._send_json(200, {"ok": True, "config": preset_snapshot(), "message": f'Preset "{incoming["name"]}" berhasil diimpor.'})
            except Exception as exc:
                self._send_json(503, {"ok": False, "message": str(exc)})
            return

        if parsed.path == "/api/presets":
            action = str(data.get("action", "")).strip().lower()
            ensure_preset_store()
            with state_lock:
                presets = preset_store["presets"]
                active = _active_preset_locked()
                if action == "create":
                    name = str(data.get("name", "")).strip()[:40]
                    if not name:
                        self._send_json(400, {"ok": False, "message": "Nama preset tidak boleh kosong."})
                        return
                    if len(presets) >= MAX_PRESETS:
                        self._send_json(400, {"ok": False, "message": f"Maksimal {MAX_PRESETS} preset."})
                        return
                    new_id = uuid.uuid4().hex[:12]
                    new_preset = copy.deepcopy(active)
                    new_preset.update({"id": new_id, "name": name})
                    presets.append(new_preset)
                    preset_store["active_preset_id"] = new_id
                    _persist_preset_store_locked()
                elif action == "rename":
                    name = str(data.get("name", "")).strip()[:40]
                    if not name:
                        self._send_json(400, {"ok": False, "message": "Nama preset tidak boleh kosong."})
                        return
                    target_id = str(data.get("id") or preset_store["active_preset_id"])
                    target = next((item for item in presets if item["id"] == target_id), None)
                    if target is None:
                        self._send_json(404, {"ok": False, "message": "Preset tidak ditemukan."})
                        return
                    target["name"] = name
                    _persist_preset_store_locked()
                elif action == "switch":
                    target_id = str(data.get("id", ""))
                    if not any(item["id"] == target_id for item in presets):
                        self._send_json(404, {"ok": False, "message": "Preset tidak ditemukan."})
                        return
                    preset_store["active_preset_id"] = target_id
                    _persist_preset_store_locked()
                elif action == "delete":
                    target_id = str(data.get("id") or preset_store["active_preset_id"])
                    if len(presets) <= 1:
                        self._send_json(400, {"ok": False, "message": "Preset terakhir tidak bisa dihapus."})
                        return
                    target_index = next((i for i, item in enumerate(presets) if item["id"] == target_id), None)
                    if target_index is None:
                        self._send_json(404, {"ok": False, "message": "Preset tidak ditemukan."})
                        return
                    was_active = preset_store["active_preset_id"] == target_id
                    presets.pop(target_index)
                    if was_active:
                        preset_store["active_preset_id"] = presets[min(target_index, len(presets)-1)]["id"]
                    _persist_preset_store_locked()
                else:
                    self._send_json(400, {"ok": False, "message": "Aksi preset tidak dikenal."})
                    return
            self._send_json(200, {"ok": True, "config": preset_snapshot()})
            return

        if parsed.path == "/api/config":
            follow = data.get("follow", {})
            if not isinstance(follow, dict):
                self._send_json(400, {"ok": False, "message": "Pengaturan Follow tidak valid."})
                return
            command = str(follow.get("command", "")).strip()[:500]
            if "\n" in command or "\r" in command:
                self._send_json(400, {"ok": False, "message": "Command tidak boleh berisi baris baru."})
                return
            if command and not command.startswith("/"):
                command = "/" + command
            enabled = bool(follow.get("enabled", False))
            if enabled and not command:
                self._send_json(400, {"ok": False, "message": "Isi command Follow terlebih dahulu atau matikan Follow Mapping."})
                return
            try:
                cooldown_ms = int(data.get("cooldown_ms", DEFAULT_COMMAND_COOLDOWN_MS))
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "message": "Cooldown harus berupa angka milidetik."})
                return
            if not MIN_COMMAND_COOLDOWN_MS <= cooldown_ms <= MAX_COMMAND_COOLDOWN_MS:
                self._send_json(400, {"ok": False, "message": f"Cooldown harus {MIN_COMMAND_COOLDOWN_MS}–{MAX_COMMAND_COOLDOWN_MS} ms."})
                return
            save_active_config({"enabled": enabled, "command": command}, cooldown_ms)
            self._send_json(200, {"ok": True, "config": preset_snapshot(), "message": "Pengaturan Follow dan cooldown tersimpan di preset aktif."})
            return

        if parsed.path == "/api/test-follow":
            follow = get_follow_rule()
            if not follow.get("enabled") or not str(follow.get("command", "")).strip():
                self._send_json(400, {"ok": False, "message": "Aktifkan Follow Mapping dan isi command terlebih dahulu."})
                return
            command = str(follow["command"])
            ok, message = send_minecraft_command(command)
            with state_lock:
                state["minecraft_last_command"] = {"event": "Follow test", "command": command, "sent_at": time.time(), "ok": ok}
            self._send_json(200 if ok else 409, {"ok": ok, "message": message, "command": command})
            return

        if parsed.path == "/api/gift-catalog/refresh":
            client = tiktok_client
            if client is None:
                self._send_json(409, {"ok": False, "message": "Hubungkan username TikTok terlebih dahulu agar katalog dapat diambil."})
                return
            with state_lock:
                state["gift_catalog_status"] = "loading"
                state["gift_catalog_message"] = "Memperbarui katalog gift TikTok…"
            if ws_loop is None or not ws_loop.is_running():
                self._send_json(503, {"ok": False, "message": "Event loop VoxelLive belum siap. Restart server.py lalu coba lagi."})
                return
            asyncio.run_coroutine_threadsafe(_fetch_default_gift_catalog(client), ws_loop)
            self._send_json(202, {"ok": True, "message": "Permintaan refresh katalog dimulai."})
            return

        if parsed.path == "/api/gifts":
            gifts = data.get("gifts")
            if not isinstance(gifts, list) or len(gifts) > 100:
                self._send_json(400, {"ok": False, "message": "Daftar gift tidak valid (maksimal 100)."})
                return
            cleaned = []
            seen = set()
            for item in gifts:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()[:50]
                raw_command = str(item.get("command", "")).replace("\r", "").strip()[:5000]
                command_lines = [line.strip() for line in raw_command.split("\n") if line.strip()]
                emoji = str(item.get("emoji", "🎁")).strip()[:8] or "🎁"
                gift_id = str(item.get("gift_id", "")).strip()[:40]
                gid = str(item.get("id", "")).strip()[:80] or str(uuid.uuid4())[:8]
                if (not name or not command_lines or len(command_lines) > MAX_COMMANDS_PER_GIFT_RULE or gid in seen
                    or any(len(line) > 500 or any(ord(ch) < 32 for ch in line) for line in command_lines)):
                    continue
                command = "\n".join(line if line.startswith("/") else "/" + line for line in command_lines)
                seen.add(gid)
                cleaned.append({"id": gid, "gift_id": gift_id, "name": name, "emoji": emoji, "command": command, "enabled": bool(item.get("enabled", True))})
            with state_lock:
                save_gifts(cleaned)
            self._send_json(200, {"ok": True, "gifts": cleaned})
            return

        if parsed.path == "/api/test":
            gift_id = str(data.get("gift_id", ""))
            gift = next((g for g in get_gifts() if g["id"] == gift_id), None)
            if not gift:
                self._send_json(404, {"ok": False, "message": "Gift tidak ditemukan."})
                return
            if not gift.get("enabled", True):
                self._send_json(400, {"ok": False, "message": "Mapping gift sedang dinonaktifkan."})
                return
            commands = [expand_gift_placeholders(cmd, "VoxelLiveTest", gift["name"]) for cmd in gift_command_lines(gift.get("command", ""))]
            if not commands:
                self._send_json(400, {"ok": False, "message": "Mapping gift tidak memiliki command yang valid."})
                return
            if ws_loop is None or not ws_loop.is_running():
                self._send_json(409, {"ok": False, "message": "WebSocket server belum siap."})
                return
            future = asyncio.run_coroutine_threadsafe(dispatch_live_commands(commands, f"Gift test: {gift['name']}", "VoxelLiveTest"), ws_loop)
            try:
                ok, message = future.result(timeout=15)
            except Exception as exc:
                ok, message = False, f"Test command gagal: {exc}"
            with state_lock:
                state["minecraft_last_command"] = {"gift": gift["name"], "command": "\n".join(commands), "commands": commands, "sent_at": time.time(), "ok": ok}
            self._send_json(200 if ok else 409, {"ok": ok, "message": message, "command": "\n".join(commands), "commands": commands})
            return

        if parsed.path == "/api/tiktok/connect":
            username = str(data.get("username", "")).strip().lstrip("@").strip()[:50]
            if not username:
                self._send_json(400, {"ok": False, "message": "Masukkan username TikTok dulu."})
                return
            if not re.fullmatch(r"[A-Za-z0-9._]+", username):
                self._send_json(400, {"ok": False, "message": "Username hanya boleh berisi huruf, angka, titik, dan garis bawah."})
                return
            ok, message = self._run_async(start_tiktok_connection(username), timeout=12)
            self._send_json(200 if ok else 409, {"ok": ok, "message": message})
            return

        if parsed.path == "/api/tiktok/disconnect":
            ok, message = self._run_async(stop_tiktok_connection(), timeout=8)
            self._send_json(200 if ok else 500, {"ok": ok, "message": message})
            return

        self._send_json(404, {"ok": False, "message": "Endpoint tidak ditemukan."})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def http_thread_main():
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    log.info("Dashboard ready at http://%s:%s", HTTP_HOST, HTTP_PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


_android_server_thread = None

def start_background(data_dir=None):
    """Start the backend inside the Android foreground service."""
    global _android_server_thread, BASE_DATA_DIR, DATA_DIR, GIFTS_FILE, PRESETS_FILE, preset_store
    if data_dir:
        BASE_DATA_DIR = Path(data_dir)
        DATA_DIR = BASE_DATA_DIR
        GIFTS_FILE = DATA_DIR / "gifts.json"
        PRESETS_FILE = DATA_DIR / "presets.json"
        preset_store = None
    if _android_server_thread and _android_server_thread.is_alive():
        return "VoxelLive sudah berjalan."
    _android_server_thread = threading.Thread(target=main, name="VoxelLive-Main", daemon=True)
    _android_server_thread.start()
    return "VoxelLive sedang dimulai."

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_preset_store()
    http_thread = threading.Thread(target=http_thread_main, name="VoxelLive-HTTP", daemon=True)
    http_thread.start()
    try:
        asyncio.run(websocket_main())
    except KeyboardInterrupt:
        log.info("VoxelLive stopped")


if __name__ == "__main__":
    main()
