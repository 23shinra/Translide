"""
Список языков для UI и валидации: как у LibreTranslate GET /languages (локальный инстанс,
при недоступности — публичный каталог libretranslate.com, затем Argos, затем встроенный минимум).
Казахский (kk/kz) в проекте не предлагаем.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

import httpx

EXCLUDED_LANG_CODES = frozenset({"kk", "kz"})

CATALOG_FALLBACK_BASE = (
    (os.getenv("TRANSLATE_LANGUAGES_CATALOG") or "https://libretranslate.com").strip().rstrip("/")
    or "https://libretranslate.com"
)

BUILTIN_LANGUAGES: list[dict[str, str]] = [
    {"code": "en", "name": "English"},
    {"code": "ru", "name": "Russian"},
    {"code": "de", "name": "German"},
    {"code": "fr", "name": "French"},
    {"code": "es", "name": "Spanish"},
    {"code": "pt", "name": "Portuguese"},
    {"code": "it", "name": "Italian"},
    {"code": "ja", "name": "Japanese"},
    {"code": "zh", "name": "Chinese"},
    {"code": "uk", "name": "Ukrainian"},
    {"code": "pl", "name": "Polish"},
    {"code": "nl", "name": "Dutch"},
    {"code": "ar", "name": "Arabic"},
    {"code": "tr", "name": "Turkish"},
    {"code": "cs", "name": "Czech"},
    {"code": "ro", "name": "Romanian"},
    {"code": "el", "name": "Greek"},
    {"code": "sv", "name": "Swedish"},
    {"code": "hu", "name": "Hungarian"},
]

_lang_cache: dict[str, Any] = {"expires": 0.0, "payload": None}


def normalize_language_entries(raw: list[Any]) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip().lower()  # Libre / UI ожидают нижний регистр
        if not code or code in EXCLUDED_LANG_CODES:
            continue
        name = str(item.get("name", "")).strip() or code
        seen.setdefault(code, name)
    return [{"code": c, "name": n} for c, n in sorted(seen.items(), key=lambda x: (x[1].lower(), x[0]))]


async def fetch_languages_from_http(base_url: str, api_key: Optional[str], timeout_s: float) -> Optional[list[dict[str, str]]]:
    url = base_url.rstrip("/") + "/languages"
    params: dict[str, str] = {}
    if api_key:
        params["api_key"] = api_key
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            r = await client.get(url, headers={"Accept": "application/json"}, params=params or None)
            if r.status_code != 200:
                return None
            data = r.json()
            if not isinstance(data, list):
                return None
            out = normalize_language_entries(data)
            return out or None
    except Exception:
        return None


def load_languages_from_argos() -> list[dict[str, str]]:
    try:
        import argostranslate.package as apkg
    except ImportError:
        return []
    try:
        apkg.get_available_packages()
        pkgs = apkg.get_available_packages()
    except Exception:
        return []
    names: dict[str, str] = {}
    for p in pkgs:
        if getattr(p, "type", "") != "translate":
            continue
        fc = (getattr(p, "from_code", None) or "").strip().lower()
        fn = (getattr(p, "from_name", None) or "").strip()
        tc = (getattr(p, "to_code", None) or "").strip().lower()
        tn = (getattr(p, "to_name", None) or "").strip()
        if fc and fc not in EXCLUDED_LANG_CODES:
            names.setdefault(fc, fn or fc)
        if tc and tc not in EXCLUDED_LANG_CODES:
            names.setdefault(tc, tn or tc)
    if not names:
        return []
    return [{"code": c, "name": names[c]} for c in sorted(names, key=lambda x: (names[x].lower(), x))]


async def get_translation_languages_payload() -> dict[str, Any]:
    timeout = min(12.0, float(os.getenv("TRANSLATE_TIMEOUT_S", "120")))
    endpoint = (os.getenv("TRANSLATE_ENDPOINT") or "http://127.0.0.1:5000").strip().rstrip("/")
    api_key = (os.getenv("TRANSLATE_API_KEY") or "").strip() or None

    langs = await fetch_languages_from_http(endpoint, api_key, timeout)
    source = "libretranslate"
    if not langs:
        langs = await fetch_languages_from_http(CATALOG_FALLBACK_BASE, None, timeout)
        source = "libretranslate_com"
    if not langs:
        langs = await asyncio.to_thread(load_languages_from_argos)
        source = "argos"
    if not langs:
        langs = list(BUILTIN_LANGUAGES)
        source = "builtin"
    return {"languages": langs, "source": source}


async def get_translation_languages_cached() -> dict[str, Any]:
    now = time.monotonic()
    ttl = 300.0
    if _lang_cache["payload"] is not None and now < _lang_cache["expires"]:
        return _lang_cache["payload"]
    payload = await get_translation_languages_payload()
    _lang_cache["payload"] = payload
    _lang_cache["expires"] = now + ttl
    return payload


async def get_allowed_language_codes() -> frozenset[str]:
    p = await get_translation_languages_cached()
    return frozenset(str(x["code"]).strip().lower() for x in p.get("languages", []) if x.get("code"))
