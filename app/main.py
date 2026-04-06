import os
import re
import zipfile
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth_service import (
    add_history,
    authenticate,
    create_access_token,
    create_user,
    clear_history,
    ensure_demo_user,
    get_current_user,
    get_optional_user,
    list_history,
)
from .database import init_db
from .languages import EXCLUDED_LANG_CODES, get_allowed_language_codes, get_translation_languages_cached
from .pptx_translate import translate_pptx_bytes
from .translate import ArgosTranslateClient, AutoTranslateClient, LibreTranslateClient, TranslateClient, TranslateError


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="PPTX Translator", lifespan=lifespan)
_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Idempotent; ensures tables exist even if lifespan is not run (e.g. some test transports).
init_db()
ensure_demo_user()

if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

def _sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name)
    return name or "presentation.pptx"


def _content_disposition_attachment(filename: str) -> str:
    """
    Starlette encodes headers as latin-1; use RFC 5987 `filename*` to safely support UTF-8 filenames.
    """
    fn = filename or "presentation.pptx"
    # ASCII fallback for legacy clients.
    fallback = fn.encode("ascii", "ignore").decode("ascii").strip() or "presentation.pptx"
    fallback = fallback.replace('"', "_")
    encoded = quote(fn, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _build_translate_client() -> TranslateClient:
    timeout_s = float(os.getenv("TRANSLATE_TIMEOUT_S", "120"))
    endpoint = (os.getenv("TRANSLATE_ENDPOINT") or "http://127.0.0.1:5000").strip().rstrip("/")
    api_key = (os.getenv("TRANSLATE_API_KEY") or "").strip() or None
    libre = LibreTranslateClient(endpoint=endpoint, api_key=api_key, timeout=httpx.Timeout(timeout_s))
    backend = (os.getenv("TRANSLATE_BACKEND") or "auto").strip().lower()
    if backend in ("libre", "libretranslate", "http", "remote"):
        return libre
    if backend in ("argos", "local"):
        return ArgosTranslateClient()
    if backend in ("auto", "", "both"):
        return AutoTranslateClient(libre)
    return AutoTranslateClient(libre)


def _is_zip_with_entry_prefix(data: bytes, required_prefix: str) -> bool:
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            return any(name.startswith(required_prefix) for name in z.namelist())
    except Exception:
        return False


def _looks_like_ole_compound_file(data: bytes) -> bool:
    return data.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1")


async def _read_auth_body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Некорректный JSON.") from e
    data = data or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    first_name = str(data.get("first_name", "")).strip()
    last_name = str(data.get("last_name", "")).strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Укажи логин и пароль.")
    if len(username) > 64 or len(password) > 128:
        raise HTTPException(status_code=400, detail="Слишком длинные поля.")
    if len(first_name) > 64 or len(last_name) > 64:
        raise HTTPException(status_code=400, detail="Слишком длинные поля.")
    return {"username": username, "password": password, "first_name": first_name, "last_name": last_name}


@app.post("/api/auth/register")
async def api_register(request: Request):
    data = await _read_auth_body(request)
    user = create_user(data["username"], data["password"], data.get("first_name", ""), data.get("last_name", ""))
    token = create_access_token(user["id"], user["username"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
        },
    }


@app.post("/api/auth/login")
async def api_login(request: Request):
    data = await _read_auth_body(request)
    user = authenticate(data["username"], data["password"])
    token = create_access_token(user["id"], user["username"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
        },
    }


@app.get("/api/me")
async def api_me(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "username": user["username"],
        "created_at": user.get("created_at"),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
    }


@app.get("/api/history")
async def api_history(user: dict = Depends(get_current_user)):
    return {"items": list_history(user["id"])}


@app.delete("/api/history")
async def api_history_clear(user: dict = Depends(get_current_user)):
    deleted = clear_history(user["id"])
    return {"ok": True, "deleted": deleted}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return FileResponse(os.path.join(_TEMPLATES_DIR, "index.html"), media_type="text/html; charset=utf-8")


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    return FileResponse(os.path.join(_TEMPLATES_DIR, "account.html"), media_type="text/html; charset=utf-8")


@app.get("/api/translate/languages")
async def api_translate_languages():
    """Список языков в формате LibreTranslate GET /languages (kk/kz исключены)."""
    return await get_translation_languages_cached()


@app.post("/api/translate/pptx")
async def translate_pptx(
    file: UploadFile = File(...),
    target_lang: str = Form(...),
    source_lang: Optional[str] = Form(None),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    data = await file.read()
    filename = (file.filename or "").strip()
    ext = os.path.splitext(filename.lower())[1]

    if ext in (".pptx", ".ppsx", ".potx"):
        if not _is_zip_with_entry_prefix(data, "ppt/"):
            raise HTTPException(status_code=400, detail="Это не презентация PowerPoint (.pptx). Выберите другой файл.")
    elif ext in (".ppt", ".pps", ".pot"):
        if not _looks_like_ole_compound_file(data):
            raise HTTPException(status_code=400, detail="Это не презентация PowerPoint (.ppt). Выберите другой файл.")
        raise HTTPException(status_code=400, detail="Формат .ppt пока не поддерживается для перевода. Сохраните как .pptx и загрузите снова.")
    elif ext == ".odp":
        if not (_is_zip_with_entry_prefix(data, "content.xml") or _is_zip_with_entry_prefix(data, "mimetype")):
            raise HTTPException(status_code=400, detail="Это не презентация (.odp). Выберите другой файл.")
        raise HTTPException(status_code=400, detail="Формат .odp пока не поддерживается для перевода. Загрузите .pptx.")
    else:
        raise HTTPException(status_code=400, detail="Это не презентация. Выберите файл .pptx/.ppt/.odp.")

    tl = (target_lang or "").strip().lower()
    if tl in EXCLUDED_LANG_CODES:
        raise HTTPException(status_code=400, detail="Этот язык сейчас не поддерживается. Выберите другой целевой язык.")
    allowed = await get_allowed_language_codes()
    if tl not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Неподдерживаемый целевой язык «{target_lang}». Обновите страницу и выберите язык из списка.",
        )
    sl = (source_lang or "").strip().lower()
    if not sl:
        raise HTTPException(status_code=400, detail="Укажите исходный язык.")
    if sl not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Неподдерживаемый исходный язык «{source_lang}». Выберите язык из списка.",
        )

    client = _build_translate_client()
    src_for_translate = sl
    try:
        out_bytes = await translate_pptx_bytes(
            pptx_bytes=data,
            source_lang=src_for_translate,
            target_lang=tl,
            translator=client,
        )
    except TranslateError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                "Перевод не выполнен. В режиме `TRANSLATE_BACKEND=auto` сначала используется LibreTranslate, "
                "при недоступности сервера — локальный Argos. Проверьте сеть, `pip install -r requirements.txt`, "
                f"Docker LibreTranslate и выбранные языки. Тех.детали: {str(e)[:400]}"
            ),
        ) from e

    original = _sanitize_filename(file.filename or "presentation.pptx")
    base = original[:-5] if original.lower().endswith(".pptx") else original
    out_name = f"{base}.{tl}.pptx"

    if current_user:
        try:
            add_history(current_user["id"], original, tl, out_name)
        except Exception:
            pass

    return StreamingResponse(
        BytesIO(out_bytes),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": _content_disposition_attachment(out_name)},
    )
