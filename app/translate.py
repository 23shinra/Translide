from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Protocol

import httpx


class TranslateClient(Protocol):
    async def translate(self, text: str, *, source_lang: Optional[str], target_lang: str) -> str: ...


class TranslateError(RuntimeError):
    pass


def _is_libre_unreachable(err: BaseException) -> bool:
    """True, если LibreTranslate не достучаться по сети (а не 4xx/5xx ответ)."""
    seen: set[int] = set()
    root: BaseException | None = err
    while root is not None and id(root) not in seen:
        seen.add(id(root))
        if isinstance(root, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return True
        nxt = root.__cause__
        root = nxt if nxt is not root else None

    msg = str(err).lower()
    if "all connection attempts failed" in msg:
        return True
    if "connection refused" in msg:
        return True
    if "name or service not known" in msg:
        return True
    if "getaddrinfo failed" in msg:
        return True
    return False


_LANG_ALIASES: dict[str, str] = {}


def _normalize_lang_code(code: Optional[str]) -> str:
    c = (code or "").strip().lower()
    if not c or c == "auto":
        return ""
    return _LANG_ALIASES.get(c, c)


def _infer_source_lang(text: str) -> str:
    try:
        from langdetect import detect

        return _normalize_lang_code(detect(text)) or "en"
    except Exception:
        return "en"


@dataclass(frozen=True)
class LibreTranslateClient:
    """
    LibreTranslate HTTP API (self-hosted or public instance).
    See: https://github.com/LibreTranslate/LibreTranslate
    """

    endpoint: str = "http://127.0.0.1:5000"
    api_key: Optional[str] = None
    timeout: httpx.Timeout = field(default_factory=lambda: httpx.Timeout(120))

    async def translate(self, text: str, *, source_lang: Optional[str], target_lang: str) -> str:
        text = text or ""
        if not text.strip():
            return text

        tgt = (target_lang or "").strip()
        if not tgt:
            raise TranslateError("Не указан целевой язык.")

        url = self.endpoint.rstrip("/") + "/translate"
        payload: dict = {
            "q": text,
            "source": (source_lang or "").strip() or "auto",
            "target": tgt,
            "format": "text",
        }
        if self.api_key:
            payload["api_key"] = self.api_key

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                r = await client.post(url, data=payload, headers={"Accept": "application/json"})
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPStatusError as e:
                body = ""
                try:
                    body = e.response.text
                except Exception:
                    body = ""
                raise TranslateError(
                    f"LibreTranslate отклонил запрос ({e.response.status_code}). {body[:500]}".strip()
                ) from e
            except httpx.HTTPError as e:
                raise TranslateError(f"Запрос к LibreTranslate не удался. {e}".strip()) from e

        translated = data.get("translatedText") if isinstance(data, dict) else None
        if isinstance(translated, str):
            return translated
        return text


class ArgosTranslateClient:
    """
    Локальный перевод через Argos Translate (те же модели, что под капотом у LibreTranslate).
    При первом запросе для пары языков может скачаться пакет с интернета.
    Цепочка: кратчайший путь по графу из индекса пакетов (например ru→en→fr), плюс явный update индекса.
    """

    _install_lock = threading.Lock()

    def _pair_ready(self, from_code: str, to_code: str) -> bool:
        import argostranslate.translate as atr

        from_lang = atr.get_language_from_code(from_code)
        to_lang = atr.get_language_from_code(to_code)
        if from_lang is None or to_lang is None:
            return False
        return from_lang.get_translation(to_lang) is not None

    @staticmethod
    def _argos_directed_graph(apkg) -> dict[str, list[str]]:
        adj: dict[str, list[str]] = {}
        for p in apkg.get_available_packages():
            if getattr(p, "type", "") != "translate":
                continue
            fc = (getattr(p, "from_code", None) or "").strip().lower()
            tc = (getattr(p, "to_code", None) or "").strip().lower()
            if fc and tc and fc != tc:
                adj.setdefault(fc, []).append(tc)
        return adj

    @staticmethod
    def _shortest_path_in_graph(adj: dict[str, list[str]], start: str, goal: str, max_depth: int = 14) -> Optional[list[str]]:
        if start == goal:
            return [start]
        if start not in adj:
            return None
        q = deque([(start, [start])])
        visited = {start}
        while q:
            node, path = q.popleft()
            if len(path) > max_depth:
                continue
            for nb in adj.get(node, ()):
                if nb in path:
                    continue
                if nb == goal:
                    return path + [nb]
                if nb not in visited:
                    visited.add(nb)
                    q.append((nb, path + [nb]))
        return None

    def _ensure_argos_path(self, from_code: str, to_code: str) -> None:
        import argostranslate.package as apkg
        import argostranslate.translate as atr

        from_code = from_code.strip().lower()
        to_code = to_code.strip().lower()

        with ArgosTranslateClient._install_lock:
            try:
                apkg.update_package_index()
            except Exception:
                pass
            apkg.get_available_packages()

            if self._pair_ready(from_code, to_code):
                return

            try:
                if apkg.install_package_for_language_pair(from_code, to_code):
                    atr.get_installed_languages.cache_clear()
            except Exception as e:
                raise TranslateError(f"Argos: не удалось установить пакет {from_code}→{to_code}: {e}") from e
            if self._pair_ready(from_code, to_code):
                return

            adj = self._argos_directed_graph(apkg)
            path = self._shortest_path_in_graph(adj, from_code, to_code)
            if path is None or len(path) < 2:
                raise TranslateError(
                    f"Локальный Argos не смог настроить перевод {from_code}→{to_code}: "
                    "в индексе пакетов нет ориентированной цепочки между этими языками. "
                    "Запустите LibreTranslate (Docker) или выберите другие языки."
                )

            failed: list[str] = []
            for i in range(len(path) - 1):
                a, b = path[i], path[i + 1]
                try:
                    if not apkg.install_package_for_language_pair(a, b):
                        failed.append(f"{a}→{b}")
                except Exception as e:
                    failed.append(f"{a}→{b}: {e}")
                atr.get_installed_languages.cache_clear()

            if self._pair_ready(from_code, to_code):
                return

            hint = (" Не удалось скачать сегменты: " + "; ".join(failed) + ".") if failed else ""
            raise TranslateError(
                f"Локальный Argos не активировал составной перевод {from_code}→{to_code} "
                f"(цепочка {' → '.join(path)}).{hint} "
                "Проверьте интернет, место на диске или запустите LibreTranslate (Docker)."
            )

    def _translate_sync(self, text: str, source_lang: Optional[str], target_lang: str) -> str:
        text = text or ""
        if not text.strip():
            return text

        tgt = _normalize_lang_code(target_lang)
        if not tgt:
            raise TranslateError("Не указан целевой язык.")

        src_raw = (source_lang or "").strip().lower()
        if not src_raw or src_raw == "auto":
            from_code = _infer_source_lang(text)
        else:
            from_code = _normalize_lang_code(source_lang) or "en"

        if not from_code:
            from_code = "en"
        if from_code == tgt:
            return text

        try:
            import argostranslate.translate as atr
        except ImportError as e:
            raise TranslateError(
                "Локальный переводчик (argostranslate) не установлен. Выполните: pip install -r requirements.txt"
            ) from e

        self._ensure_argos_path(from_code, tgt)

        try:
            return atr.translate(text, from_code, tgt)
        except Exception as e:
            raise TranslateError(f"Локальный Argos Translate не смог перевести: {e}") from e

    async def translate(self, text: str, *, source_lang: Optional[str], target_lang: str) -> str:
        return await asyncio.to_thread(self._translate_sync, text, source_lang, target_lang)


class AutoTranslateClient:
    """
    Сначала LibreTranslate; если сервер недоступен по сети — локальный Argos
    (прямой пакет или кратчайшая цепочка по индексу Argos, напр. ru→en→fr).
    """

    def __init__(self, libre: LibreTranslateClient) -> None:
        self._libre = libre
        self._argos: Optional[ArgosTranslateClient] = None
        self._prefer_local = False

    def _get_argos(self) -> ArgosTranslateClient:
        if self._argos is None:
            self._argos = ArgosTranslateClient()
        return self._argos

    async def translate(self, text: str, *, source_lang: Optional[str], target_lang: str) -> str:
        if self._prefer_local:
            return await self._get_argos().translate(text, source_lang=source_lang, target_lang=target_lang)
        try:
            return await self._libre.translate(text, source_lang=source_lang, target_lang=target_lang)
        except TranslateError as e:
            if _is_libre_unreachable(e):
                self._prefer_local = True
                return await self._get_argos().translate(text, source_lang=source_lang, target_lang=target_lang)
            raise
