# Развертывание yt-dll в Docker (Windows)

Этот гайд описывает запуск **контейнеризированной** версии `yt-dll`
на хосте с Windows в Docker Desktop. На исходный сервис на исходном хосте
ничего не меняется.

## Требования

- Windows 10/11 с включенным **WSL2**.
- Установленный **Docker Desktop** (backend: WSL2).
- Свободный порт `49977` (или измените маппинг портов в `docker-compose.yml`).

## Быстрый старт

В PowerShell:

```powershell
git clone https://github.com/teletonn/yt-dll.git
cd yt-dll
docker compose up -d --build
```

После запуска интерфейс доступен по адресу:

```
http://localhost:49977
```
или по LAN:

```
http://<IP-вашего-ПК>:49977
```

## Данные и постоянство

Все пользовательские данные хранятся в папке `.\data` (на хосте):

- `.\data\videos` — скачанные видео.
- `.\data\state.json` — очередь, плейлисты, настройки, каналы, seen.
- `.\data\cookies.txt` — cookies (если экспортированы из браузера).

При `docker compose down` данные сохраняются в `.\data`. Полное удаление данных
— удалить папку `.\data`.

Контейнер создает `state.json` и `cookies.txt` автоматически при первом запуске.

## Обновление yt-dlp / ffmpeg пересборкой образа

```powershell
docker compose build --no-cache
docker compose up -d
```

## Полезные команды

```powershell
# Логи
docker compose logs -f yt-dll

# Остановить
docker compose down

# Статус
docker compose ps
```

## Перенос данных с другого хоста

1. Остановите сервис на исходном хосте.
2. Скопируйте всю папку `videos/`, а также `state.json` и `cookies.txt`
   в `.\data` на новом хосте (соответственно `.\data\videos`, `.\data\state.json`, `.\data\cookies.txt`).
3. Запустите `docker compose up -d`.
4. Откройте UI и снимите паузу (после перезапуска сервис всегда на паузе).

## Примечания

- Сервис слушает `0.0.0.0:49977` внутри контейнера и публикуется на хост.
- По умолчанию после запуска загрузки на паузе (`paused=true`) — снимите паузу в UI.
- Cookies: экспорт из браузера (`/api/cookies/export`) сохраняет файл в `.\data\cookies.txt`.
- Порт и маппинг можно изменить в `docker-compose.yml`, например:
  `- "8080:49977"`.
