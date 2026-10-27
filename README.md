# yt-dll — Web UI очередь загрузок yt-dlp

Глассморфизм-интерфейс для скачивания YouTube-видео через `yt-dlp`.
**Ноль внешних зависимостей**: только Python 3 stdlib + системные `yt-dlp` и `ffmpeg`.

- URL: `http://<host>:49977` — слушает `0.0.0.0`, доступен по LAN без авторизации
- По умолчанию: **720p**, приоритет **русской аудиодорожки**, **без субтитров**, merge в **MP4**
- Файлы: `videos/«Название [videoId].mp4»` — кириллица сохраняется
- Скачивание **строго по очереди** (один воркер), прогресс/скорость/ETA live через polling `/api/state` (1 c)
- Защита от дублей по `videoId` (очередь + загруженные + файлы на диске)
- Встроенный плеер (`<video>` с HTTP Range), плейлисты с последовательным проигрыванием
- Пауза: после (пере)запуска загрузки всегда на паузе — продолжение кнопкой в UI, докачка `.part` с того же места
- `Playlist_1` — постоянный (удалить нельзя): ручные загрузки append'ятся в конец; live-плеер добрасывает новые без перезагрузки
- Просмотренное (в `Playlist_1`/`Подписки`): через 30 мин убирается из плейлиста, через 24 ч удаляется файл (кроме 📌 «не удалять»)
- Раздел «Каналы»: автопроверка (`--flat-playlist`), новые видео — в очередь с меткой «авто» и в конец «Подписки»; реестр `seen` защищает от повторной загрузки уже виденного
- Кнопка ⬇ «Скачать» — сохранение файла на устройство (удобно по LAN с телефона)
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
- `POST /api/pause` `{paused: bool}` — пауза/продолжение (пауза останавливает и текущую загрузку)
- `POST /api/watched` `{id}` — пометить просмотренным (старт таймеров очистки)
- `POST /api/keep` `{id, keep}` — флаг «не удалять»
- `POST /api/channels/add|update|delete|check|list` — отслеживаемые каналы (`{url, interval_min, max_videos, enabled}`)
- `POST /api/queue/move` `{id, to: up|down|top|<index>}`
- `POST /api/queue/remove`, `POST /api/queue/retry`
- `POST /api/settings` `{resolution, prefer_ru_audio, proxy_enabled, proxy_url, use_cookies, cookies_browser}`
- `POST /api/cookies/export` `{browser}` / `POST /api/cookies/clear`
- `POST /api/playlists/create|rename|delete|add|remove|move`
- `POST /api/files/delete` `{filepath}`
- `GET /videos/<file>` — стриминг с Range; `?dl=1` — скачать файлом (attachment)

## Контейнеризация (Docker)

Проект содержит готовую контейнеризированную версию для запуска в Docker,
в том числе на Windows (Docker Desktop). Оригинальный хост-сервис
(`yt-dll.service`) при этом не меняется и продолжает работать как есть.

- Репозиторий: https://github.com/teletonn/yt-dll
- Подробный гайд: `DEPLOYMENT.md`

### Быстрый старт (Docker Compose)

```bash
git clone https://github.com/teletonn/yt-dll.git
cd yt-dll
docker compose up -d --build
```

Интерфейс: `http://localhost:49977` или `http://<IP-хоста>:49977`.

### Данные

Все постоянные данные хранятся в `./data` на хосте:
- `./data/videos` — скачанные видео
- `./data/state.json` — очередь, плейлисты, настройки, каналы, seen
- `./data/cookies.txt` — cookies

При `docker compose down` данные сохраняются. Удаление папки `./data`
полностью очищает состояние.

### Обновление

```bash
docker compose build --no-cache
docker compose up -d
```

После перезапуска сервис всегда на паузе (`paused=true`) — снимите паузу в UI.
