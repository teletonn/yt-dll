#!/usr/bin/env python3
"""yt-dll — Web UI очередь загрузок yt-dlp (только stdlib, без внешних зависимостей).

- HTTP: 0.0.0.0:49977, без авторизации (LAN).
- Скачивание строго последовательно (один воркер-поток).
- По умолчанию: 720p, русская аудиодорожка, без субтитров, merge в mp4.
- Имена файлов: "%(title)s [%(id)s].%(ext)s" — кириллица сохраняется.
"""
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VIDEOS_DIR = os.path.join(BASE_DIR, "videos")
STATIC_DIR = os.path.join(BASE_DIR, "static")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
YTDLP = shutil.which("yt-dlp") or "yt-dlp"

HOST = "0.0.0.0"
PORT = 49977

VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/|/v/)([A-Za-z0-9_-]{11})"
)
PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
PROGRESS_FULL_RE = re.compile(
    r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?\s*(\S+).*?at\s+(\S+).*?ETA\s+(\S+)"
)

DEFAULT_SETTINGS = {
    "resolution": "720",          # 360/480/720/1080/2160/best
    "prefer_ru_audio": True,      # русская аудиодорожка приоритетом
    "no_subs": True,              # субтитры не качаем (фиксировано)
    "proxy_enabled": False,
    "proxy_url": "",
    "use_cookies": False,         # использовать cookies.txt при скачивании
    "cookies_browser": "chromium",
}

RESOLUTION_HEIGHT = {
    "360": 360, "480": 480, "720": 720, "1080": 1080, "2160": 2160,
}

lock = threading.RLock()

state = {
    "queue": [],        # [{id,url,title,status,progress,speed,eta,size,stage,added_at,error}]
    "downloaded": [],   # [{id,url,title,filepath,filesize,downloaded_at,duration}]
    "playlists": [],    # [{id,name,items:[downloaded_id],created_at}]
    "settings": dict(DEFAULT_SETTINGS),
    "current_id": None,
}
worker_event = threading.Event()


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


# ---------- persistence ----------

def load_state():
    global state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in ("queue", "downloaded", "playlists"):
            if isinstance(data.get(k), list):
                state[k] = data[k]
        if isinstance(data.get("settings"), dict):
            s = dict(DEFAULT_SETTINGS)
            s.update(data["settings"])
            state["settings"] = s
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {"queue": state["queue"], "downloaded": state["downloaded"],
             "playlists": state["playlists"], "settings": state["settings"]},
            f, ensure_ascii=False, indent=2,
        )
    os.replace(tmp, STATE_FILE)


# ---------- helpers ----------

def extract_video_id(url):
    m = VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def new_id(prefix):
    return f"{prefix}_{int(time.time()*1000)}_{os.urandom(3).hex()}"


def format_selector(height):
    if height is None:  # best
        return "bv*+ba/b"
    return f"bv*[height<={height}]+ba/b[height<={height}]/b"


def format_sort(prefer_ru):
    # res ограничиваем высотой через selector; sort: русская дорожка -> качество
    if prefer_ru:
        return "lang:ru,en,res,ext:mp4:m4a:vcodec:h264,acodec:aac"
    return "res,ext:mp4:m4a:vcodec:h264,acodec:aac"


def base_cmd(extra=None):
    """Базовые флаги yt-dlp с учётом настроек (прокси/куки)."""
    s = state["settings"]
    cmd = [YTDLP, "--no-playlist", "--no-colors", "--no-warnings",
           "--no-write-subs", "--no-write-auto-subs",
           "--socket-timeout", "25"]
    if s.get("proxy_enabled") and s.get("proxy_url", "").strip():
        cmd += ["--proxy", s["proxy_url"].strip()]
    else:
        cmd += ["--proxy", ""]  # прямое подключение хоста (с учётом VPN)
    if s.get("use_cookies") and os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    if extra:
        cmd += extra
    return cmd


def known_ids():
    ids = set()
    for q in state["queue"]:
        if q.get("video_id"):
            ids.add(q["video_id"])
    for d in state["downloaded"]:
        if d.get("video_id"):
            ids.add(d["video_id"])
    # файлы в videos/ вида "Название [VIDEOID].mp4"
    try:
        for fn in os.listdir(VIDEOS_DIR):
            m = re.search(r"\[([A-Za-z0-9_-]{11})\]", fn)
            if m:
                ids.add(m.group(1))
    except FileNotFoundError:
        pass
    return ids


