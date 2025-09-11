import httpx
import pyotp
import asyncio
import json
from urllib import parse
from fyers_apiv3 import fyersModel

# Your Client Information
FY_ID = "XL03389"  # Your Fyers ID
APP_ID_TYPE = "2"  # Keep default as 2, It denotes web login
TOTP_KEY = "UV6M4FNPC6T4M5NDYEZ6IBJJ45ROD7JJ"  # TOTP secret key
PIN = "1234"  # User pin for Fyers account
APP_ID = "TJDIKK4H13"  # App ID from myapi dashboard
APP_SECRET = "60LJUYJ1H9"  # Your secret key
REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"  # Redirect URL from the app
APP_TYPE = "100"
APP_ID_HASH = "5c1375c7f1a4c113d247df0d80ef9475468ed85fb0accab6062fb8f45e369add"

# API endpoints
BASE_URL = "https://api-t2.fyers.in/vagator/v2"
BASE_URL_2 = "https://api-t1.fyers.in/api/v3"
URL_SEND_LOGIN_OTP = BASE_URL + "/send_login_otp"
URL_VERIFY_TOTP = BASE_URL + "/verify_otp"
URL_VERIFY_PIN = BASE_URL + "/verify_pin"
URL_TOKEN = BASE_URL_2 + "/token"
URL_VALIDATE_AUTH_CODE = BASE_URL_2 + "/validate-authcode"

SUCCESS = 1
ERROR = -1


async def send_login_otp(fy_id, app_id):
    async with httpx.AsyncClient() as client:
        payload = {
            "fy_id": fy_id,
            "app_id": app_id
        }
        response = await client.post(URL_SEND_LOGIN_OTP, json=payload)
        if response.status_code != 200:
            return ERROR, response.text
        result = response.json()
        request_key = result["request_key"]
        return SUCCESS, request_key


async def generate_totp(secret):
    try:
        generated_totp = pyotp.TOTP(secret).now()
        return SUCCESS, generated_totp
    except Exception as e:
        return ERROR, str(e)


async def verify_totp(request_key, totp):
    async with httpx.AsyncClient() as client:
        payload = {
            "request_key": request_key,
            "otp": totp
        }
        result_string = await client.post(URL_VERIFY_TOTP, json=payload)
        if result_string.status_code != 200:
            return ERROR, result_string.text
        result = result_string.json()
        request_key = result["request_key"]
        return SUCCESS, request_key


async def verify_PIN(request_key, pin):
    async with httpx.AsyncClient() as client:
        payload = {
            "request_key": request_key,
            "identity_type": "pin",
            "identifier": pin
        }
        result_string = await client.post(URL_VERIFY_PIN, json=payload)
        if result_string.status_code != 200:
            return ERROR, result_string.text
        result = result_string.json()
        access_token = result["data"]["access_token"]
        return SUCCESS, access_token


async def token(fy_id, app_id, redirect_uri, app_type, access_token):
    async with httpx.AsyncClient() as client:
        payload = {
            "fyers_id": fy_id,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "appType": app_type,
            "code_challenge": "",
            "state": "sample_state",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True
        }
        headers = {'Authorization': f'Bearer {access_token}'}
        result_string = await client.post(URL_TOKEN, json=payload, headers=headers)
        # expected 308 redirect (as original code)
        if result_string.status_code != 308:
            return ERROR, result_string.text
        result = result_string.json()
        url = result["Url"]
        auth_code = parse.parse_qs(parse.urlparse(url).query)['auth_code'][0]
        return SUCCESS, auth_code


async def validate_authcode(app_id_hash, auth_code):
    async with httpx.AsyncClient() as client:
        payload = {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": auth_code,
        }
        result_string = await client.post(URL_VALIDATE_AUTH_CODE, json=payload)
        if result_string.status_code != 200:
            return ERROR, result_string.text
        result = result_string.json()
        access_token = result["access_token"]
        return SUCCESS, access_token


async def perform_login():
    """
    Orchestrates the login flow. ALWAYS returns a dict:
      - on success: {"status": "success", "access_token": ...}
      - on failure: {"status": "error", "message": ...}
    This makes callers (e.g. your /login endpoint) able to rely on a consistent shape.
    """
    send_otp_result = await send_login_otp(FY_ID, APP_ID_TYPE)
    if send_otp_result[0] != SUCCESS:
        return {"status": "error", "message": send_otp_result[1]}

    generate_totp_result = await generate_totp(TOTP_KEY)
    if generate_totp_result[0] != SUCCESS:
        return {"status": "error", "message": generate_totp_result[1]}

    verify_totp_result = await verify_totp(send_otp_result[1], generate_totp_result[1])
    if verify_totp_result[0] != SUCCESS:
        return {"status": "error", "message": verify_totp_result[1]}

    verify_pin_result = await verify_PIN(verify_totp_result[1], PIN)
    if verify_pin_result[0] != SUCCESS:
        return {"status": "error", "message": verify_pin_result[1]}

    token_result = await token(FY_ID, APP_ID, REDIRECT_URI, APP_TYPE, verify_pin_result[1])
    if token_result[0] != SUCCESS:
        return {"status": "error", "message": token_result[1]}

    validate_authcode_result = await validate_authcode(APP_ID_HASH, token_result[1])
    if validate_authcode_result[0] != SUCCESS:
        return {"status": "error", "message": validate_authcode_result[1]}

    return {"status": "success", "access_token": validate_authcode_result[1]}