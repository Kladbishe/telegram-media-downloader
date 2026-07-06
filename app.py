import asyncio
import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)
from telethon.utils import get_extension

app = Flask(__name__)

CREDENTIALS_FILE = Path(".credentials.json")

def load_credentials():
    if CREDENTIALS_FILE.exists():
        return json.loads(CREDENTIALS_FILE.read_text())
    return {}

def save_credentials(api_id, api_hash):
    CREDENTIALS_FILE.write_text(json.dumps({"api_id": api_id, "api_hash": api_hash}))

# Persistent loop keeps the Telethon client alive between send_code and sign_in (2FA support)
_auth_loop = asyncio.new_event_loop()
threading.Thread(target=_auth_loop.run_forever, daemon=True).start()

class _AuthState:
    client = None
    phone = None
    phone_code_hash = None

_auth = _AuthState()

def run_auth(coro):
    return asyncio.run_coroutine_threadsafe(coro, _auth_loop).result(timeout=30)

# StringSession avoids concurrent SQLite writes when multiple threads create clients
_session_str: str = ""

def _make_session():
    return StringSession(_session_str) if _session_str else "session"

def _export_session(client: TelegramClient) -> str:
    ss = StringSession()
    ss.set_dc(client.session.dc_id, client.session.server_address, client.session.port)
    ss.auth_key = client.session.auth_key
    return ss.save()

_photo_cache: dict = {}
_entity_cache: dict = {}

_download_queue: Queue = Queue()
_stop_event = threading.Event()
_active = False

def is_video(document):
    return (getattr(document, "mime_type", "") or "").startswith("video/")

def is_gif(document):
    if (getattr(document, "mime_type", "") or "") == "image/gif":
        return True
    return any(isinstance(a, DocumentAttributeAnimated) for a in getattr(document, "attributes", []))

def is_round(document):
    return any(
        isinstance(a, DocumentAttributeVideo) and getattr(a, "round_message", False)
        for a in getattr(document, "attributes", [])
    )

def make_filename(message):
    date_str = message.date.strftime("%Y%m%d_%H%M%S")
    media = message.media
    if isinstance(media, MessageMediaPhoto):
        ext = ".jpg"
    elif isinstance(media, MessageMediaDocument):
        ext = get_extension(media.document) or ".bin"
    else:
        ext = ".bin"
    return f"{date_str}_{message.id}{ext}"

async def run_download(api_id, api_hash, chat, output_dir, dl_photos, dl_videos, dl_rounds, limit, queue, stop_event, cached_entity=None, date_from=None, date_to=None, organize_by_month=False):
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    async with TelegramClient(_make_session(), api_id, api_hash) as client:
        try:
            entity = cached_entity if cached_entity is not None else await client.get_entity(chat)
        except Exception as e:
            queue.put({"type": "error", "msg": str(e)})
            return

        chat_name = (getattr(entity, "title", None)
                     or getattr(entity, "username", None)
                     or str(chat))
        queue.put({"type": "chat_name", "name": chat_name})

        date_to_end = date_to + timedelta(days=1) if date_to else None

        to_download = []
        n_photos = n_videos = 0
        scanned = 0
        async for message in client.iter_messages(entity, wait_time=0, offset_date=date_to_end):
            if stop_event.is_set():
                break
            scanned += 1
            if scanned % 100 == 0:
                queue.put({"type": "scanning", "scanned": scanned, "found": len(to_download)})
            if date_to_end and message.date >= date_to_end:
                continue
            if date_from and message.date < date_from:
                break
            media = message.media
            if isinstance(media, MessageMediaPhoto):
                if dl_photos:
                    n_photos += 1
                    to_download.append(message)
            elif isinstance(media, MessageMediaDocument):
                doc = media.document
                if is_gif(doc):
                    pass
                elif is_round(doc):
                    if dl_rounds:
                        n_videos += 1
                        to_download.append(message)
                elif is_video(doc) and dl_videos:
                    n_videos += 1
                    to_download.append(message)
            if limit and len(to_download) >= limit:
                break

        total_found = len(to_download)
        queue.put({"type": "scan", "photos": n_photos, "videos": n_videos, "total": total_found})

        if total_found == 0:
            queue.put({"type": "finish", "no_media": True, "dl": 0, "sk": 0, "total": 0, "path": str(output_path.resolve())})
            return

        dl_count = [0]
        sk_count = [0]
        sem = asyncio.Semaphore(3)

        async def dl_one(message, idx):
            if stop_event.is_set():
                return
            filename = make_filename(message)
            if organize_by_month:
                dest = output_path / message.date.strftime("%Y-%m") / filename
            else:
                dest = output_path / filename
            part = Path(str(dest) + ".part")

            if dest.exists():
                sk_count[0] += 1
                queue.put({"type": "skip", "msg": filename})
                return

            dest.parent.mkdir(parents=True, exist_ok=True)

            async with sem:
                if stop_event.is_set():
                    return
                queue.put({"type": "progress", "msg": filename, "current": idx + 1, "total": total_found})
                try:
                    last_pct = [-1]
                    def on_progress(received, total, _fn=filename):
                        if not total:
                            return
                        pct = int(received * 100 / total)
                        if pct - last_pct[0] >= 1:
                            last_pct[0] = pct
                            queue.put({"type": "dl_progress", "msg": _fn, "pct": pct})

                    await asyncio.wait_for(
                        client.download_media(message.media, file=str(dest), progress_callback=on_progress),
                        timeout=600,
                    )
                    size_kb = dest.stat().st_size // 1024
                    dl_count[0] += 1
                    queue.put({"type": "done", "msg": filename, "size": size_kb, "count": dl_count[0],
                               "current": idx + 1, "total": total_found})
                except asyncio.TimeoutError:
                    dest.unlink(missing_ok=True)
                    part.unlink(missing_ok=True)
                    queue.put({"type": "error", "msg": f"{filename}: timeout (>10 min)"})
                except Exception as e:
                    dest.unlink(missing_ok=True)
                    part.unlink(missing_ok=True)
                    queue.put({"type": "error", "msg": f"{filename}: {e}"})

        await asyncio.gather(*[dl_one(msg, i) for i, msg in enumerate(to_download)])

        queue.put({
            "type": "finish",
            "dl": dl_count[0],
            "sk": sk_count[0],
            "total": total_found,
            "path": str(output_path.resolve()),
        })

