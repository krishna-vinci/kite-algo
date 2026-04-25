# broker_api/kite_auth.py

import logging
import os
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from dotenv import load_dotenv
from kiteconnect import KiteConnect


load_dotenv()

logger = logging.getLogger(__name__)

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")
USER_ID = os.getenv("KITE_USER_ID")
PASSWORD = os.getenv("KITE_PASSWORD")
TOTP_KEY = os.getenv("KITE_TOTP_KEY")
HEADLESS_LOGIN_TIMEOUT_SECONDS = max(5, int(os.getenv("KITE_HEADLESS_LOGIN_TIMEOUT_SECONDS", "15")))


def _require_env(name: str, value: str | None) -> str:
    if value and value.strip():
        return value.strip()
    raise ValueError(f"Missing required environment variable: {name}")


def _read_json(response: requests.Response) -> dict:
    try:
        return response.json()
    except Exception as exc:
        raise ValueError(f"Unexpected non-JSON response from Kite login flow: {response.text[:200]}") from exc


def login_headless() -> tuple[KiteConnect, str]:
    api_key = _require_env("KITE_API_KEY", API_KEY)
    api_secret = _require_env("KITE_API_SECRET", API_SECRET)
    user_id = _require_env("KITE_USER_ID", USER_ID)
    password = _require_env("KITE_PASSWORD", PASSWORD)
    totp_key = _require_env("KITE_TOTP_KEY", TOTP_KEY)

    timeout = HEADLESS_LOGIN_TIMEOUT_SECONDS
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    resp = session.get(login_url, timeout=timeout)
    resp.raise_for_status()
    approve_url = resp.url

    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Referer": login_url,
            "Origin": "https://kite.zerodha.com",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
    )
    login_response = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": user_id, "password": password},
        timeout=timeout,
    )
    if login_response.status_code != 200:
        raise ValueError(f"Login failed: {login_response.text[:200]}")

    login_payload = _read_json(login_response)
    request_id = (login_payload.get("data") or {}).get("request_id")
    if not request_id:
        raise ValueError("Login succeeded but request_id was missing")

    twofa_response = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": user_id,
            "request_id": request_id,
            "twofa_value": pyotp.TOTP(totp_key).now(),
            "twofa_type": "totp",
            "skip_session": "true",
        },
        timeout=timeout,
    )
    if twofa_response.status_code != 200:
        raise ValueError(f"2FA failed: {twofa_response.text[:200]}")

    final = session.get(f"{approve_url}&skip_session=true", allow_redirects=True, timeout=timeout)
    request_token = parse_qs(urlparse(final.url).query).get("request_token", [None])[0]
    if not request_token:
        raise ValueError("No request_token in final URL")

    kite = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data.get("access_token")
    if not access_token:
        raise ValueError("Kite session generation did not return an access_token")

    kite.set_access_token(access_token)
    logger.info("Headless Kite login succeeded (..%s)", access_token[-6:])
    return kite, access_token
