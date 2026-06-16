import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "wei_auth.db"

SESSION_TTL_DAYS = int(os.getenv("AUTH_SESSION_TTL_DAYS", "7"))
PBKDF2_ITERATIONS = int(os.getenv("AUTH_PBKDF2_ITERATIONS", "200000"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_auth_db_path() -> Path:
    raw = os.getenv("AUTH_DB_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_DB_PATH


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    path = get_auth_db_path()
    ensure_parent_dir(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iteration_str, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iteration_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except Exception:
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def get_user_by_username(username: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, username, password_hash, display_name, role,
                   created_at, updated_at
            FROM users
            WHERE lower(username) = lower(?)
            """,
            (username,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, username, display_name, role,
                   created_at, updated_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def create_user(
    username: str,
    password: str,
    display_name: str | None = None,
    role: str = "user",
) -> int:
    now = utcnow().isoformat()
    display_name = (display_name or username).strip() or username
    password_hash = hash_password(password)
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users (
                username, password_hash, display_name, role,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                username.strip(),
                password_hash,
                display_name,
                role,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def count_users() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
    return int(row["count"]) if row else 0


def ensure_seed_admin() -> None:
    username = os.getenv("AUTH_ADMIN_USERNAME", "").strip()
    password = os.getenv("AUTH_ADMIN_PASSWORD", "").strip()
    if not username or not password:
        return
    if count_users() > 0:
        return
    create_user(
        username=username,
        password=password,
        display_name=os.getenv("AUTH_ADMIN_DISPLAY_NAME", username).strip() or username,
        role="admin",
    )


def create_session(user_id: int) -> tuple[str, datetime]:
    session_id = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(days=SESSION_TTL_DAYS)
    now = utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sessions (id, user_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, user_id, expires_at.isoformat(), now),
        )
    return session_id, expires_at


def delete_session(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def purge_expired_sessions() -> None:
    now = utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))


def get_session_user(session_id: str) -> dict[str, Any] | None:
    purge_expired_sessions()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                u.id,
                u.username,
                u.display_name,
                u.role,
                s.expires_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None