def download_thread(api_id, api_hash, chat, output_dir, dl_photos, dl_videos, dl_rounds, limit, queue, stop_event, cached_entity=None, date_from=None, date_to=None, organize_by_month=False):
    asyncio.run(run_download(api_id, api_hash, chat, output_dir, dl_photos, dl_videos, dl_rounds, limit, queue, stop_event, cached_entity, date_from, date_to, organize_by_month))


@app.route("/")
def index():
    creds = load_credentials()
    api_id = str(creds.get("api_id", ""))
    api_hash = str(creds.get("api_hash", ""))
    has_creds = bool(api_id and api_hash)
    return render_template("index.html", api_id=api_id, api_hash=api_hash, has_creds=has_creds)


@app.route("/auth/status")
def auth_status():
    creds = load_credentials()
    if not creds.get("api_id") or not creds.get("api_hash"):
        return jsonify({"ok": True, "authenticated": False})

    async def check():
        global _session_str
        c = TelegramClient("session", creds["api_id"], creds["api_hash"])
        await c.connect()
        try:
            authed = await c.is_user_authorized()
            if authed and not _session_str:
                _session_str = _export_session(c)
            return authed
        finally:
            await c.disconnect()

    try:
        return jsonify({"ok": True, "authenticated": run_auth(check())})
    except Exception as e:
        return jsonify({"ok": True, "authenticated": False, "error": str(e)})


@app.route("/auth/send-code", methods=["POST"])
def auth_send_code():
    creds = load_credentials()
    phone = str(request.json.get("phone", "")).strip()
    if not phone:
        return jsonify({"ok": False, "error": "Phone required"})

    async def send():
        if _auth.client:
            try:
                await _auth.client.disconnect()
            except Exception:
                pass
        _auth.client = TelegramClient("session", creds["api_id"], creds["api_hash"])
        await _auth.client.connect()
        sent = await _auth.client.send_code_request(phone)
        _auth.phone = phone
        _auth.phone_code_hash = sent.phone_code_hash

    try:
        run_auth(send())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/auth/verify-code", methods=["POST"])
def auth_verify_code():
    code = str(request.json.get("code", "")).strip()
    password = str(request.json.get("password", "")).strip()

    if not _auth.client or not _auth.phone:
        return jsonify({"ok": False, "error": "Request a code first"})

    async def verify():
        global _session_str
        try:
            await _auth.client.sign_in(_auth.phone, code, phone_code_hash=_auth.phone_code_hash)
        except SessionPasswordNeededError:
            if not password:
                return {"needs_2fa": True}
            await _auth.client.sign_in(password=password)

        _session_str = _export_session(_auth.client)

        try:
            await _auth.client.disconnect()
        except Exception:
            pass
        _auth.client = None
        _auth.phone = None
        _auth.phone_code_hash = None
        return {"ok": True}

    try:
        return jsonify(run_auth(verify()))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/save-creds", methods=["POST"])
def save_creds_route():
    data = request.json
    api_id_raw = str(data.get("api_id", "")).strip()
    api_hash = str(data.get("api_hash", "")).strip()
    if api_id_raw.isdigit() and api_hash:
        save_credentials(int(api_id_raw), api_hash)
    return jsonify({"ok": True})


