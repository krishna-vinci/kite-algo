# broker_api/kite_auth.py

import os
import pyotp
import requests
from urllib.parse import urlparse, parse_qs

from kiteconnect import KiteConnect
from fastapi import HTTPException, Request

# load your .env
from dotenv import load_dotenv
load_dotenv()

API_KEY    = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")
USER_ID    = os.getenv("KITE_USER_ID")
PASSWORD   = os.getenv("KITE_PASSWORD")
TOTP_KEY   = os.getenv("KITE_TOTP_KEY")


def login_headless():
    session = requests.Session()

    # 1) GET the login page (cookies)
    login_url = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    })
    resp = session.get(login_url)
    resp.raise_for_status()
    approve_url = resp.url

    # 2) Submit credentials
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Referer": login_url,
        "Origin": "https://kite.zerodha.com",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
    })
    r1 = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": USER_ID, "password": PASSWORD}
    )
    if r1.status_code != 200:
        raise HTTPException(400, f"Login failed: {r1.text}")
    req_id = r1.json()["data"]["request_id"]

    # 3) Submit TOTP
    r2 = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": USER_ID,
            "request_id": req_id,
            "twofa_value": pyotp.TOTP(TOTP_KEY).now(),
            "twofa_type": "totp",
            "skip_session": "true",
        }
    )
    if r2.status_code != 200:
        raise HTTPException(400, f"2FA failed: {r2.text}")

    # 4) Fetch redirect with request_token
    final = session.get(approve_url + "&skip_session=true", allow_redirects=True)
    token = parse_qs(urlparse(final.url).query).get("request_token", [None])[0]
    if not token:
        raise HTTPException(400, "No request_token in final URL")

    # 5) Exchange for access_token via official library
    kite = KiteConnect(api_key=API_KEY)
    data = kite.generate_session(token, api_secret=API_SECRET)
    kite.set_access_token(data["access_token"])
    return kite, data["access_token"]


def get_kite(request: Request) -> KiteConnect:
    """
    FastAPI dependency: reads 'kite_at' cookie, returns configured KiteConnect.
    """
    token = request.cookies.get("kite_at")
    if not token:
        raise HTTPException(401, "Not authenticated; login first")
    client = KiteConnect(api_key=API_KEY)
    client.set_access_token(token)
    return client