def find_queue(qid):
    for q in state["queue"]:
        if q["id"] == qid:
            return q
    return None


def scan_videos_dir():
    """Файлы в videos/, которых нет в state['downloaded'] — подхватить."""
    try:
        files = sorted(os.listdir(VIDEOS_DIR))
    except FileNotFoundError:
        return []
    known_files = {d.get("filepath") for d in state["downloaded"]}
    out = []
    for fn in files:
        if fn.startswith(".") or not fn.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
            continue
        if fn in known_files:
            continue
        full = os.path.join(VIDEOS_DIR, fn)
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        m = re.search(r"\[([A-Za-z0-9_-]{11})\]", fn)
        title = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]\.[^.]+$", "", fn)
        out.append({
            "id": "ext_" + (m.group(1) if m else fn),
            "video_id": m.group(1) if m else None,
            "url": "",
            "title": title or fn,
            "filepath": fn,
            "filesize": size,
            "downloaded_at": int(os.path.getmtime(full)),
            "duration": None,
            "external": True,
        })
    return out


# ---------- yt-dlp operations ----------

def fetch_info(url, timeout=120):
    """Метаданные без скачивания. Возвращает dict или {'error': ...}."""
    cmd = base_cmd(["--dump-single-json", "--skip-download", url])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "таймаут получения информации"}
    except Exception as e:
        return {"error": str(e)}
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return {"error": err[-1] if err else f"yt-dlp exit {p.returncode}"}
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"error": "не удалось разобрать ответ yt-dlp"}
    return {
        "video_id": data.get("id"),
        "title": data.get("title"),
        "duration": data.get("duration"),
        "uploader": data.get("uploader") or data.get("channel"),
        "thumbnail": data.get("thumbnail"),
    }


