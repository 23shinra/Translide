import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, HTTPException
from jose import JWTError, jwt
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext

from .database import get_conn, utc_now_iso

SECRET_KEY = os.getenv("JWT_SECRET", "dev-secret-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
security = HTTPBearer(auto_error=False)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\u0400-\u04FF]{3,32}$")
NAME_RE = re.compile(r"^[\u0400-\u04FFa-zA-Z][\u0400-\u04FFa-zA-Z \-']{0,39}$")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: int, username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def try_decode_token(token: str) -> Optional[dict[str, Any]]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def get_user_by_id(user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, created_at, first_name, last_name FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "created_at": row["created_at"],
            "first_name": row.get("first_name") if hasattr(row, "get") else row["first_name"],
            "last_name": row.get("last_name") if hasattr(row, "get") else row["last_name"],
        }


def get_user_by_username(username: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, created_at, first_name, last_name FROM users WHERE username = ? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "password_hash": row["password_hash"],
            "created_at": row["created_at"],
            "first_name": row.get("first_name") if hasattr(row, "get") else row["first_name"],
            "last_name": row.get("last_name") if hasattr(row, "get") else row["last_name"],
        }


def create_user(username: str, password: str, first_name: str, last_name: str) -> dict:
    if not USERNAME_RE.match(username.strip()):
        raise HTTPException(
            status_code=400,
            detail="Логин: 3–32 символа, буквы/цифры/подчёркивание.",
        )
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    if not first_name or not last_name:
        raise HTTPException(status_code=400, detail="Укажи имя и фамилию.")
    if not NAME_RE.match(first_name) or not NAME_RE.match(last_name):
        raise HTTPException(status_code=400, detail="Имя/фамилия: только буквы, пробелы и дефис (до 40 символов).")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Пароль не короче 6 символов.")
    h = hash_password(password)
    now = utc_now_iso()
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, created_at, first_name, last_name) VALUES (?, ?, ?, ?, ?)",
                (username.strip(), h, now, first_name, last_name),
            )
            conn.commit()
            uid = cur.lastrowid
        except Exception as e:
            conn.rollback()
            if "UNIQUE" in str(e).upper():
                raise HTTPException(status_code=400, detail="Такой логин уже занят.") from e
            raise
    return {
        "id": uid,
        "username": username.strip(),
        "created_at": now,
        "first_name": first_name,
        "last_name": last_name,
    }


def ensure_demo_user() -> None:
    """
    Creates a demo user (username=123456, password=123456) if missing.
    Intended only for local testing.
    """
    username = "123456"
    password = "123456"
    existing = get_user_by_username(username)
    if existing:
        # Backfill names for older DBs.
        if not (existing.get("first_name") and existing.get("last_name")):
            with get_conn() as conn:
                try:
                    conn.execute(
                        "UPDATE users SET first_name = COALESCE(first_name, ?), last_name = COALESCE(last_name, ?) WHERE username = ? COLLATE NOCASE",
                        ("Тест", "Пользователь", username),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
        return
    now = utc_now_iso()
    h = hash_password(password)
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at, first_name, last_name) VALUES (?, ?, ?, ?, ?)",
                (username, h, now, "Тест", "Пользователь"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            # If a race created it, ignore.
            return


def authenticate(username: str, password: str) -> dict:
    u = get_user_by_username(username)
    if not u or not verify_password(password, u["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль.")
    return {
        "id": u["id"],
        "username": u["username"],
        "created_at": u["created_at"],
        "first_name": u.get("first_name"),
        "last_name": u.get("last_name"),
    }


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    if not credentials or credentials.scheme.lower() != "bearer":
        return None
    payload = try_decode_token(credentials.credentials)
    if not payload:
        return None
    try:
        uid = int(payload.get("sub", 0))
    except (TypeError, ValueError):
        return None
    user = get_user_by_id(uid)
    return user


async def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> dict:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Требуется авторизация.")
    payload = try_decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Недействительный токен.")
    try:
        uid = int(payload.get("sub", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Недействительный токен.")
    user = get_user_by_id(uid)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не найден.")
    return user


def add_history(user_id: int, original_filename: str, target_lang: str, output_filename: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO translation_history (user_id, original_filename, target_lang, output_filename, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, original_filename[:500], target_lang[:32], output_filename[:500], utc_now_iso()),
        )
        conn.commit()


def list_history(user_id: int, limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, original_filename, target_lang, output_filename, created_at
            FROM translation_history
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_history(user_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM translation_history WHERE user_id = ?", (user_id,))
        conn.commit()
        return int(cur.rowcount or 0)
