from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import edge_tts
import imageio_ffmpeg
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LIBRARY_DIR = DATA_DIR / "library"
TEMP_DIR = DATA_DIR / "temp"
INDEX_FILE = DATA_DIR / "library.json"
DATABASE_FILE = DATA_DIR / "users.db"
SECRET_FILE = DATA_DIR / ".session_secret"
MAX_TEXT_LENGTH = 5000
ALLOWED_UPLOADS = {"wav", "mp3", "m4a", "aac", "ogg", "flac", "webm", "mp4"}

for directory in (DATA_DIR, LIBRARY_DIR, TEMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)
if not INDEX_FILE.exists():
    INDEX_FILE.write_text("[]", encoding="utf-8")

app = Flask(__name__)
if not SECRET_FILE.exists():
    SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
app.secret_key = os.environ.get("IVR_SECRET_KEY") or SECRET_FILE.read_text(encoding="utf-8").strip()
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

VOICES = {
    "en-US-AriaNeural": {"language": "English (US)", "gender": "Female"},
    "en-US-GuyNeural": {"language": "English (US)", "gender": "Male"},
    "en-IN-NeerjaNeural": {"language": "English (India)", "gender": "Female"},
    "en-IN-PrabhatNeural": {"language": "English (India)", "gender": "Male"},
    "hi-IN-SwaraNeural": {"language": "Hindi", "gender": "Female"},
    "hi-IN-MadhurNeural": {"language": "Hindi", "gender": "Male"},
    "bn-IN-TanishaaNeural": {"language": "Bengali", "gender": "Female"},
    "ta-IN-PallaviNeural": {"language": "Tamil", "gender": "Female"},
    "te-IN-ShrutiNeural": {"language": "Telugu", "gender": "Female"},
    "mr-IN-AarohiNeural": {"language": "Marathi", "gender": "Female"},
    "gu-IN-DhwaniNeural": {"language": "Gujarati", "gender": "Female"},
    "kn-IN-SapnaNeural": {"language": "Kannada", "gender": "Female"},
    "ml-IN-SobhanaNeural": {"language": "Malayalam", "gender": "Female"},
    "ur-IN-GulNeural": {"language": "Urdu", "gender": "Female"},
    "ne-NP-HemkalaNeural": {"language": "Nepali", "gender": "Female"},
}

FORMATS = {
    "wav8": {"ext": "wav", "args": ["-ar", "8000", "-ac", "1", "-c:a", "pcm_s16le"]},
    "wav16": {"ext": "wav", "args": ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"]},
    "wav8ulaw": {"ext": "wav", "args": ["-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw"]},
    "gsm": {"ext": "gsm", "args": ["-ar", "8000", "-ac", "1", "-c:a", "libgsm"]},
    "mp3": {"ext": "mp3", "args": ["-ar", "44100", "-ac", "1", "-b:a", "128k"]},
}


def database():
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with database() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE, password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, user_id INTEGER, username TEXT, event_type TEXT NOT NULL, detail TEXT, created TEXT NOT NULL)")
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            password = os.environ.get("IVR_ADMIN_PASSWORD")
            if not password: raise RuntimeError("IVR_ADMIN_PASSWORD must be set for the first administrator")
            connection.execute("INSERT OR IGNORE INTO users (username,password_hash,is_admin,created) VALUES (?,?,1,?)", (os.environ.get("IVR_ADMIN_USER", "admin"), generate_password_hash(password), datetime.now(timezone.utc).isoformat()))
initialize_database()


def record_event(event_type, detail=""):
    with database() as connection:
        connection.execute("INSERT INTO events (user_id,username,event_type,detail,created) VALUES (?,?,?,?,?)", (session.get("user_id"), session.get("username"), event_type, detail, datetime.now(timezone.utc).isoformat()))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return (jsonify(error="Please sign in."), 401) if request.path.startswith("/api/") else redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        return view(*args, **kwargs) if session.get("is_admin") else (jsonify(error="Administrator access is required."), 403)
    return wrapped