def export_cookies(browser, timeout=150):
    """Экспорт cookies из браузера через yt-dlp в cookies.txt."""
    cmd = [YTDLP, "--cookies-from-browser", browser,
           "--cookies", COOKIES_FILE,
           "--skip-download", "--no-playlist", "--no-warnings",
           "--print", "title", "https://www.youtube.com/"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "таймаут экспорта cookies"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        return {"ok": True, "message": f"cookies экспортированы ({browser})"}
    err = (p.stderr or "").strip().splitlines()
    return {"ok": False,
            "error": (err[-1] if err else "cookies не получены") +
            " — откройте YouTube в браузере и залогиньтесь"}


def download_item(item):
    """Скачивание одного элемента очереди (вызывается из воркера)."""
    s = state["settings"]
    height = RESOLUTION_HEIGHT.get(str(s.get("resolution", "720")))
    out_tmpl = os.path.join(VIDEOS_DIR, "%(title)s [%(id)s].%(ext)s")
    cmd = base_cmd([
        "--newline", "--progress",
        "--format", format_selector(height),
        "--format-sort", format_sort(bool(s.get("prefer_ru_audio", True))),
        "--merge-output-format", "mp4",
        "--output", out_tmpl,
        "--print", "after_move:filepath",
        item["url"],
    ])
    log("START", item["url"])
    item["status"] = "downloading"
    item["stage"] = "Загрузка…"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    final_path = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith("[download]"):
                if "has already been" in line:
                    item["stage"] = "Уже загружено"
                elif "Destination:" in line:
                    item["stage"] = "Загрузка…"
                elif "Merging" in line or "Merger" in line:
                    item["stage"] = "Склейка видео+аудио…"
                    item["progress"] = 99.0
                else:
                    m = PROGRESS_FULL_RE.search(line)
                    if m:
                        item["progress"] = float(m.group(1))
                        item["size"] = m.group(2)
                        item["speed"] = m.group(3)
                        item["eta"] = m.group(4)
                        item["stage"] = "Загрузка…"
                    else:
                        m2 = PROGRESS_RE.search(line)
                        if m2:
                            item["progress"] = float(m2.group(1))
            elif line.startswith(("[Merger", "[ExtractAudio", "[MoveFiles", "[Fixup")):
                item["stage"] = "Обработка (merge/remux)…"
                item["progress"] = max(item.get("progress", 0), 99.0)
            elif line.startswith("ERROR"):
                item["error"] = line[:500]
            elif os.path.sep in line and re.search(r"\.(mp4|mkv|webm|mov)$", line):
                # строка after_move:filepath
                final_path = line.strip()
        proc.wait()
    except Exception as e:
        try:
            proc.kill()
        except OSError:
            pass
        item["status"] = "error"
        item["error"] = str(e)[:500]
        return

    if proc.returncode == 0:
        # найти итоговый файл: сначала after_move, иначе по video_id в папке
        filepath = None
        if final_path and os.path.exists(final_path):
            filepath = os.path.basename(final_path)
        else:
            vid = item.get("video_id")
            try:
                for fn in os.listdir(VIDEOS_DIR):
                    if vid and vid in fn:
                        filepath = fn
                        break
            except FileNotFoundError:
                pass
        full = os.path.join(VIDEOS_DIR, filepath) if filepath else None
        entry = {
            "id": new_id("dl"),
            "video_id": item.get("video_id"),
            "url": item["url"],
            "title": item.get("title") or item["url"],
            "filepath": filepath,
            "filesize": os.path.getsize(full) if full and os.path.exists(full) else 0,
            "downloaded_at": int(time.time()),
            "duration": item.get("duration"),
        }
        with lock:
            state["downloaded"].insert(0, entry)
            state["queue"] = [q for q in state["queue"] if q["id"] != item["id"]]
            state["current_id"] = None
            save_state()
        log("DONE", entry["title"])
    else:
        item["status"] = "error"
        if not item.get("error"):
            item["error"] = f"yt-dlp завершился с кодом {proc.returncode}"
        with lock:
            state["current_id"] = None
            save_state()
        log("ERROR", item["url"], item.get("error"))


def worker():
    while True:
        worker_event.wait(timeout=1.0)
        with lock:
            nxt = None
            for q in state["queue"]:
                if q["status"] in ("queued", "error") and q.get("auto_retry"):
                    nxt = q
                    break
            if nxt is None:
                for q in state["queue"]:
                    if q["status"] == "queued":
                        nxt = q
                        break
            if nxt is not None:
                nxt["status"] = "downloading"
                nxt["progress"] = 0.0
                state["current_id"] = nxt["id"]
                save_state()
            else:
                state["current_id"] = None
        if nxt is None:
            worker_event.clear()
            continue
        download_item(nxt)


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "yt-dll/1.0"

    def log_message(self, *a):
        pass

    # -- helpers --
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- static --
    def serve_static(self, path):
        if path == "/" or path == "":
            path = "/index.html"
        # /videos/<file> — видеофайлы с поддержкой Range
        if path.startswith("/videos/"):
            return self.serve_video(urllib.parse.unquote(path[len("/videos/"):]))
        rel = urllib.parse.unquote(path.lstrip("/"))
        if ".." in rel or rel.startswith("/"):
            return self.send_error(403)
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            # SPA fallback
            full = os.path.join(STATIC_DIR, "index.html")
            if not os.path.isfile(full):
                return self.send_error(404)
        ctype, _ = mimetypes.guess_type(full)
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", (ctype or "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def serve_video(self, name):
        if "/" in name or ".." in name:
            return self.send_error(403)
        full = os.path.join(VIDEOS_DIR, name)
        if not os.path.isfile(full):
            return self.send_error(404)
        ctype, _ = mimetypes.guess_type(full)
        size = os.path.getsize(full)
        range_h = self.headers.get("Range")
        try:
            f = open(full, "rb")
        except OSError:
            return self.send_error(404)
        try:
            if range_h:
                m = re.match(r"bytes=(\d*)-(\d*)", range_h)
                if m:
                    s = int(m.group(1)) if m.group(1) else 0
                    e = int(m.group(2)) if m.group(2) else size - 1
                    e = min(e, size - 1)
                    if s >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    length = e - s + 1
                    self.send_response(206)
                    self.send_header("Content-Type", ctype or "video/mp4")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {s}-{e}/{size}")
                    self.send_header("Content-Length", str(length))
                    self.end_headers()
                    f.seek(s)
                    shutil.copyfileobj(f, self.wfile, length)
                    return
            self.send_response(200)
            self.send_header("Content-Type", ctype or "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            shutil.copyfileobj(f, self.wfile)
        finally:
            f.close()

    # -- API --
    def api_state(self):
        with lock:
            externals = scan_videos_dir()
            self.send_json({
                "queue": state["queue"],
                "downloaded": state["downloaded"] + externals,
                "playlists": state["playlists"],
                "settings": state["settings"],
                "current_id": state["current_id"],
                "videos_dir": VIDEOS_DIR,
                "cookies_present": os.path.exists(COOKIES_FILE),
                "now": int(time.time()),
            })

    def api_add(self, data):
        url = (data.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return self.send_json({"ok": False, "error": "Нужна http(s) ссылка"}, 400)
        vid = extract_video_id(url)
        with lock:
            urls = {q.get("url") for q in state["queue"]} | \
                   {d.get("url") for d in state["downloaded"]}
            if (vid and vid in known_ids()) or url in urls:
                return self.send_json({"ok": False, "error": "Это видео уже в очереди или загружено",
                                        "duplicate": True}, 409)
            item = {"id": new_id("q"), "url": url, "video_id": vid,
                    "title": data.get("title") or url,
                    "duration": data.get("duration"),
                    "status": "queued", "progress": 0.0, "speed": "",
                    "eta": "", "size": "", "stage": "В очереди",
                    "added_at": int(time.time()), "error": ""}
            state["queue"].append(item)
            save_state()
        worker_event.set()
        # обогатить названием в фоне (не блокируем ответ)
        threading.Thread(target=self._enrich_title, args=(item["id"], url), daemon=True).start()
        with lock:
            self.send_json({"ok": True, "item": item})

    def _enrich_title(self, qid, url):
        info = fetch_info(url)
        if "error" in info:
            return
        with lock:
            q = find_queue(qid)
            if q and (q["title"] == q["url"] or not q.get("title")):
                q["title"] = info.get("title") or q["title"]
                q["duration"] = info.get("duration")
                if info.get("video_id"):
                    q["video_id"] = info["video_id"]
                save_state()

    def api_move(self, data):
        qid = data.get("id")
        dest = data.get("to")  # индекс либо 'up'/'down'/'top'
        with lock:
            q = find_queue(qid)
            if not q:
                return self.send_json({"ok": False, "error": "не найдено"}, 404)
            if q["status"] == "downloading":
                return self.send_json({"ok": False, "error": "загружается прямо сейчас"}, 409)
            qs = [x for x in state["queue"] if x["status"] != "downloading"]
            others = [x for x in state["queue"] if x["status"] != "downloading"]
            try:
                i = others.index(q)
            except ValueError:
                return self.send_json({"ok": False, "error": "не найдено"}, 404)
            others.pop(i)
            if dest == "up":
                dest = max(0, i - 1)
            elif dest == "down":
                dest = min(len(others), i + 1)
            elif dest == "top":
                dest = 0
            try:
                dest = max(0, min(len(others), int(dest)))
            except (TypeError, ValueError):
                return self.send_json({"ok": False, "error": "плохой индекс"}, 400)
            others.insert(dest, q)
            # собрать обратно: downloading первым(ые), затем переупорядоченные
            downloading = [x for x in state["queue"] if x["status"] == "downloading"]
            state["queue"] = downloading + others
            save_state()
            self.send_json({"ok": True})

    def api_remove(self, data):
        qid = data.get("id")
        with lock:
            q = find_queue(qid)
            if not q:
                return self.send_json({"ok": False, "error": "не найдено"}, 404)
            if q["status"] == "downloading":
                return self.send_json({"ok": False, "error": "загружается прямо сейчас"}, 409)
            state["queue"] = [x for x in state["queue"] if x["id"] != qid]
            save_state()
            self.send_json({"ok": True})

    def api_retry(self, data):
        qid = data.get("id")
        with lock:
            q = find_queue(qid)
            if not q:
                return self.send_json({"ok": False, "error": "не найдено"}, 404)
            q["status"] = "queued"
            q["progress"] = 0.0
            q["error"] = ""
            q["stage"] = "В очереди"
            q.pop("auto_retry", None)
            save_state()
        worker_event.set()
        self.send_json({"ok": True})

    def api_settings(self, data):
        with lock:
            s = state["settings"]
            if "resolution" in data and str(data["resolution"]) in list(RESOLUTION_HEIGHT) + ["best"]:
                s["resolution"] = str(data["resolution"])
            for k in ("prefer_ru_audio", "proxy_enabled", "use_cookies"):
                if k in data:
                    s[k] = bool(data[k])
            if "proxy_url" in data:
                s["proxy_url"] = str(data["proxy_url"] or "").strip()
            if "cookies_browser" in data and str(data["cookies_browser"]) in \
                    ("chromium", "chrome", "brave", "firefox", "edge", "opera", "vivaldi"):
                s["cookies_browser"] = str(data["cookies_browser"])
            save_state()
            self.send_json({"ok": True, "settings": s})

    def api_delete_file(self, data):
        name = data.get("filepath") or ""
        if "/" in name or ".." in name:
            return self.send_json({"ok": False, "error": "плохое имя"}, 400)
        full = os.path.join(VIDEOS_DIR, name)
        with lock:
            state["downloaded"] = [d for d in state["downloaded"] if d.get("filepath") != name]
            for pl in state["playlists"]:
                pl["items"] = [i for i in pl["items"]
                               if not (isinstance(i, dict) and i.get("filepath") == name) and i != name]
            save_state()
        try:
            if os.path.exists(full):
                os.remove(full)
        except OSError as e:
            return self.send_json({"ok": False, "error": str(e)}, 500)
        self.send_json({"ok": True})

    def api_playlist(self, action, data):
        with lock:
            if action == "create":
                name = (data.get("name") or "").strip()[:80] or "Без названия"
                pl = {"id": new_id("pl"), "name": name, "items": [],
                      "created_at": int(time.time())}
                state["playlists"].append(pl)
                save_state()
                return self.send_json({"ok": True, "playlist": pl})
            pl = next((p for p in state["playlists"] if p["id"] == data.get("playlist_id")), None)
            if not pl:
                return self.send_json({"ok": False, "error": "плейлист не найден"}, 404)
            if action == "rename":
                pl["name"] = (data.get("name") or "").strip()[:80] or pl["name"]
            elif action == "delete":
                state["playlists"] = [p for p in state["playlists"] if p["id"] != pl["id"]]
            elif action == "add":
                did = data.get("downloaded_id")
                if did and did not in pl["items"]:
                    pl["items"].append(did)
            elif action == "remove":
                pl["items"] = [i for i in pl["items"] if i != data.get("downloaded_id")]
            elif action == "move":
                try:
                    it = pl["items"].pop(pl["items"].index(data.get("downloaded_id")))
                    pl["items"].insert(max(0, min(len(pl["items"]), int(data.get("to", 0)))), it)
                except (ValueError, TypeError):
                    return self.send_json({"ok": False, "error": "элемент не найден"}, 404)
            save_state()
            return self.send_json({"ok": True, "playlist": pl})

    # -- routing --
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/api/state":
            return self.api_state()
        if path == "/api/info":
            url = (qs.get("url") or [""])[0]
            if not url:
                return self.send_json({"ok": False, "error": "нужен ?url="}, 400)
            info = fetch_info(url)
            if "error" in info:
                return self.send_json({"ok": False, **info})
            vid = info.get("video_id")
            with lock:
                dup = bool(vid and vid in known_ids())
            return self.send_json({"ok": True, "duplicate": dup, **info})
        return self.serve_static(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        data = self.read_json()
        if path == "/api/add":
            return self.api_add(data)
        if path == "/api/queue/move":
            return self.api_move(data)
        if path == "/api/queue/remove":
            return self.api_remove(data)
        if path == "/api/queue/retry":
            return self.api_retry(data)
        if path == "/api/settings":
            return self.api_settings(data)
        if path == "/api/cookies/export":
            browser = (data.get("browser") or state["settings"].get("cookies_browser") or "chromium")
            res = export_cookies(browser)
            return self.send_json(res, 200 if res.get("ok") else 500)
        if path == "/api/cookies/clear":
            try:
                if os.path.exists(COOKIES_FILE):
                    os.remove(COOKIES_FILE)
            except OSError as e:
                return self.send_json({"ok": False, "error": str(e)}, 500)
            with lock:
                state["settings"]["use_cookies"] = False
                save_state()
            return self.send_json({"ok": True})
        if path == "/api/files/delete":
            return self.api_delete_file(data)
        if path.startswith("/api/playlists/"):
            return self.api_playlist(path.rsplit("/", 1)[-1], data)
        return self.send_json({"ok": False, "error": "unknown endpoint"}, 404)


def main():
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)
    load_state()
    for q in state["queue"]:
        if q.get("status") == "downloading":
            q["status"] = "queued"  # сброс зависшего после рестарта
            q["progress"] = 0.0
    save_state()
    threading.Thread(target=worker, daemon=True).start()
    worker_event.set()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"yt-dll on http://{HOST}:{PORT}  videos={VIDEOS_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
