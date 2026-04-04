import hmac
import logging
import os
import secrets
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import HTTPException, Request, Response


logger = logging.getLogger(__name__)

ACCESS_COOKIE_NAME = "app_access_token"
REFRESH_COOKIE_NAME = "app_refresh_token"


@dataclass
class AppUser:
    username: str
    role: str = "admin"


def _jwt_secret() -> str:
    secret = os.getenv("APP_JWT_SECRET") or os.getenv("JWT_SECRET") or "dev-insecure-change-me"
    if secret == "dev-insecure-change-me":
        logger.warning("APP_JWT_SECRET is not set; using insecure development secret")
    return secret


def _access_ttl_minutes() -> int:
    return max(5, int(os.getenv("APP_JWT_ACCESS_MINUTES", "15")))


def _refresh_ttl_days() -> int:
    return max(1, int(os.getenv("APP_JWT_REFRESH_DAYS", "14")))


def _cookie_secure(request: Optional[Request] = None) -> bool:
    if request is not None:
        forwarded_proto = request.headers.get("x-forwarded-proto")
        scheme = forwarded_proto or request.url.scheme
        return scheme == "https"
    return os.getenv("APP_COOKIE_SECURE", "false").lower() == "true"


def get_configured_app_username() -> str:
    return os.getenv("APP_ADMIN_USERNAME", "admin")


def _get_password_hash_from_env() -> Optional[str]:
    direct = os.getenv("APP_ADMIN_PASSWORD_HASH")
    if direct:
        direct = direct.strip().replace("$$", "$")
        if "$" in direct:
            return direct
        try:
            decoded = base64.b64decode(direct.encode("utf-8")).decode("utf-8").strip()
            if decoded.startswith("pbkdf2_sha256$"):
                return decoded
        except Exception:
            pass

    b64_value = os.getenv("APP_ADMIN_PASSWORD_HASH_B64")
    if b64_value:
        try:
            return base64.b64decode(b64_value.encode("utf-8")).decode("utf-8").strip()
        except Exception:
            logger.error("Invalid APP_ADMIN_PASSWORD_HASH_B64 value")

    file_path = os.getenv("APP_ADMIN_PASSWORD_HASH_FILE")
    if file_path:
        try:
            return open(file_path, "r", encoding="utf-8").read().strip()
        except Exception as exc:
            logger.error("Failed to read APP_ADMIN_PASSWORD_HASH_FILE %s: %s", file_path, exc)

    return None


def hash_password(password: str, *, salt: Optional[str] = None, iterations: int = 200000) -> str:
    salt = salt or secrets.token_hex(16)
    digest = __import__("hashlib").pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def _verify_password_hash(password: str, encoded_hash: str) -> bool:
    try:
        scheme, iterations, salt, digest = encoded_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        candidate = hash_password(password, salt=salt, iterations=int(iterations))
        return hmac.compare_digest(candidate, encoded_hash)
    except Exception:
        return False


def verify_app_credentials(username: str, password: str) -> bool:
    expected_username = get_configured_app_username()
    expected_password_hash = _get_password_hash_from_env()
    expected_password = os.getenv("APP_ADMIN_PASSWORD", "admin123")
    if not hmac.compare_digest(username, expected_username):
        return False
    if expected_password_hash:
        return _verify_password_hash(password, expected_password_hash)
    return hmac.compare_digest(password, expected_password)


def _encode_token(subject: str, role: str, token_type: str, expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def create_access_token(user: AppUser) -> str:
    return _encode_token(user.username, user.role, "access", timedelta(minutes=_access_ttl_minutes()))


def create_refresh_token(user: AppUser) -> str:
    return _encode_token(user.username, user.role, "refresh", timedelta(days=_refresh_ttl_days()))


def _decode_token(token: str, expected_type: str) -> AppUser:
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail=f"{expected_type.title()} token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    token_type = payload.get("type")
    if token_type != expected_type:
        raise HTTPException(status_code=401, detail=f"Invalid token type: expected {expected_type}")
    subject = payload.get("sub")
    if not subject:
        raise HTTPException(status_code=401, detail="Token subject missing")
    return AppUser(username=str(subject), role=str(payload.get("role") or "admin"))


def get_optional_app_user(request: Request) -> Optional[AppUser]:
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        return None
    try:
        return _decode_token(token, "access")
    except HTTPException:
        return None


def require_app_user(request: Request) -> AppUser:
    user = get_optional_app_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="App authentication required")
    return user


def get_refresh_user(request: Request) -> AppUser:
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Refresh token missing")
    return _decode_token(token, "refresh")


def issue_auth_cookies(response: Response, request: Request, user: AppUser) -> None:
    secure = _cookie_secure(request)
    same_site = "none" if secure else "lax"
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        create_access_token(user),
        httponly=True,
        secure=secure,
        samesite=same_site,
        max_age=_access_ttl_minutes() * 60,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        create_refresh_token(user),
        httponly=True,
        secure=secure,
        samesite=same_site,
        max_age=_refresh_ttl_days() * 24 * 60 * 60,
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/")
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/")


def auth_exempt_path(path: str) -> bool:
    return path in {
        "/api/auth/login",
        "/api/auth/refresh",
        "/api/auth/session-status",
    }
