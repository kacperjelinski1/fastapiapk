import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy import text

from app.config import settings
from app.database import engine


@dataclass
class CurrentUser:
    id: str
    username: str
    display_name: str
    role: str


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    iterations = 210_000
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return (
        f"pbkdf2_sha256${iterations}$"
        f"{base64.urlsafe_b64encode(salt).decode()}$"
        f"{base64.urlsafe_b64encode(digest).decode()}"
    )


def verify_password(password: str, encoded: Optional[str]) -> bool:
    if not encoded or not encoded.startswith("pbkdf2_sha256$"):
        return False

    try:
        _, iterations, salt_b64, digest_b64 = encoded.split("$", 3)

        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )

        return hmac.compare_digest(actual, expected)

    except Exception:
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(
        data + "=" * (-len(data) % 4)
    )


def create_token(
    user_id: str,
    username: str,
    role: str,
) -> str:

    payload = {
        "uid": user_id,
        "usr": username,
        "role": role,
        "exp": int(time.time()) + settings.token_hours * 3600,
    }

    body = _b64(
        json.dumps(
            payload,
            separators=(",", ":"),
        ).encode()
    )

    sig = _b64(
        hmac.new(
            settings.api_secret.encode(),
            body.encode(),
            hashlib.sha256,
        ).digest()
    )

    return f"{body}.{sig}"


def decode_token(token: str) -> dict:
    try:
        body, sig = token.split(".", 1)

        expected = _b64(
            hmac.new(
                settings.api_secret.encode(),
                body.encode(),
                hashlib.sha256,
            ).digest()
        )

        if not hmac.compare_digest(sig, expected):
            raise ValueError("signature")

        payload = json.loads(
            _unb64(body)
        )

        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("expired")

        return payload

    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail="Nieprawidłowy lub wygasły token.",
        ) from exc


def _load_user(username: str):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT
                    id,
                    username,
                    display_name,
                    role,
                    password_hash,
                    is_active
                FROM core.app_users
                WHERE username = :username
                LIMIT 1
                """
            ),
            {
                "username": username,
            },
        ).mappings().first()


def get_current_user(
    authorization: Optional[str] = Header(default=None),
) -> CurrentUser:

    # Jeżeli aplikacja przesłała token,
    # używamy użytkownika z tego tokenu.
    if (
        authorization
        and authorization.lower().startswith("bearer ")
    ):
        token = authorization.split(" ", 1)[1].strip()

        payload = decode_token(token)

        row = _load_user(
            payload.get("usr", "")
        )

        if row is None or not row["is_active"]:
            raise HTTPException(
                status_code=401,
                detail="Użytkownik jest nieaktywny.",
            )

        return CurrentUser(
            str(row["id"]),
            row["username"],
            row["display_name"],
            str(row["role"]),
        )

    # Bez tokenu zachowujemy dotychczasowe
    # działanie aplikacji Owner.
    if not settings.auth_required:
        row = _load_user(
            settings.owner_username
        )

        if row is None:
            raise HTTPException(
                status_code=500,
                detail="Brak użytkownika właściciela w bazie.",
            )

        return CurrentUser(
            str(row["id"]),
            row["username"],
            row["display_name"],
            str(row["role"]),
        )

    raise HTTPException(
        status_code=401,
        detail="Brak autoryzacji.",
    )


def require_owner(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:

    if user.role not in ("OWNER", "ADMIN"):
        raise HTTPException(
            status_code=403,
            detail="Brak uprawnień właściciela.",
        )

    return user


def require_staff(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:

    if user.role not in (
        "OWNER",
        "ADMIN",
        "RECEPTION",
        "TECHNICIAN",
    ):
        raise HTTPException(
            status_code=403,
            detail="Brak uprawnień.",
        )

    return user
