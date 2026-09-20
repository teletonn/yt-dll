#!/usr/bin/env python3
"""yt-dll — Web UI очередь загрузок yt-dlp (только stdlib, без внешних зависимостей).

- HTTP: 0.0.0.0:49977, без авторизации (LAN).
- Скачивание строго последовательно (один воркер-поток).
- По умолчанию: 720p, русская аудиодорожка, без субтитров, merge в mp4.
- Имена файлов: "%(title)s [%(id)s].%(ext)s" — кириллица сохраняется.
- Пауза: после (пере)запуска всегда paused=true; воркер стоит, пока пользователь
  не снимет паузу в UI. Пауза убивает текущий yt-dlp, .part остаётся,
  докачка продолжается с того же места (--continue).
- Плейлист Playlist_1 — постоянный: ручные загрузки append'ятся в конец.
- Плейлист Подписки — цель автозагрузок из раздела «Каналы».
- Просмотренное: через 30 мин убирается из автоплейлистов,
  через 24 ч удаляется файл (кроме keep=true).
- seen: реестр video_id, переживающий удаление файлов, — защита от повторной
  автозагрузки уже виденного.
"""
import html
import json
import mimetypes
import os
import re
import shutil
import signal
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

WATCH_PL_NAME = "Playlist_1"   # постоянный плейлист ручных загрузок
SUBS_PL_NAME = "Подписки"       # цель автозагрузок из каналов

WATCHED_PL_REMOVE_SEC = 30 * 60       # убрать из автоплейлиста после просмотра
WATCHED_FILE_DELETE_SEC = 24 * 3600   # удалить файл после просмотра
JANITOR_INTERVAL_SEC = 60
SEEN_LIMIT = 5000

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
    "watch_pl_id": None,          # плейлист №1 (постоянный)
    "subs_pl_id": None,           # плейлист автозагрузок из каналов
}

RESOLUTION_HEIGHT = {
    "360": 360, "480": 480, "720": 720, "1080": 1080, "2160": 2160,
}

lock = threading.RLock()

state = {
    "queue": [],        # [{id,url,video_id,title,status,progress,...,source,channel_id,upload_date,added_at}]
    "downloaded": [],   # [{id,video_id,url,title,filepath,filesize,downloaded_at,duration,source,channel_id,upload_date,watched_at,keep}]
    "playlists": [],    # [{id,name,items:[downloaded_id],created_at,permanent}]
    "settings": dict(DEFAULT_SETTINGS),
    "paused": True,     # после старта всегда пауза (снимается из UI)
    "channels": [],     # [{id,url,name,interval_min,max_videos,enabled,last_check,last_found,created_at}]
    "seen": {},         # {video_id: {title, at, source}}
    "current_id": None,
}
worker_event = threading.Event()

# текущий дочерний yt-dlp (для паузы/остановки); под lock
dl_proc = None
dl_item_id = None
# каналы, по которым прямо сейчас идёт сканирование
scan_busy = set()


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


# ---------- persistence ----------

def load_state():
    global state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in ("queue", "downloaded", "playlists", "channels"):
            if isinstance(data.get(k), list):
                state[k] = data[k]
        if isinstance(data.get("seen"), dict):
            state["seen"] = data["seen"]
        if isinstance(data.get("settings"), dict):
            s = dict(DEFAULT_SETTINGS)
            s.update(data["settings"])
            state["settings"] = s
        if isinstance(data.get("paused"), bool):
            state["paused"] = data["paused"]
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {"queue": state["queue"], "downloaded": state["downloaded"],
             "playlists": state["playlists"], "settings": state["settings"],
             "paused": state["paused"], "channels": state["channels"],
             "seen": state["seen"]},
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


def net_flags():
    """Прокси/куки-флаги, общие для всех вызовов yt-dlp."""
    s = state["settings"]
    cmd = []
    if s.get("proxy_enabled") and s.get("proxy_url", "").strip():
        cmd += ["--proxy", s["proxy_url"].strip()]
    else:
        cmd += ["--proxy", ""]  # прямое подключение хоста (с учётом VPN)
    if s.get("use_cookies") and os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    return cmd