def safe_name(value: str, fallback: str = "ivr_message") -> str:
    value = secure_filename(value.strip())
    value = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", value)
    return value[:80] or fallback


def library_items() -> list[dict]:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_library(items: list[dict]) -> None:
    temporary = INDEX_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(INDEX_FILE)


def convert_audio(source: Path, destination: Path, format_key: str) -> None:
    spec = FORMATS[format_key]
    command = [shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), *spec["args"], str(destination)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Audio conversion failed")


async def synthesize(text: str, voice: str, rate: str, pitch: str, output: Path) -> None:
    communicator = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    await communicator.save(str(output))


@app.get("/")
@login_required
def index():
    return render_template("index.html", voices=VOICES, username=session.get("username"), is_admin=session.get("is_admin"))


@app.post("/api/generate")
@login_required
def generate():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    voice = str(payload.get("voice", ""))
    format_key = str(payload.get("format", "wav8"))
    rate = str(payload.get("rate", "+0%"))
    pitch = str(payload.get("pitch", "+0Hz"))
    if not text:
        return jsonify(error="Enter an IVR script first."), 400
    if len(text) > MAX_TEXT_LENGTH:
        return jsonify(error=f"Script is limited to {MAX_TEXT_LENGTH} characters."), 400
    if voice not in VOICES or format_key not in FORMATS:
        return jsonify(error="Invalid voice or output format."), 400
    if not re.fullmatch(r"[+-](?:[0-9]|[1-9][0-9]|100)%", rate):
        return jsonify(error="Invalid speed value."), 400
    if not re.fullmatch(r"[+-](?:[0-9]|[1-9][0-9]|100)Hz", pitch):
        return jsonify(error="Invalid pitch value."), 400

    item_id = uuid.uuid4().hex
    basename = safe_name(str(payload.get("name", "")))
    spec = FORMATS[format_key]
    target_name = f"{basename}_{item_id[:8]}.{spec['ext']}"
    source = TEMP_DIR / f"{item_id}.mp3"
    target = LIBRARY_DIR / target_name
    try:
        asyncio.run(synthesize(text, voice, rate, pitch, source))
        convert_audio(source, target, format_key)
    except Exception as exc:
        target.unlink(missing_ok=True)
        return jsonify(error=f"Voice generation failed: {exc}"), 502
    finally:
        source.unlink(missing_ok=True)

    record_event("audio_created", basename)
    item = {"owner": session.get("username"), "id": item_id, "name": basename, "filename": target_name, "voice": voice, "format": format_key, "created": datetime.now(timezone.utc).isoformat(), "text": text[:240]}
    items = library_items()
    items.insert(0, item)
    save_library(items)
    return jsonify(item=item, audioUrl=f"/api/audio/{item_id}", downloadUrl=f"/api/audio/{item_id}?download=1")


@app.post("/api/convert")
@login_required
def convert():
    upload = request.files.get("audio")
    format_key = request.form.get("format", "wav8")
    if not upload or not upload.filename:
        return jsonify(error="Choose an audio file first."), 400
    extension = Path(upload.filename).suffix.lower().lstrip(".")
    if extension not in ALLOWED_UPLOADS or format_key not in FORMATS:
        return jsonify(error="Unsupported input file or output format."), 400
    item_id = uuid.uuid4().hex
    basename = safe_name(Path(upload.filename).stem, "converted_audio")
    source = TEMP_DIR / f"{item_id}.{extension}"
    spec = FORMATS[format_key]
    target_name = f"{basename}_{item_id[:8]}.{spec['ext']}"
    target = LIBRARY_DIR / target_name
    upload.save(source)
    try:
        convert_audio(source, target, format_key)
    except Exception as exc:
        target.unlink(missing_ok=True)
        return jsonify(error=f"Conversion failed: {exc}"), 422
    finally:
        source.unlink(missing_ok=True)
    record_event("audio_created", basename)
    item = {"owner": session.get("username"), "id": item_id, "name": basename, "filename": target_name, "voice": "Imported", "format": format_key, "created": datetime.now(timezone.utc).isoformat(), "text": "Imported audio"}
    items = library_items()
    items.insert(0, item)
    save_library(items)
    return jsonify(item=item, audioUrl=f"/api/audio/{item_id}", downloadUrl=f"/api/audio/{item_id}?download=1")


