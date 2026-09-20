# yt-dll — Web UI очередь загрузок yt-dlp

Глассморфизм-интерфейс для скачивания YouTube-видео через `yt-dlp`.
**Ноль внешних зависимостей**: только Python 3 stdlib + системные `yt-dlp` и `ffmpeg`.

- URL: `http://<host>:49977` — слушает `0.0.0.0`, доступен по LAN без авторизации
- По умолчанию: **720p**, приоритет **русской аудиодорожки**, **без субтитров**, merge в **MP4**
- Файлы: `videos/«Название [videoId].mp4»` — кириллица сохраняется
- Скачивание **строго по очереди** (один воркер), прогресс/скорость/ETA live через polling `/api/state` (1 c)
- Защита от дублей по `videoId` (очередь + загруженные + файлы на диске)
- Встроенный плеер (`<video>` с HTTP Range), плейлисты с последовательным проигрыванием
- Настройки: разрешение, RU-аудио, прокси (`--proxy`, по умолчанию выкл = прямое подключение хоста с учётом VPN), cookies из браузера (`--cookies-from-browser chromium` → `cookies.txt`)

## Запуск

```bash
./start.sh
# или
python3 app.py
```

## Автозапуск при загрузке (systemd user unit)

```bash
mkdir -p ~/.config/systemd/user
cp yt-dll.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now yt-dll.service
```

## API

- `GET /api/state` — очередь, загруженные, плейлисты, настройки
- `GET /api/info?url=` — метаданные без скачивания (+ флаг `duplicate`)
- `POST /api/add` `{url}` — в очередь
- `POST /api/queue/move` `{id, to: up|down|top|<index>}`
- `POST /api/queue/remove`, `POST /api/queue/retry`
- `POST /api/settings` `{resolution, prefer_ru_audio, proxy_enabled, proxy_url, use_cookies, cookies_browser}`
- `POST /api/cookies/export` `{browser}` / `POST /api/cookies/clear`
- `POST /api/playlists/create|rename|delete|add|remove|move`
- `POST /api/files/delete` `{filepath}`
- `GET /videos/<file>` — стриминг с Range