def base_cmd(extra=None):
    """Базовые флаги yt-dlp для одиночных видео."""
    cmd = [YTDLP, "--no-playlist", "--no-colors", "--no-warnings",
           "--no-write-subs", "--no-write-auto-subs",
           "--socket-timeout", "25"] + net_flags()
    if extra:
        cmd += extra
    return cmd


def mark_seen(video_id, title="", source="manual"):
    if not video_id:
        return
    state["seen"][video_id] = {"title": title or video_id,
                               "at": int(time.time()), "source": source}
    if len(state["seen"]) > SEEN_LIMIT:  # prune самых старых
        old = sorted(state["seen"].items(), key=lambda kv: kv[1].get("at", 0))
        for vid, _ in old[:len(state["seen"]) - SEEN_LIMIT]:
            del state["seen"][vid]


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


def find_downloaded(did):
    for d in state["downloaded"]:
        if d["id"] == did:
            return d
    return None


def find_playlist(pid):
    for p in state["playlists"]:
        if p["id"] == pid:
            return p
    return None


def find_channel(cid):
    for c in state["channels"]:
        if c["id"] == cid:
            return c
    return None


def ensure_special_playlists():
    """Playlist_1 (постоянный) и Подписки всегда существуют; id — в settings."""
    s = state["settings"]
    for key, name, permanent in (("watch_pl_id", WATCH_PL_NAME, True),
                                 ("subs_pl_id", SUBS_PL_NAME, False)):
        pl = find_playlist(s.get(key)) if s.get(key) else None
        if pl is None:
            pl = next((p for p in state["playlists"] if p.get("name") == name), None)
        if pl is None:
            pl = {"id": new_id("pl"), "name": name, "items": [],
                  "created_at": int(time.time()), "permanent": permanent}
            state["playlists"].append(pl)
            log("PLAYLIST created", name)
        else:
            if permanent:
                pl["permanent"] = True
            if s.get(key) != pl["id"]:
                s[key] = pl["id"]
    save_state()


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
            "source": "manual",
            "watched_at": None,
            "keep": False,
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
        "upload_date": data.get("upload_date"),
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


def stop_current_download(reason="пауза"):
    """Остановить текущую загрузку (убить process group). .part остаётся,
    докачка продолжится с того же места. Возвращает True если было что стопать."""
    global dl_proc, dl_item_id
    with lock:
        proc, qid = dl_proc, dl_item_id
        item = find_queue(qid) if qid else None
        if item is not None:
            item["stop_requested"] = True
    if proc is None:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    with lock:
        if item is not None and item.get("status") == "downloading":
            item["status"] = "queued"
            item["stage"] = "На паузе" if reason == "пауза" else "Остановлено"
            item.pop("stop_requested", None)
        state["current_id"] = None
        save_state()
    log("STOPPED", qid, reason)
    return True


