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


def _describe_connect_redirect(url: str) -> str:
    """Return a safe, compact description of Kite's final Connect redirect."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    safe_params = {
        key: values[0]
        for key, values in query.items()
        if key in {"status", "message", "error_type", "action", "type"} and values
    }
    parts = [f"path={parsed.path or '/'}"]
    if safe_params:
        parts.append("params=" + ", ".join(f"{key}={value}" for key, value in safe_params.items()))
    return "; ".join(parts)


def _request_token_from_url(url: str) -> str | None:
    return parse_qs(urlparse(url).query).get("request_token", [None])[0]


def _approve_connect_app(
    session: requests.Session,
    approve_url: str,
    api_key: str,
    timeout: int,
    current_url: str | None = None,
) -> requests.Response | None:
    """
    Approve the Kite Connect app when Zerodha returns the SPA authorisation page.

    Existing/previously-approved accounts often redirect straight to the app redirect URL
    with request_token after 2FA. Newly switched accounts can instead land on
    /connect/authorize and require the same hidden form POST the Kite web app performs.
    """
    sess_id = None
    if current_url:
        sess_id = parse_qs(urlparse(current_url).query).get("sess_id", [None])[0]
    if not sess_id:
        sess_id = parse_qs(urlparse(approve_url).query).get("sess_id", [None])[0]
    public_token = session.cookies.get("public_token")
    if not sess_id or not public_token:
        logger.debug(
            "Kite Connect manual approval fallback unavailable (sess_id=%s, public_token=%s)",
            bool(sess_id),
            bool(public_token),
        )
        return None

    return session.post(
        "https://kite.zerodha.com/connect/finish",
        data={"sess_id": sess_id, "api_key": api_key, "authorize": public_token},
        allow_redirects=True,
        timeout=timeout,
    )


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
    if final.status_code >= 400:
        raise ValueError(
            "Kite Connect approval failed "
            f"(HTTP {final.status_code}; {_describe_connect_redirect(final.url)})"
        )

    request_token = _request_token_from_url(final.url)
    if not request_token:
        approved = _approve_connect_app(session, approve_url, api_key, timeout, current_url=final.url)
        if approved is not None:
            if approved.status_code >= 400:
                raise ValueError(
                    "Kite Connect app approval failed "
                    f"(HTTP {approved.status_code}; {_describe_connect_redirect(approved.url)})"
                )
            final = approved
            request_token = _request_token_from_url(final.url)

    if not request_token:
        raise ValueError(
            "Kite Connect approval did not return request_token "
            f"({_describe_connect_redirect(final.url)}). Check that KITE_API_KEY belongs to an active Kite Connect app, "
            "that the redirect URL is valid, and that the configured KITE_USER_ID/TOTP account is allowed to approve the app."
        )

    kite = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data.get("access_token")
    if not access_token:
        raise ValueError("Kite session generation did not return an access_token")

    kite.set_access_token(access_token)
    logger.info("Headless Kite login succeeded (..%s)", access_token[-6:])
    return kite, access_token