@app.get("/api/library")
@login_required
def library():
    items = library_items()
    if request.args.get("scope") == "history":
        if not session.get("is_admin"): items = [x for x in items if x.get("owner") == session.get("username")]
    else:
        items = [x for x in items if x.get("owner") == session.get("username")][:1]
    return jsonify(items=items)


@app.get("/api/audio/<item_id>")
@login_required
def audio(item_id: str):
    item = next((entry for entry in library_items() if entry.get("id") == item_id), None)
    if not item:
        return jsonify(error="Audio not found."), 404
    path = LIBRARY_DIR / Path(item["filename"]).name
    if not path.is_file():
        return jsonify(error="Audio file is missing."), 404
    if request.args.get("download") == "1": record_event("download", item["filename"])
    return send_file(path, as_attachment=request.args.get("download") == "1", download_name=item["filename"], conditional=True)


@app.delete("/api/library/<item_id>")
@login_required
def delete_audio(item_id: str):
    items = library_items()
    item = next((entry for entry in items if entry.get("id") == item_id), None)
    if not item:
        return jsonify(error="Audio not found."), 404
    (LIBRARY_DIR / Path(item["filename"]).name).unlink(missing_ok=True)
    save_library([entry for entry in items if entry.get("id") != item_id])
    return jsonify(ok=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET": return redirect(url_for("index")) if session.get("user_id") else render_template("login.html")
    with database() as connection: user = connection.execute("SELECT * FROM users WHERE username=? AND is_active=1", (request.form.get("username", "").strip(),)).fetchone()
    if not user or not check_password_hash(user["password_hash"], request.form.get("password", "")): return render_template("login.html", error="Invalid username or password."), 401
    session.clear(); session.update(user_id=user["id"], username=user["username"], is_admin=bool(user["is_admin"])); record_event("login")
    return redirect(url_for("index"))

@app.post("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.get("/users")
@admin_required
def users_page(): return render_template("users.html", is_admin=True)

@app.route("/api/users", methods=["GET", "POST"])
@admin_required
def users_api():
    with database() as connection:
        if request.method == "GET": return jsonify(users=[dict(r) for r in connection.execute("SELECT id,username,is_admin,is_active,created FROM users ORDER BY username")])
        p=request.get_json(silent=True) or {}; username=str(p.get("username","")).strip(); password=str(p.get("password",""))
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}",username): return jsonify(error="Invalid username."),400
        if len(password)<8: return jsonify(error="Password needs at least 8 characters."),400
        try: cursor=connection.execute("INSERT INTO users (username,password_hash,is_admin,created) VALUES (?,?,?,?)",(username,generate_password_hash(password),int(bool(p.get("is_admin"))),datetime.now(timezone.utc).isoformat()))
        except sqlite3.IntegrityError: return jsonify(error="Username already exists."),409
        return jsonify(id=cursor.lastrowid,ok=True),201

@app.patch("/api/users/<int:user_id>")
@admin_required
def update_user(user_id):
    p=request.get_json(silent=True) or {}; updates=[]; values=[]
    if user_id==session.get("user_id") and p.get("is_active") is False: return jsonify(error="You cannot disable your own account."),400
    if p.get("password"):
        if len(str(p["password"]))<8:return jsonify(error="Password needs at least 8 characters."),400
        updates.append("password_hash=?");values.append(generate_password_hash(str(p["password"])))
    if "is_active" in p:updates.append("is_active=?");values.append(int(bool(p["is_active"])))
    if not updates:return jsonify(error="No changes supplied."),400
    values.append(user_id)
    with database() as connection: result=connection.execute("UPDATE users SET "+", ".join(updates)+" WHERE id=?",values)
    return jsonify(ok=True) if result.rowcount else (jsonify(error="User not found."),404)

@app.get("/dashboard")
@login_required
def dashboard_page(): return render_template("dashboard.html", is_admin=session.get("is_admin"))
@app.get("/history")
@login_required
def history_page(): return render_template("history.html", is_admin=session.get("is_admin"))
@app.get("/profile")
@login_required
def profile_page(): return render_template("profile.html", is_admin=session.get("is_admin"), username=session.get("username"))
@app.get("/api/dashboard")
@login_required
def dashboard_api():
    today = date.today()
    audio = library_items()
    daily_counts = {(today - timedelta(days=offset)).isoformat(): 0 for offset in range(6, -1, -1)}
    for item in audio:
        day = str(item.get("created", ""))[:10]
        if day in daily_counts:
            daily_counts[day] += 1
    with database() as connection:
        logins = connection.execute("SELECT COUNT(*) FROM events WHERE event_type='login'").fetchone()[0]
    disk = shutil.disk_usage(BASE_DIR)
    memory_percent = 0
    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0])
        memory_percent = round((1 - meminfo.get("MemAvailable", 0) / meminfo["MemTotal"]) * 100, 1)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        pass
    try:
        processes = sum(1 for entry in Path("/proc").iterdir() if entry.name.isdigit())
    except OSError:
        processes = 0
    cpu_count = os.cpu_count() or 1
    try:
        cpu_percent = round(min(100, os.getloadavg()[0] / cpu_count * 100), 1)
    except (AttributeError, OSError):
        cpu_percent = 0
    metrics = {
        "cpu": {"value": cpu_percent, "unit": "% load", "detail": f"{cpu_count} CPU cores"},
        "logins": {"value": logins, "unit": "events", "detail": "Successful portal sign-ins"},
        "today_audio": {"value": daily_counts[today.isoformat()], "unit": "files", "detail": "Created today"},
        "total_audio": {"value": len(audio), "unit": "files", "detail": "Stored on server"},
        "processes": {"value": processes, "unit": "running", "detail": "Server processes"},
        "ram": {"value": memory_percent, "unit": "% used", "detail": "System memory"},
        "disk": {"value": round(disk.used / disk.total * 100, 1), "unit": "% used", "detail": f"{disk.free / 1073741824:.1f} GB free"},
        "status": {"value": "Online", "unit": "healthy", "detail": "IVR service available"},
    }
    daily = [{"day": day, "count": count} for day, count in daily_counts.items()]
    return jsonify(metrics=metrics, daily=daily)
@app.post("/api/profile/password")
@login_required
def profile_password():
    p=request.get_json(silent=True) or {}; old=str(p.get("current_password","")); new=str(p.get("new_password",""))
    if len(new)<8:return jsonify(error="New password needs at least 8 characters."),400
    with database() as c:
        user=c.execute("SELECT password_hash FROM users WHERE id=?",(session["user_id"],)).fetchone()
        if not user or not check_password_hash(user["password_hash"],old):return jsonify(error="Current password is incorrect."),400
        c.execute("UPDATE users SET password_hash=? WHERE id=?",(generate_password_hash(new),session["user_id"]))
    record_event("password_changed");return jsonify(ok=True)

@app.get("/api/profile/activity")
@login_required
def profile_activity():
    with database() as c:
        rows=c.execute("SELECT event_type,detail,created FROM events WHERE user_id=? AND event_type IN ('login','logout') ORDER BY id DESC LIMIT 100",(session["user_id"],)).fetchall()
    return jsonify(events=[dict(r) for r in rows])

@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="File is larger than 50 MB."), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