def download_item(item):
    """Скачивание одного элемента очереди (вызывается из воркера)."""
    global dl_proc, dl_item_id
    s = state["settings"]
    height = RESOLUTION_HEIGHT.get(str(s.get("resolution", "720")))
    out_tmpl = os.path.join(VIDEOS_DIR, "%(title)s [%(id)s].%(ext)s")
    cmd = base_cmd([
        "--newline", "--progress",
        "--continue",                      # докачка .part с того же места
        "--retries", "10", "--fragment-retries", "10",
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
    item.pop("stop_requested", None)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, start_new_session=True)
    with lock:
        global dl_proc, dl_item_id
        dl_proc, dl_item_id = proc, item["id"]
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
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        item["status"] = "error"
        item["error"] = str(e)[:500]
        with lock:
            dl_proc, dl_item_id = None, None
            state["current_id"] = None
            save_state()
        return
    finally:
        with lock:
            if dl_item_id == item["id"]:
                dl_proc, dl_item_id = None, None

    if item.pop("stop_requested", None):
        # остановили паузой/сигналом — назад в очередь, .part на месте
        with lock:
            if item.get("status") == "downloading":
                item["status"] = "queued"
                item["stage"] = "На паузе"
            state["current_id"] = None
            save_state()
        log("PAUSED", item["url"], f"progress={item.get('progress')}")
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
            "source": item.get("source") or "manual",
            "channel_id": item.get("channel_id"),
            "upload_date": item.get("upload_date"),
            "watched_at": None,
            "keep": False,
        }
        with lock:
            state["downloaded"].insert(0, entry)
            state["queue"] = [q for q in state["queue"] if q["id"] != item["id"]]
            state["current_id"] = None
            if entry.get("video_id"):
                mark_seen(entry["video_id"], entry["title"], entry["source"])
            # автодобавление в целевой плейлист (ручные -> Playlist_1, авто -> Подписки)
            s = state["settings"]
            target_pid = s.get("subs_pl_id") if entry["source"] == "channel" else s.get("watch_pl_id")
            pl = find_playlist(target_pid)
            if pl is None:  # плейлист удалили вручную — пересоздать
                ensure_special_playlists()
                s = state["settings"]
                pl = find_playlist(s.get("subs_pl_id") if entry["source"] == "channel" else s.get("watch_pl_id"))
            if pl is not None and entry["id"] not in pl["items"]:
                pl["items"].append(entry["id"])
                log("PLAYLIST +", pl["name"], "<-", entry["title"][:60])
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
            if state.get("paused"):
                state["current_id"] = None
                worker_event.clear()
                continue
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
                state["current_id"] = nxt["id"]
                save_state()
            else:
                state["current_id"] = None
        if nxt is None:
            worker_event.clear()
            continue
        download_item(nxt)


# ---------- channels ----------

def fetch_channel_flat(url, limit, timeout=180):
    """Плоский список видео канала (новейшие первыми) через yt-dlp."""
    cmd = ([YTDLP, "--flat-playlist", "--dump-single-json", "--no-warnings",
            "--socket-timeout", "25",
            "--playlist-end", str(max(1, limit))] + net_flags() + [url])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "таймаут опроса канала"}
    except Exception as e:
        return {"error": str(e)}
    if p.returncode != 0 or not p.stdout.strip():
        err = (p.stderr or "").strip().splitlines()
        return {"error": err[-1] if err else f"yt-dlp exit {p.returncode}"}
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"error": "не удалось разобрать ответ yt-dlp"}
    entries = []
    for e in (data.get("entries") or []):
        if not isinstance(e, dict) or not e.get("id"):
            continue
        entries.append({"id": e["id"], "title": e.get("title") or e["id"],
                        "upload_date": e.get("upload_date"),
                        "duration": e.get("duration")})
    return {"entries": entries,
            "channel": data.get("channel") or data.get("uploader") or data.get("title")}


def scan_channel(channel_id):
    """Проверка канала: новейшие max_videos unseen-видео — в очередь."""
    with lock:
        if channel_id in scan_busy:
            return {"ok": False, "error": "уже проверяется"}
        ch = find_channel(channel_id)
        if ch is None:
            return {"ok": False, "error": "канал не найден"}
        scan_busy.add(channel_id)
    try:
        limit = max(int(ch.get("max_videos") or 5), 10)
        log("SCAN", ch.get("url"))
        res = fetch_channel_flat(ch["url"], limit)
        if "error" in res:
            log("SCAN FAIL", ch["url"], res["error"])
            return {"ok": False, "error": res["error"]}
        with lock:
            ch = find_channel(channel_id)
            if ch is None:
                return {"ok": False, "error": "канал удалён"}
            if res.get("channel") and not ch.get("name_set"):
                ch["name"] = res["channel"][:120]
            known = known_ids() | set(state["seen"].keys())
            fresh = [e for e in (res["entries"] or []) if e["id"] not in known]
            take = fresh[:max(1, int(ch.get("max_videos") or 5))]
            added = 0
            for e in reversed(take):  # старые первыми — порядок очереди
                urls = {q.get("url") for q in state["queue"]}
                url = f"https://www.youtube.com/watch?v={e['id']}"
                if e["id"] in known or url in urls:
                    continue
                state["queue"].append({
                    "id": new_id("q"), "url": url, "video_id": e["id"],
                    "title": e["title"], "duration": e.get("duration"),
                    "status": "queued", "progress": 0.0, "speed": "",
                    "eta": "", "size": "", "stage": "В очереди (авто)",
                    "added_at": int(time.time()), "error": "",
                    "source": "channel", "channel_id": channel_id,
                    "upload_date": e.get("upload_date"),
                })
                known.add(e["id"])
                added += 1
            # всё увиденное помечаем — защита от повторной автозагрузки,
            # в т.ч. после просмотра+удаления по таймеру
            for e in (res["entries"] or []):
                if e["id"] not in state["seen"]:
                    mark_seen(e["id"], e.get("title"), "channel")
            ch["last_check"] = int(time.time())
            ch["last_found"] = added
            save_state()
        if added:
            worker_event.set()
        log("SCAN DONE", ch.get("name") or ch["url"], f"+{added}")
        return {"ok": True, "added": added}
    finally:
        with lock:
            scan_busy.discard(channel_id)