@app.route("/chats")
def get_chats():
    creds = load_credentials()
    if not creds.get("api_id") or not creds.get("api_hash"):
        return jsonify({"ok": False, "error": "No credentials"})

    result = []
    err = []

    async def fetch():
        try:
            async with TelegramClient(_make_session(), creds["api_id"], creds["api_hash"]) as client:
                async for dialog in client.iter_dialogs(limit=300):
                    ent = dialog.entity
                    if getattr(ent, "megagroup", False):
                        kind = "group"
                    elif getattr(ent, "broadcast", False):
                        kind = "channel"
                    elif getattr(ent, "title", None):
                        kind = "group"
                    else:
                        kind = "private"
                    photo = getattr(ent, "photo", None)
                    has_photo = (
                        photo is not None
                        and not type(photo).__name__.endswith("Empty")
                        and (hasattr(photo, "photo_small") or hasattr(photo, "photo_id"))
                    )
                    _entity_cache[dialog.id] = ent
                    result.append({
                        "id": dialog.id,
                        "name": dialog.name or "Unknown",
                        "kind": kind,
                        "username": getattr(ent, "username", None),
                        "has_photo": has_photo,
                    })
        except Exception as e:
            err.append(str(e))

    done = threading.Event()
    def run():
        try:
            asyncio.run(fetch())
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    done.wait(timeout=30)

    if err:
        return jsonify({"ok": False, "error": err[0]})
    return jsonify({"ok": True, "chats": result})


@app.route("/chat-photo/<int:chat_id>")
def chat_photo(chat_id):
    if chat_id in _photo_cache:
        data = _photo_cache[chat_id]
        return (Response(data, mimetype="image/jpeg") if data else Response(status=404))

    creds = load_credentials()
    photo_data = [None]

    async def fetch():
        try:
            async with TelegramClient(_make_session(), creds["api_id"], creds["api_hash"]) as client:
                entity = _entity_cache.get(chat_id) or await client.get_entity(chat_id)
                buf = BytesIO()
                result = await client.download_profile_photo(entity, file=buf)
                if result:
                    photo_data[0] = buf.getvalue()
        except Exception:
            pass

    done = threading.Event()
    def run():
        try:
            asyncio.run(fetch())
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    done.wait(timeout=10)

    _photo_cache[chat_id] = photo_data[0]
    if photo_data[0]:
        return Response(photo_data[0], mimetype="image/jpeg")
    return Response(status=404)


@app.route("/browse-folder")
def browse_folder():
    try:
        script = 'POSIX path of (choose folder with prompt "Select download folder")'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"ok": True, "path": ""})
        path = result.stdout.strip().rstrip("/")
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/start", methods=["POST"])
def start():
    global _active, _stop_event, _download_queue

    if _active:
        return jsonify({"ok": False, "error": "Download already running"})

    data = request.json
    api_id_raw = str(data.get("api_id", "")).strip()
    api_hash = str(data.get("api_hash", "")).strip()
    chat = str(data.get("chat", "")).strip()
    output_dir = str(data.get("output_dir", "downloads")).strip() or "downloads"
    dl_photos = bool(data.get("photos", True))
    dl_videos = bool(data.get("videos", True))
    dl_rounds = bool(data.get("rounds", False))
    organize_by_month = bool(data.get("organize_by_month", False))
    limit_raw = data.get("limit", "")
    limit = int(limit_raw) if str(limit_raw).strip().isdigit() else None
    # tz_offset: JS getTimezoneOffset() — minutes, negative for UTC+
    # Adding it converts UTC midnight → local midnight in UTC
    try:
        tz_offset = int(data.get("tz_offset", 0))
    except (ValueError, TypeError):
        tz_offset = 0
    tz_delta = timedelta(minutes=tz_offset)

    date_from_str = str(data.get("date_from", "") or "").strip()
    date_from = None
    if date_from_str:
        try:
            date_from = datetime.strptime(date_from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + tz_delta
        except ValueError:
            pass
    date_to_str = str(data.get("date_to", "") or "").strip()
    date_to = None
    if date_to_str:
        try:
            date_to = datetime.strptime(date_to_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + tz_delta
        except ValueError:
            pass

    if not api_id_raw.isdigit():
        return jsonify({"ok": False, "error": "API ID must be a number"})
    if not api_hash or not chat:
        return jsonify({"ok": False, "error": "Missing fields"})

    api_id = int(api_id_raw)
    save_credentials(api_id, api_hash)

    chat_id_int = int(chat) if chat.lstrip("-").isdigit() else None
    cached_entity = _entity_cache.get(chat_id_int) if chat_id_int else None

    _stop_event = threading.Event()
    _download_queue = Queue()
    _active = True

    threading.Thread(
        target=download_thread,
        args=(api_id, api_hash, chat, output_dir, dl_photos, dl_videos, dl_rounds, limit, _download_queue, _stop_event, cached_entity, date_from, date_to, organize_by_month),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/stop", methods=["POST"])
def stop():
    global _active
    _stop_event.set()
    _active = False
    return jsonify({"ok": True})


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    global _session_str
    _session_str = ""
    for f in ["session.session", "session.session-journal"]:
        p = Path(f)
        if p.exists():
            p.unlink()
    return jsonify({"ok": True})


@app.route("/stream")
def stream():
    def generate():
        global _active
        while True:
            try:
                event = _download_queue.get(timeout=1)
                yield f"data: {json.dumps(event)}\n\n"
                if event["type"] == "finish":
                    _active = False
                    break
            except Empty:
                if not _active:
                    break
                yield 'data: {"type":"ping"}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=False, port=5055)
