# PPTX Translator (MVP)

Сайт для перевода презентаций PowerPoint: загружаете `.pptx` → выбираете язык → скачиваете `.pptx` с переведённым текстом.

## Запуск (Windows / PowerShell)

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Открыть в браузере: `http://127.0.0.1:8000`

## Переводчик: LibreTranslate + локальный Argos

По умолчанию **`TRANSLATE_BACKEND=auto`**: сначала запрос идёт в [LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) (`TRANSLATE_ENDPOINT`), а если сервер **недоступен** — **локальный [Argos Translate](https://github.com/argosopentech/argos-translate)** (при необходимости ставит прямой пакет или цепочку через `en` / `ru` / `tr`).

**Языки в интерфейсе** совпадают с ответом LibreTranslate **`GET /languages`**: запрос `GET /api/translate/languages` сначала ходит на ваш `TRANSLATE_ENDPOINT`, при ошибке — на публичный каталог `https://libretranslate.com/languages` (список кодов без API-ключа), затем на индекс пакетов Argos, в крайнем случае — встроенный минимальный список. **Казахский (`kk` / `kz`) в списке не показывается и на сервере отклоняется.** Каталог для подгрузки списка можно переопределить: `TRANSLATE_LANGUAGES_CATALOG` (URL без `/languages` в конце).

Переменные окружения:

- `TRANSLATE_BACKEND` — `auto` (по умолчанию), `libretranslate` — только HTTP, `argos` — только локально без Docker
- `TRANSLATE_ENDPOINT` — URL инстанса LibreTranslate (по умолчанию `http://127.0.0.1:5000`)
- `TRANSLATE_API_KEY` — если на сервере включена проверка ключа
- `TRANSLATE_TIMEOUT_S` — таймаут HTTP (по умолчанию 120)
- `TRANSLATE_LANGUAGES_CATALOG` — базовый URL для запасного `GET …/languages` (по умолчанию `https://libretranslate.com`)

Пример с Docker (в отдельном терминале):

```bash
docker run --rm -p 5000:5000 libretranslate/libretranslate
```

Или из корня проекта (удобнее перезапускать):

```powershell
docker compose up -d
```

Первый запуск контейнера часто **долгий**: качаются языковые модели. Пока контейнер не готов, translide будет выдавать ошибку соединения.

Проверка, что API живой (должен вернуться JSON со списком языков):

```powershell
curl http://127.0.0.1:5000/languages
```

```powershell
$env:TRANSLATE_ENDPOINT="http://127.0.0.1:5000"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

По умолчанию endpoint уже `http://127.0.0.1:5000`; переменную нужно задавать только если LibreTranslate на другом хосте/порту.

## Ошибка «All connection attempts failed» / LibreTranslate недоступен

Это значит, что до указанного в `TRANSLATE_ENDPOINT` адреса **не удаётся подключиться** (не HTTP-ошибка, а отсутствие ответа на порту).

1. Запущен ли контейнер: `docker ps` (должен быть порт `5000`).
2. Дождитесь окончания инициализации после первого `docker compose up` / `docker run` (логи: `docker compose logs -f`).
3. Убедитесь, что translide смотрит туда же, куда слушает Docker: для приложения на Windows/macOS обычно `http://127.0.0.1:5000`. Если сам translide крутится **внутри Docker**, вместо `127.0.0.1` часто нужен `http://host.docker.internal:5000` (и проброс порта на хосте).
4. Не занят ли порт другим процессом (другой сервис на `5000`).
5. Если используете **публичный** инстанс LibreTranslate — проверьте URL, лимиты и при необходимости задайте `TRANSLATE_API_KEY`.

## Если «не переводит» (но соединение есть)

- Убедитесь, что LibreTranslate отвечает по `TRANSLATE_ENDPOINT` (см. `curl` выше).
- Публичные инстансы часто требуют API-ключ — тогда задайте `TRANSLATE_API_KEY` или используйте свой Docker.

## Ограничения MVP

- Переводятся текстовые блоки на слайдах (включая **таблицы**, **группы фигур** и **заметки докладчика**). Текст внутри картинок не переводится.
- При нескольких фрагментах текста в одном абзаце (разные `run`) перевод идёт по частям, чтобы не склеивались слова и сохранялось форматирование.