# ---------- janitor ----------

def janitor():
    """Раз в минуту: очистка просмотренного + плановые проверки каналов."""
    while True:
        time.sleep(JANITOR_INTERVAL_SEC)
        try:
            now = int(time.time())
            changed = False
            with lock:
                s = state["settings"]
                auto_pl_ids = {s.get("watch_pl_id"), s.get("subs_pl_id")} - {None}
                for d in list(state["downloaded"]):
                    w = d.get("watched_at")
                    if not w:
                        continue
                    if now - w >= WATCHED_PL_REMOVE_SEC:
                        for pl in state["playlists"]:
                            if pl["id"] in auto_pl_ids and d["id"] in pl["items"]:
                                pl["items"].remove(d["id"])
                                changed = True
                                log("JANITOR unlist", d.get("title", "")[:50])
                    if now - w >= WATCHED_FILE_DELETE_SEC and not d.get("keep"):
                        full = os.path.join(VIDEOS_DIR, d.get("filepath") or "")
                        try:
                            if full and os.path.exists(full):
                                os.remove(full)
                        except OSError as e:
                            log("JANITOR rm fail", full, e)
                        state["downloaded"] = [x for x in state["downloaded"] if x["id"] != d["id"]]
                        for pl in state["playlists"]:
                            if d["id"] in pl["items"]:
                                pl["items"].remove(d["id"])
                        changed = True
                        log("JANITOR rmfile", d.get("title", "")[:50])
                if changed:
                    save_state()
                due = [c for c in state["channels"]
                       if c.get("enabled", True) and c.get("id") not in scan_busy
                       and now - int(c.get("last_check") or 0) >= max(5, int(c.get("interval_min") or 60)) * 60]
            for c in due:
                threading.Thread(target=scan_channel, args=(c["id"],), daemon=True).start()
        except Exception as e:
            log("JANITOR error", e)


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
    def serve_static(self, path, query):
        if path == "/" or path == "":
            path = "/index.html"
        # /videos/<file> — видеофайлы с поддержкой Range; ?dl=1 — скачать файлом
        if path.startswith("/videos/"):
            return self.serve_video(urllib.parse.unquote(path[len("/videos/"):]),
                                    download=query.get("dl") == ["1"])
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

    def serve_video(self, name, download=False):
        if "/" in name or ".." in name:
            return self.send_error(403)
        full = os.path.join(VIDEOS_DIR, name)
        if not os.path.isfile(full):
            return self.send_error(404)
        ctype, _ = mimetypes.guess_type(full)
        size = os.path.getsize(full)
        disp = None
        if download:
            safe = urllib.parse.quote(name)
            disp = f"attachment; filename*=UTF-8''{safe}"
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
                    if disp:
                        self.send_header("Content-Disposition", disp)
                    self.send_header("Content-Range", f"bytes {s}-{e}/{size}")
                    self.send_header("Content-Length", str(length))
                    self.end_headers()
                    f.seek(s)
                    shutil.copyfileobj(f, self.wfile, length)
                    return
            self.send_response(200)
            self.send_header("Content-Type", ctype or "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            if disp:
                self.send_header("Content-Disposition", disp)
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
                "paused": state["paused"],
                "channels": state["channels"],
                "scanning": sorted(scan_busy),
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
                    "added_at": int(time.time()), "error": "",
                    "source": "manual", "channel_id": None,
                    "upload_date": data.get("upload_date")}
            state["queue"].append(item)
            if vid:
                mark_seen(vid, item["title"], "manual")
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
                    mark_seen(info["video_id"], q["title"], q.get("source") or "manual")
                if info.get("upload_date"):
                    q["upload_date"] = info["upload_date"]
                save_state()

    def api_pause(self, data):
        want = data.get("paused")
        if not isinstance(want, bool):
            return self.send_json({"ok": False, "error": "нужен флаг paused"}, 400)
        with lock:
            state["paused"] = want
            save_state()
        if want:
            stop_current_download("пауза")
        else:
            worker_event.set()
        with lock:
            self.send_json({"ok": True, "paused": state["paused"]})

    def api_move(self, data):
        qid = data.get("id")
        dest = data.get("to")  # индекс либо 'up'/'down'/'top'
        with lock:
            q = find_queue(qid)
            if not q:
                return self.send_json({"ok": False, "error": "не найдено"}, 404)
            if q["status"] == "downloading":
                return self.send_json({"ok": False, "error": "загружается прямо сейчас"}, 409)
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
            q["error"] = ""
            q["stage"] = "В очереди"
            q.pop("auto_retry", None)
            save_state()
        worker_event.set()
        self.send_json({"ok": True})

    def api_watched(self, data):
        did = data.get("id")
        with lock:
            d = find_downloaded(did)
            if not d:
                return self.send_json({"ok": False, "error": "не найдено"}, 404)
            d["watched_at"] = int(time.time())
            save_state()
            self.send_json({"ok": True, "watched_at": d["watched_at"]})

    def api_keep(self, data):
        did = data.get("id")
        with lock:
            d = find_downloaded(did)
            if not d:
                return self.send_json({"ok": False, "error": "не найдено"}, 404)
            d["keep"] = bool(data.get("keep"))
            save_state()
            self.send_json({"ok": True, "keep": d["keep"]})

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
        # seen НЕ чистим: защита от повторной автозагрузки сохраняется
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
                      "created_at": int(time.time()), "permanent": False}
                state["playlists"].append(pl)
                save_state()
                return self.send_json({"ok": True, "playlist": pl})
            pl = next((p for p in state["playlists"] if p["id"] == data.get("playlist_id")), None)
            if not pl:
                return self.send_json({"ok": False, "error": "плейлист не найден"}, 404)
            if action == "rename":
                pl["name"] = (data.get("name") or "").strip()[:80] or pl["name"]
            elif action == "delete":
                if pl.get("permanent"):
                    return self.send_json({"ok": False, "error": "постоянный плейлист нельзя удалить"}, 403)
                state["playlists"] = [p for p in state["playlists"] if p["id"] != pl["id"]]
                for k in ("watch_pl_id", "subs_pl_id"):
                    if state["settings"].get(k) == pl["id"]:
                        state["settings"][k] = None
                ensure_special_playlists()
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

    def api_channels(self, action, data):
        if action == "list":
            with lock:
                return self.send_json({"ok": True, "channels": state["channels"],
                                       "scanning": sorted(scan_busy)})
        if action == "add":
            url = (data.get("url") or "").strip()
            if not url or not url.startswith(("http://", "https://")):
                return self.send_json({"ok": False, "error": "Нужна http(s) ссылка на канал"}, 400)
            try:
                interval = max(5, min(1440 * 7, int(data.get("interval_min") or 60)))
            except (TypeError, ValueError):
                interval = 60
            try:
                maxv = max(1, min(50, int(data.get("max_videos") or 5)))
            except (TypeError, ValueError):
                maxv = 5
            with lock:
                if any(c.get("url") == url for c in state["channels"]):
                    return self.send_json({"ok": False, "error": "такой канал уже добавлен"}, 409)
                ch = {"id": new_id("ch"), "url": url, "name": url,
                      "name_set": False, "interval_min": interval, "max_videos": maxv,
                      "enabled": True, "last_check": 0, "last_found": 0,
                      "created_at": int(time.time())}
                state["channels"].append(ch)
                save_state()
            # сразу первая проверка в фоне (заодно подтянет название)
            threading.Thread(target=scan_channel, args=(ch["id"],), daemon=True).start()
            with lock:
                return self.send_json({"ok": True, "channel": find_channel(ch["id"]) or ch})
        if action == "update":
            with lock:
                ch = find_channel(data.get("id"))
                if not ch:
                    return self.send_json({"ok": False, "error": "канал не найден"}, 404)
                if "enabled" in data:
                    ch["enabled"] = bool(data["enabled"])
                if "interval_min" in data:
                    try:
                        ch["interval_min"] = max(5, min(1440 * 7, int(data["interval_min"])))
                    except (TypeError, ValueError):
                        pass
                if "max_videos" in data:
                    try:
                        ch["max_videos"] = max(1, min(50, int(data["max_videos"])))
                    except (TypeError, ValueError):
                        pass
                if "name" in data and str(data["name"]).strip():
                    ch["name"] = str(data["name"]).strip()[:120]
                    ch["name_set"] = True
                save_state()
                return self.send_json({"ok": True, "channel": ch})
        if action == "delete":
            with lock:
                ch = find_channel(data.get("id"))
                if not ch:
                    return self.send_json({"ok": False, "error": "канал не найден"}, 404)
                state["channels"] = [c for c in state["channels"] if c["id"] != ch["id"]]
                save_state()
                return self.send_json({"ok": True})
        if action == "check":
            with lock:
                ch = find_channel(data.get("id"))
                if not ch:
                    return self.send_json({"ok": False, "error": "канал не найден"}, 404)
                if ch["id"] in scan_busy:
                    return self.send_json({"ok": False, "error": "уже проверяется"}, 409)
            threading.Thread(target=scan_channel, args=(ch["id"],), daemon=True).start()
            return self.send_json({"ok": True, "message": "проверка запущена"})
        return self.send_json({"ok": False, "error": "unknown action"}, 404)

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
        return self.serve_static(path, qs)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        data = self.read_json()
        if path == "/api/add":
            return self.api_add(data)
        if path == "/api/pause":
            return self.api_pause(data)
        if path == "/api/queue/move":
            return self.api_move(data)
        if path == "/api/queue/remove":
            return self.api_remove(data)
        if path == "/api/queue/retry":
            return self.api_retry(data)
        if path == "/api/watched":
            return self.api_watched(data)
        if path == "/api/keep":
            return self.api_keep(data)
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
        if path.startswith("/api/channels/"):
            return self.api_channels(path.rsplit("/", 1)[-1], data)
        return self.send_json({"ok": False, "error": "unknown endpoint"}, 404)


def shutdown_handler(signum, frame):
    log("SHUTDOWN signal", signum)
    try:
        stop_current_download("shutdown")
    except Exception as e:
        log("shutdown stop error", e)
    try:
        with lock:
            save_state()
    except Exception:
        pass
    sys.exit(0)


def main():
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)
    load_state()
    with lock:
        for q in state["queue"]:
            if q.get("status") == "downloading":
                q["status"] = "queued"  # сброс зависшего после рестарта
                q["stage"] = "На паузе"
        state["current_id"] = None
        state["paused"] = True  # после (пере)запуска всегда пауза — снимается из UI
        for d in state["downloaded"]:
            d.setdefault("source", "manual")
            d.setdefault("watched_at", None)
            d.setdefault("keep", False)
        for q in state["queue"]:
            q.setdefault("source", "manual")
        for pl in state["playlists"]:
            pl.setdefault("permanent", False)
        ensure_special_playlists()
        save_state()
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    worker_event.set()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"yt-dll on http://{HOST}:{PORT}  videos={VIDEOS_DIR} paused=True")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
