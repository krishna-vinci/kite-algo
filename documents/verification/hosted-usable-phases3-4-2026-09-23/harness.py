#!/usr/bin/env python
"""Isolated browser QA for the hosted-strategy composer and operations UI.

This is a QA harness, not a product surface. It brings up the shape of
environment the campaign allows for verification:

* a **disposable** PostgreSQL database on the shared test instance (port 15433),
  created fresh, migrated with ``alembic upgrade head`` and dropped afterwards;
* the **real** operator routers (``/api/auth``, ``/api/strategies``) served over
  loopback by ``uvicorn``, with the market boundary faked (a synthetic quote) -
  no broker, no notifications, no production database;
* the real ``frontend-next`` dev server pointed at that API through its existing
  ``/api`` rewrite;
* headless Chrome driven over CDP with real DOM events, capturing the
  screenshots recorded next to this file.

Nothing here is a substitute for production certification, and nothing here
touches port 15432, the running production containers, the broker, or any
notification channel.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
ADMIN_DSN = os.environ.get(
    "UI_QA_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)
GENERATION = str(uuid.uuid4())
OPERATOR_USER = "ui-qa-operator"
OPERATOR_PASSWORD = "ui-qa-password"
SHOTS: list = []


def log(message: str) -> None:
    print(f"[ui-qa] {message}", flush=True)


# ------------------------------------------------------------------ database


def create_database() -> tuple:
    import psycopg2

    name = f"kite_ui_qa_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    root, _sep, _db = ADMIN_DSN.rpartition("/")
    return name, f"{root}/{name}"


def drop_database(name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


def migrate(dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    os.environ["DATABASE_URL"] = dsn
    cfg = Config(str(REPO / "backend" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    cfg.set_main_option("script_location", str(REPO / "backend" / "alembic"))
    command.upgrade(cfg, "head")


def seed_catalog(session_factory) -> str:
    """One published generation with one resolvable instrument."""
    from sqlalchemy import text

    with session_factory() as session:
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES (:gen, 'published', NOW())"
            ),
            {"gen": GENERATION},
        )
        instrument_id = str(uuid.uuid4())
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_records "
                "(instrument_id, identity_key, public_key, exchange, tradingsymbol, "
                " lifecycle_status, instrument_type, lot_size, tick_size, current_generation_id) "
                "VALUES (:iid, 'NSE:RELIANCE', 'NSE:RELIANCE', 'NSE', 'RELIANCE', "
                " 'active', 'EQ', 1, 0.05, :gen)"
            ),
            {"gen": GENERATION, "iid": instrument_id},
        )
        session.execute(
            text(
                "INSERT INTO public.instrument_broker_mappings "
                "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
                " valid_from_generation, is_current) "
                "VALUES (:mid, :iid, 'kite', 'NSE', 'RELIANCE', 738561, :gen, TRUE)"
            ),
            {"gen": GENERATION, "iid": instrument_id, "mid": str(uuid.uuid4())},
        )
        session.commit()
    return instrument_id


class SyntheticQuotes:
    """The market boundary: one deterministic price. No broker, no Redis."""

    def __init__(self, price: float = 1500.0) -> None:
        self.price = float(price)

    async def get_tick(self, token: int) -> Dict[str, Any]:
        return {"instrument_token": int(token), "last_price": self.price}

    async def get_last_price(self, token: int) -> float:
        return self.price


def seed_waiting_request(session_factory, *, strategy_id: str, account_id: str) -> str:
    """One real pending execution request, written into the platform's tables.

    A live child would take a whole supervisor plus a broker boundary to produce
    this row; the row itself has exactly the shape the Phase-2 service writes, so
    the operator panel reads it through the ordinary API.
    """
    import psycopg2

    request_id = f"qa-request-{uuid.uuid4().hex[:8]}"
    proposal_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, version, source_sha256 FROM public.hosted_strategy_versions "
                "WHERE strategy_id = %s ORDER BY version DESC LIMIT 1",
                (strategy_id,),
            )
            version_id, version_number, source_sha256 = cur.fetchone()
            cur.execute(
                "SELECT policy_hash FROM public.hosted_execution_grants "
                "WHERE strategy_id = %s ORDER BY issued_at DESC LIMIT 1",
                (strategy_id,),
            )
            row = cur.fetchone()
            policy_hash = row[0] if row else ("0" * 64)
            cur.execute(
                "INSERT INTO public.strategy_proposals "
                "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, job_id, "
                " strategy_run_id, target_kind, payload, payload_sha256, status) "
                "VALUES (%s, %s, %s, 'qa-eval-1', 'run_now', NULL, 'qa-run-1', "
                " 'single_instrument', %s, %s, 'validated')",
                (
                    proposal_id,
                    strategy_id,
                    account_id,
                    json.dumps(
                        {
                            "instrument_token": 738561,
                            "exchange": "NSE",
                            "tradingsymbol": "RELIANCE",
                            "product": "CNC",
                            "target_quantity": 5,
                        }
                    ),
                    "f" * 64,
                ),
            )
            cur.execute(
                "INSERT INTO public.strategy_plans "
                "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, logical_plan, "
                " resolved_plan, pinned_catalog_generation) "
                "VALUES (%s, %s, %s, %s, 'single_instrument', %s, %s, %s, %s)",
                (
                    plan_id,
                    proposal_id,
                    strategy_id,
                    account_id,
                    "e" * 64,
                    json.dumps({"target_kind": "single_instrument"}),
                    json.dumps(
                        {
                            "target_kind": "single_instrument",
                            "catalog_generation": GENERATION,
                            "legs": [
                                {
                                    "instrument_id": "qa-instrument",
                                    "exchange": "NSE",
                                    "tradingsymbol": "RELIANCE",
                                    "product": "CNC",
                                    "signed_quantity": 5,
                                    "reference_price": 1500.0,
                                }
                            ],
                        }
                    ),
                    GENERATION,
                ),
            )
            cur.execute(
                "INSERT INTO public.hosted_execution_requests "
                "(request_id, owner_id, strategy_id, canonical_strategy_id, account_id, "
                " execution_environment, strategy_run_id, job_id, attempt, lease_epoch, version_id, "
                " version_number, source_sha256, policy_hash, evaluation_id, plan_id, plan_hash, "
                " authorization_mode, status, idempotency_key, request_hash) "
                "VALUES (%s, %s, %s, %s, %s, 'paper', 'qa-run-1', NULL, 1, 0, %s, %s, %s, %s, "
                " 'qa-eval-1', %s, %s, 'approval_based', 'awaiting_approval', %s, %s)",
                (
                    request_id,
                    f"app:{OPERATOR_USER}",
                    strategy_id,
                    strategy_id,
                    account_id,
                    version_id,
                    version_number,
                    source_sha256,
                    policy_hash,
                    plan_id,
                    "e" * 64,
                    f"qa-key-{uuid.uuid4().hex[:8]}",
                    "a" * 64,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return request_id


# ------------------------------------------------------------------ api server


def build_app(session_factory):
    from fastapi import FastAPI

    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.api.routers import auth as auth_module
    from backend.api.routers import strategies as strategies_module
    from backend.paper_runtime.repository import SqlAlchemyPaperRepository
    from backend.paper_runtime.service import PaperTradingService
    from backend.strategies.attribution import SqlAttributionStore

    app = FastAPI(title="hosted usable platform UI QA API")
    app.include_router(auth_module.router, prefix="/api")
    app.include_router(strategies_module.router, prefix="/api")
    app.state.strategies_session_factory = session_factory
    app.state.attribution_store = SqlAttributionStore(session_factory=session_factory)
    app.state.algo_worker_repository = SqlAlchemyAlgoWorkerRepository(session_factory)
    app.state.paper_runtime_service = PaperTradingService(
        repository=SqlAlchemyPaperRepository(session_factory),
        market_data_runtime=SyntheticQuotes(),
    )
    return app


def wait_for_http(url: str, *, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=5) as response:
                if response.status < 500:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return True
        except Exception:  # noqa: BLE001 - connection refused while booting
            pass
        time.sleep(1.0)
    return False


# ------------------------------------------------------------------ CDP client


class Chrome:
    """A very small CDP client: one page, real DOM input, screenshots."""

    def __init__(self, width: int = 1440, height: int = 950) -> None:
        from websockets.sync.client import connect

        self._connect = connect
        self.user_data_dir = tempfile.mkdtemp(prefix="ui-qa-chrome-")
        self.binary = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if not self.binary:
            raise RuntimeError("google-chrome not found")
        self.process = subprocess.Popen(
            [
                self.binary,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--no-first-run",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                f"--user-data-dir={self.user_data_dir}",
                "--remote-debugging-port=0",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        endpoint = self._wait_for_endpoint()
        self.ws = self._connect(endpoint, max_size=64 * 1024 * 1024, open_timeout=30)
        self._id = 0
        log(f"chrome {self._browser_call('Browser.getVersion').get('product')}")
        target = self._browser_call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = self._browser_call(
            "Target.attachToTarget", {"targetId": target, "flatten": True}
        )["sessionId"]
        self._call("Page.enable")
        self._call("Runtime.enable")
        self.set_viewport(width, height)

    def _wait_for_endpoint(self) -> str:
        port_file = Path(self.user_data_dir) / "DevToolsActivePort"
        deadline = time.time() + 30
        while time.time() < deadline:
            if port_file.exists():
                lines = port_file.read_text().splitlines()
                if len(lines) >= 2:
                    return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
            time.sleep(0.2)
        raise RuntimeError("chrome DevTools endpoint did not appear")

    def _browser_call(self, method: str, params: Optional[dict] = None) -> dict:
        self._id += 1
        message_id = self._id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv(timeout=30))
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message.get("result", {})

    def _call(self, method: str, params: Optional[dict] = None, *, timeout: float = 60.0) -> dict:
        self._id += 1
        message_id = self._id
        self.ws.send(
            json.dumps(
                {
                    "id": message_id,
                    "method": method,
                    "params": params or {},
                    "sessionId": self.session,
                }
            )
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv(timeout=max(1.0, deadline - time.time()))
            message = json.loads(raw)
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self._call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"])[:500])
        return result.get("result", {}).get("value")

    def set_viewport(self, width: int, height: int, *, mobile: Optional[bool] = None) -> None:
        """Set the emulated viewport.

        ``mobile`` defaults to "narrow viewport means mobile", but the app has no
        viewport meta, so mobile emulation lets Chrome pick a wider layout
        viewport and the measurement stops being about this page's own width.
        The narrow-width pass asks for ``mobile=False`` for that reason: a real
        390px layout viewport, where content that does not fit reports itself.
        """
        self._call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": width < 700 if mobile is None else mobile,
            },
        )

    def navigate(self, url: str, *, timeout: float = 60.0) -> None:
        self._call("Page.navigate", {"url": url}, timeout=timeout)
        self.wait_for("document.readyState === 'complete'", timeout=timeout)

    def wait_for(self, expression: str, *, timeout: float = 30.0, interval: float = 0.25) -> Any:
        deadline = time.time() + timeout
        last: Any = None
        while time.time() < deadline:
            last = self.evaluate(expression)
            if last:
                return last
            time.sleep(interval)
        raise TimeoutError(f"timed out waiting for: {expression} (last={last!r})")

    def insert_text(self, text: str) -> None:
        self._call("Input.insertText", {"text": text})

    def key(self, key: str, code: str, key_code: int) -> None:
        for event_type in ("keyDown", "keyUp"):
            self._call(
                "Input.dispatchKeyEvent",
                {
                    "type": event_type,
                    "key": key,
                    "code": code,
                    "windowsVirtualKeyCode": key_code,
                    "nativeVirtualKeyCode": key_code,
                },
            )

    def click_text(self, text: str) -> None:
        """Click the first enabled control whose label is (or contains) `text`.

        An exact label wins over a containment match, so "Plan" cannot pick
        "Approve this plan" and a two-line option button (label plus its
        explanation) is still reachable by its label.
        """
        clicked = self.evaluate(
            """
            (text => {
              const controls = [...document.querySelectorAll('button, a')]
                .filter((candidate) => !candidate.disabled);
              const node =
                controls.find((candidate) => candidate.textContent.trim() === text) ||
                controls.find((candidate) => candidate.textContent.includes(text));
              if (!node) return false;
              node.scrollIntoView({ block: 'center' });
              node.click();
              return true;
            })(%r)
            """
            % text
        )
        if not clicked:
            raise RuntimeError(f"no clickable element with text {text!r}")

    def click_within(self, container_selector: str, text: str) -> None:
        """Click a control by label INSIDE one panel.

        Several surfaces legitimately share a label ("Disable" appears on both
        the strategy header and the schedule), so the QA script scopes the click
        to the panel whose state it is asserting.
        """
        clicked = self.evaluate(
            """
            ((containerSelector, text) => {
              const anchor = document.querySelector(containerSelector);
              if (!anchor) return false;
              const container = anchor.closest('section, [data-slot="card"]') || document.body;
              const controls = [...container.querySelectorAll('button, a')]
                .filter((candidate) => !candidate.disabled);
              const node =
                controls.find((candidate) => candidate.textContent.trim() === text) ||
                controls.find((candidate) => candidate.textContent.includes(text));
              if (!node) return false;
              node.scrollIntoView({ block: 'center' });
              node.click();
              return true;
            })(%r, %r)
            """
            % (container_selector, text)
        )
        if not clicked:
            raise RuntimeError(f"no clickable {text!r} inside {container_selector!r}")

    def set_value(self, selector: str, value: str) -> None:
        ok = self.evaluate(
            """
            ((selector, value) => {
              const input = document.querySelector(selector);
              if (!input) return false;
              input.scrollIntoView({ block: 'center' });
              const proto = input instanceof window.HTMLTextAreaElement
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
              setter.call(input, value);
              input.dispatchEvent(new Event('input', { bubbles: true }));
              return true;
            })(%r, %r)
            """
            % (selector, value)
        )
        if not ok:
            raise RuntimeError(f"no field for {selector}")

    def screenshot(self, name: str) -> Path:
        result = self._call("Page.captureScreenshot", {"format": "png"})
        path = HERE / name
        path.write_bytes(base64.b64decode(result["data"]))
        SHOTS.append({"file": name, "captured_at": time.time()})
        log(f"screenshot {name}")
        return path

    def scroll_to(self, selector: str) -> None:
        """Frame a panel before capturing it, so the shot shows the state."""
        found = self.evaluate(
            """
            (selector => {
              const node = document.querySelector(selector);
              if (!node) return false;
              node.scrollIntoView({ block: 'center' });
              return true;
            })(%r)
            """
            % selector
        )
        if not found:
            raise RuntimeError(f"nothing to scroll to for {selector!r}")

    def scroll_to_text(self, text: str) -> None:
        """Frame a section by its visible heading.

        Headings are looked for first (the composer uses real ones); a card title
        is a ``div``, so an exact-text match on any element is the fallback. One
        element whose whole text is the label, either way.
        """
        found = self.evaluate(
            """
            (text => {
              const exact = (selector) =>
                [...document.querySelectorAll(selector)].find(
                  (candidate) => candidate.textContent.trim() === text
                );
              const node = exact('h2, h3, p, span') || exact('div, button, a, dt, label');
              if (!node) return false;
              node.scrollIntoView({ block: 'center' });
              return true;
            })(%r)
            """
            % text
        )
        if not found:
            raise RuntimeError(f"nothing to scroll to for text {text!r}")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        shutil.rmtree(self.user_data_dir, ignore_errors=True)


# ------------------------------------------------------------------ scenarios


COMPOSER = "/strategies/new"
#: Names the widest elements when the page is wider than the viewport. A shell
#: that clips reports the shell's own nodes; a page that overflows reports its
#: content, and the difference decides whether the mobile claim can be made.
OVERFLOW_PROBE = """
(() => {
  const width = window.innerWidth;
  // The minimum width an element can be laid out at, measured on a detached
  // clone so nothing on the page moves. This separates "the page's own content
  // does not fit" from "the shell around it does not fit".
  const minContent = (el) => {
    if (!el) return null;
    const holder = document.createElement('div');
    holder.style.cssText = 'position:fixed;left:-10000px;top:0;width:min-content';
    holder.appendChild(el.cloneNode(true));
    document.body.appendChild(holder);
    const value = Math.round(holder.getBoundingClientRect().width);
    holder.remove();
    return value;
  };
  const over = [...document.querySelectorAll('body *')]
    .map((el) => [el, el.getBoundingClientRect().width])
    .filter(([, w]) => w > width + 1)
    .sort((a, b) => b[1] - a[1]);
  const main = document.querySelector('main');
  const root = main ? main.firstElementChild : null;
  // Which block inside this page sets its minimum width: each titled card, by
  // its own title, so the answer is a name rather than a pixel count.
  const cards = root
    ? [...root.children].map((el) => ({
        title: String(
          (el.querySelector('[data-slot="card-title"]') || el.querySelector('h2, h3') || el)
            .textContent || '',
        )
          .trim()
          .slice(0, 40),
        min_width: minContent(el),
      }))
    : [];
  return {
    viewport: width,
    scroll_width: document.documentElement.scrollWidth,
    // What THIS feature contributes, and what the fixed shell contributes.
    content_min_width: main ? minContent(main.firstElementChild) : null,
    cards: cards,
    shell_min_width: {
      header: minContent(document.querySelector('header')),
      dock: minContent(document.querySelector('footer')),
    },
    widest: over.slice(0, 4).map(([el, w]) => ({
      tag: el.tagName.toLowerCase(),
      cls: String(el.className || '').slice(0, 70),
      width: Math.round(w),
      inside_main: Boolean(el.closest('main')),
    })),
  };
})()
"""
STARTER_TS = (REPO / "frontend-next/features/strategies/lib/starter.ts").read_text()
STARTER_SOURCE = STARTER_TS.split("export const HOSTED_STARTER_SOURCE = `", 1)[1].rsplit("`;", 1)[0]


def bypass_login(chrome: Chrome, frontend: str) -> None:
    """Real login route, same-origin cookie: exactly what the login page does."""
    chrome.navigate(f"{frontend}/login")
    status = chrome.evaluate(
        """
        (async () => {
          const response = await fetch('/api/auth/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'include',
            body: JSON.stringify(%s),
          });
          return response.status;
        })()
        """
        % json.dumps({"username": OPERATOR_USER, "password": OPERATOR_PASSWORD}),
        await_promise=True,
    )
    if status != 200:
        raise RuntimeError(f"login failed with status {status}")
    log("operator session established through /api/auth/login")


def run_scenarios(chrome: Chrome, frontend: str, session_factory) -> dict:
    result: dict = {}

    # 1. Composer: source first, readiness checked in the background.
    chrome.navigate(f"{frontend}{COMPOSER}")
    chrome.wait_for("!!document.querySelector('#composer-source')")
    chrome.set_value("#composer-name", "QA RELIANCE starter")
    chrome.set_value("#composer-source", STARTER_SOURCE)
    chrome.wait_for("!!document.querySelector('[data-testid=\"readiness-ready\"]')", timeout=30)
    result["readiness_ready"] = True
    chrome.screenshot("01-composer-ready-desktop.png")

    # 2. Keyboard path: the editor is focusable and accepts real key input.
    chrome.evaluate("document.querySelector('#composer-source').focus()")
    chrome.key("End", "End", 35)
    chrome.insert_text("\n# typed with the keyboard\n")
    chrome.wait_for(
        "document.querySelector('#composer-source').value.includes('typed with the keyboard')"
    )
    result["keyboard_source_focus"] = chrome.evaluate("document.activeElement.id") == "composer-source"
    chrome.screenshot("02-composer-keyboard-focus.png")

    # 3. A source with no entrypoint is blocked before anything is stored.
    chrome.set_value("#composer-source", "print('this file has no main')\n")
    chrome.wait_for("!!document.querySelector('[data-testid=\"readiness-blocked\"]')", timeout=30)
    result["blocked_disables_action"] = chrome.evaluate(
        "[...document.querySelectorAll('button')]"
        ".filter((node) => node.textContent.includes('Create and run'))"
        ".every((node) => node.disabled)"
    )
    chrome.scroll_to('[data-testid="readiness-blocked"]')
    chrome.screenshot("03-composer-blocked-source.png")

    # 4. A dropped non-Python file is refused and the typed source is preserved.
    chrome.evaluate(
        """
        (() => {
          const area = document.querySelector('#composer-source');
          const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
          setter.call(area, "def main(ctx):\\n    return 0\\n");
          area.dispatchEvent(new Event('input', { bubbles: true }));
          const transfer = new DataTransfer();
          transfer.items.add(new File(['not python'], 'notes.txt', { type: 'text/plain' }));
          area.dispatchEvent(new DragEvent('drop', {
            bubbles: true, cancelable: true, dataTransfer: transfer,
          }));
          return true;
        })()
        """
    )
    chrome.wait_for("document.body.innerText.includes('Choose a Python file')", timeout=10)
    result["drop_error_preserves_source"] = chrome.evaluate(
        "document.querySelector('#composer-source').value.includes('def main(ctx)')"
    )
    # The refusal belongs to the DROPPED FILE, not to the strategy: it leaves the
    # typed source exactly as it was. (Whether it refuses the launch is asserted
    # at step 7, once the readiness answer for this source has arrived.)
    result["refused_drop_notice_shown"] = chrome.evaluate(
        "!!document.querySelector('[data-testid=\"source-notice\"]')"
    )
    chrome.scroll_to("#composer-source")
    chrome.screenshot("04-composer-file-type-error.png")

    # 5. Ordinary schema fields: one text parameter, added through the UI, with
    #    the value the first run will actually send.
    chrome.click_text("Add a parameter")
    chrome.wait_for("!!document.querySelector('#param-name')")
    chrome.set_value("#param-name", "symbol")
    chrome.set_value("#param-description", "Index ticker to read")
    chrome.evaluate(
        """
        (() => {
          const type = document.querySelector('#param-type');
          type.click();
          return true;
        })()
        """
    )
    chrome.wait_for("[...document.querySelectorAll('[role=option]')].length > 0")
    chrome.evaluate(
        """
        (() => {
          const option = [...document.querySelectorAll('[role=option]')]
            .find((node) => node.textContent.trim() === 'Text');
          option.click();
          return true;
        })()
        """
    )
    chrome.evaluate(
        """
        (() => {
          const add = [...document.querySelectorAll('button')]
            .filter((node) => node.textContent.trim() === 'Add').pop();
          add.click();
          return true;
        })()
        """
    )
    chrome.wait_for("!!document.querySelector('#composer-value-symbol')")
    chrome.set_value("#composer-value-symbol", "NSE:NIFTY 50")
    result["schema_parameter_added"] = True
    chrome.scroll_to_text("Inputs")
    chrome.screenshot("05-composer-schema-parameter.png")

    # 6. A data-only strategy has no trade decision at all.
    chrome.scroll_to_text("Who approves trades")
    result["data_only_has_no_decision"] = chrome.evaluate(
        "!!document.querySelector('[data-testid=\"authorization-inapplicable\"]')"
    )
    chrome.screenshot("06-composer-data-only-permissions.png")

    # 6b. If it CAN trade, both choices appear and the limits are the owner's own
    #     (empty), for review-first as well as automatic.
    chrome.evaluate(
        """
        (() => {
          const trade = [...document.querySelectorAll('[aria-label="Propose trades"]')][0];
          trade.click();
          return true;
        })()
        """
    )
    chrome.wait_for("!!document.querySelector('#limit-allocation_inr')")
    result["limits_start_empty"] = chrome.evaluate(
        "document.querySelector('#limit-allocation_inr').value === ''"
    )
    # Review-first also gets the owner's own limits: they are what a trade is
    # admitted against long before anyone approves it, and none of them is
    # invented by the page.
    result["review_first_limits_visible"] = chrome.evaluate(
        "document.body.innerText.includes('Limits a trade is admitted against')"
    )
    chrome.click_text("Trade automatically within my limits")
    chrome.scroll_to_text("Limits the authorization is bound to")
    chrome.screenshot("07-composer-autonomous-limits.png")
    # Back to a data-only strategy for the recorded creation: the starter reads
    # market data and never trades.
    chrome.evaluate(
        """
        (() => {
          const trade = [...document.querySelectorAll('[aria-label="Propose trades"]')][0];
          trade.click();
          return true;
        })()
        """
    )
    chrome.wait_for("!!document.querySelector('[data-testid=\"authorization-inapplicable\"]')")

    # 7. Create and run the shipped (data-only) starter with its one parameter.
    chrome.wait_for("!!document.querySelector('[data-testid=\"readiness-ready\"]')", timeout=30)
    # The refused drop from step 4 is still on screen, and it must not refuse the
    # launch: the readiness answer for the current source is what decides.
    result["refused_drop_does_not_block_launch"] = chrome.evaluate(
        "!!document.querySelector('[data-testid=\"source-notice\"]') && "
        "[...document.querySelectorAll('button')]"
        ".some((node) => node.textContent.includes('Create and run') && !node.disabled)"
    )
    chrome.click_text("Create and run")
    chrome.wait_for(
        "location.pathname.startsWith('/strategies/') && location.pathname !== '/strategies/new'",
        timeout=90,
    )
    chrome.wait_for("document.body.innerText.includes('Review or automate')", timeout=30)
    strategy_id = chrome.evaluate("location.pathname.split('/')[2]")
    result["created_strategy_id"] = strategy_id
    chrome.screenshot("08-strategy-detail-created.png")

    # 7b. Run now on the detail page uses the pinned version's schema as real
    #     inputs (not a raw JSON box), prefilled from the same parameter names.
    chrome.scroll_to_text("Run now")
    result["run_now_params_are_fields"] = chrome.evaluate(
        "!!document.querySelector('#run-symbol') && !document.querySelector('#run-params')"
    )
    chrome.screenshot("09-run-now-schema-parameters.png")

    # 8. Authorization: the owner's own limits, then an explicit grant.
    #    The recorded strategy is data-only, and the panel says so rather than
    #    presenting the two lanes as a decision over trades that cannot happen.
    result["authorization_notes_no_trade_capability"] = chrome.evaluate(
        "!!document.querySelector('[data-testid=\"authorization-no-trade-capability\"]')"
    )
    chrome.set_value("#auth-limit-allocation_inr", "250000")
    chrome.wait_for(
        "document.querySelector('[data-testid=\"grant-summary\"]')"
        ".textContent.includes('250000')"
    )
    chrome.scroll_to('[data-testid="grant-summary"]')
    chrome.screenshot("10-authorization-summary.png")
    chrome.click_text("Authorize automatic trading")
    chrome.wait_for("document.body.innerText.includes('Authorization active')", timeout=30)
    result["grant_issued"] = True
    chrome.scroll_to('[data-testid="grant-history"]')
    chrome.screenshot("11-authorization-granted.png")

    # 9. Revoke, with the "not a cancel" wording beside the action.
    chrome.click_text("Revoke")
    chrome.wait_for("document.body.innerText.includes('Confirm revoke')")
    chrome.click_text("Confirm revoke")
    chrome.wait_for("!document.body.innerText.includes('Authorization active')", timeout=30)
    result["grant_revoked"] = True
    chrome.scroll_to('[data-testid="grant-history"]')
    chrome.screenshot("12-authorization-revoked.png")

    # 10. A real pending request: waiting for the owner, not a running process.
    seed_waiting_request(session_factory, strategy_id=strategy_id, account_id="kite:paper")
    chrome.navigate(f"{frontend}/strategies/{strategy_id}")
    chrome.wait_for("document.body.innerText.includes('Waiting for your decision')", timeout=30)
    chrome.evaluate(
        "document.querySelector('[data-testid=\"request-state-explainer\"]')"
        ".scrollIntoView({ block: 'center' })"
    )
    result["waiting_request_visible"] = True
    chrome.screenshot("13-execution-request-waiting.png")

    # 11. Plan review behind that request.
    chrome.evaluate(
        """
        (() => {
          const plan = [...document.querySelectorAll('button')]
            .find((node) => node.textContent.trim() === 'Plan');
          plan.scrollIntoView({ block: 'center' });
          plan.click();
          return true;
        })()
        """
    )
    chrome.wait_for("document.body.innerText.includes('RELIANCE')", timeout=30)
    result["plan_review_visible"] = True
    chrome.screenshot("14-plan-review.png")

    # 12. Schedule: create one, then read the runtime's own next/missed answer.
    chrome.set_value("#schedule-time", "15:45")
    # The scheduled run carries its own parameter values (no platform stamping).
    chrome.set_value("#schedule-param-symbol", "NSE:BANKNIFTY")
    chrome.wait_for("document.querySelector('#schedule-time').value === '15:45'")
    chrome.screenshot("15-schedule-form.png")
    chrome.click_text("Create schedule")
    chrome.wait_for(
        "!!document.querySelector('[data-testid=\"schedule-next\"]')"
        " && document.body.innerText.includes('Every day at 15:45')",
        timeout=30,
    )
    chrome.evaluate(
        "document.querySelector('[data-testid=\"schedule-policy\"]')"
        ".scrollIntoView({ block: 'center' })"
    )
    result["schedule_next"] = chrome.evaluate(
        "document.querySelector('[data-testid=\"schedule-next\"]').textContent"
    )
    chrome.screenshot("16-schedule-saved.png")

    # 13. Disable it: a disabled schedule starts nothing.
    chrome.click_within('[data-testid="schedule-state"]', "Disable")
    chrome.wait_for(
        "document.querySelector('[data-testid=\"schedule-state\"]').textContent.includes('Disabled')",
        timeout=30,
    )
    result["schedule_disabled"] = True
    chrome.screenshot("17-schedule-disabled.png")

    # 14. Narrow width on both surfaces, in a real 390px layout viewport.
    chrome.set_viewport(390, 844, mobile=False)
    chrome.navigate(f"{frontend}{COMPOSER}")
    chrome.wait_for("!!document.querySelector('#composer-source')")
    result["composer_mobile_overflow"] = chrome.evaluate(OVERFLOW_PROBE)
    chrome.screenshot("18-composer-mobile.png")
    chrome.navigate(f"{frontend}/strategies/{strategy_id}")
    chrome.wait_for("document.body.innerText.includes('Review or automate')")
    result["detail_mobile_overflow"] = chrome.evaluate(OVERFLOW_PROBE)
    chrome.screenshot("19-strategy-detail-mobile.png")
    # The wide tables are the one part of this page that cannot fit: they must
    # scroll inside their own card rather than set the width of the page.
    chrome.scroll_to_text("Versions")
    result["versions_table_inside_card"] = chrome.evaluate(
        """
        (() => {
          const container = document.querySelector('[data-slot="table-container"]');
          if (!container) return null;
          const card = container.closest('[data-slot="card"]') || container.parentElement;
          return {
            table_scrollable: container.scrollWidth > container.clientWidth,
            container_width: Math.round(container.getBoundingClientRect().width),
            card_width: Math.round(card.getBoundingClientRect().width),
            viewport: window.innerWidth,
          };
        })()
        """
    )
    chrome.screenshot("20-strategy-detail-narrow-tables.png")
    chrome.set_viewport(1440, 950)

    result["screenshots"] = [entry["file"] for entry in SHOTS]
    return result


# ------------------------------------------------------------------ lifecycle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the disposable database")
    parser.add_argument("--frontend-port", type=int, default=3300)
    args = parser.parse_args()

    api_port = 8181
    # The browser talks to the dev server, which proxies /api; that origin is
    # what the deployment-style CORS/same-origin allowlist must name.
    os.environ["APP_ALLOWED_ORIGINS"] = (
        f"http://127.0.0.1:{args.frontend_port},http://localhost:{args.frontend_port}"
    )
    database_name, dsn = create_database()
    log(f"disposable database {database_name} on 15433")

    # The exchange-calendar reader (like several other runtime readers) opens its
    # own connection from these DB_* variables rather than DATABASE_URL, so point
    # them at the same disposable database.
    parsed_dsn = urllib.parse.urlsplit(dsn)
    os.environ.update(
        {
            "DB_HOST": parsed_dsn.hostname or "127.0.0.1",
            "DB_PORT": str(parsed_dsn.port or 5432),
            "DB_NAME": parsed_dsn.path.lstrip("/"),
            "DB_USER": urllib.parse.unquote(parsed_dsn.username or "postgres"),
            "DB_PASSWORD": urllib.parse.unquote(parsed_dsn.password or ""),
        }
    )

    os.environ.update(
        {
            "DATABASE_URL": dsn,
            "APP_ENV": "development",
            "APP_ALLOW_INSECURE_DEV_AUTH": "true",
            "APP_JWT_SECRET": "ui-qa-secret-not-for-any-other-environment",
            "APP_ADMIN_USERNAME": OPERATOR_USER,
            "APP_ADMIN_PASSWORD": OPERATOR_PASSWORD,
            # The workspace `.env` carries a production-shaped admin password
            # hash. Emptying the hash readers here pins the QA credential
            # instead: `load_dotenv` never overwrites an existing variable.
            "APP_ADMIN_PASSWORD_HASH": "",
            "APP_ADMIN_PASSWORD_HASH_B64": "",
            "APP_ADMIN_PASSWORD_HASH_FILE": "",
            "APP_COOKIE_SECURE": "false",
            # The dev server proxies /api for the browser, so the operator
            # origin is what the deployment-style allowlist must name.
            "HOSTED_STRATEGY_ACCOUNT_SCOPES": "kite:paper",
            "HOSTED_LIVE_ENABLED": "false",
            "HOSTED_EXECUTION_DISPATCH_ENABLED": "false",
            "STRATEGY_SCHEDULE_INTERVAL_SECONDS": "3600",
        }
    )
    sys.path.insert(0, str(REPO))

    next_process = None
    chrome = None
    result: dict = {"database": database_name, "shots": []}
    try:
        migrate(dsn)

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(dsn, future=True)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        seed_catalog(session_factory)

        import threading

        import uvicorn

        app = build_app(session_factory)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
        )
        threading.Thread(target=server.run, daemon=True).start()
        if not wait_for_http(f"http://127.0.0.1:{api_port}/api/strategies"):
            raise RuntimeError("loopback API did not come up")
        log(f"loopback API on 127.0.0.1:{api_port}")

        frontend = f"http://127.0.0.1:{args.frontend_port}"
        next_process = subprocess.Popen(
            ["npx", "next", "dev", "--port", str(args.frontend_port)],
            cwd=REPO / "frontend-next",
            env={
                **os.environ,
                "BACKEND_INTERNAL_URL": f"http://127.0.0.1:{api_port}",
                "NEXT_PUBLIC_API_BASE_URL": "",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not wait_for_http(f"{frontend}/strategies/new", timeout=300):
            raise RuntimeError("frontend dev server did not come up")
        log(f"frontend dev server on {frontend}")

        chrome = Chrome()
        bypass_login(chrome, frontend)
        result.update(run_scenarios(chrome, frontend, session_factory))
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        import traceback

        result["error"] = repr(exc)
        result["traceback"] = traceback.format_exc()
        if chrome is not None:
            try:
                chrome.screenshot("00-failure.png")
                result["failure_body_text"] = chrome.evaluate(
                    "document.body.innerText.slice(0, 1500)"
                )
                result["failure_url"] = chrome.evaluate("location.href")
                result["failure_source_value"] = chrome.evaluate(
                    "(document.querySelector('#composer-source') || {}).value?.slice(0, 200) || null"
                )
            except Exception as shot_exc:  # noqa: BLE001
                result["failure_capture_error"] = repr(shot_exc)
        log(f"FAILED: {exc!r}")
    finally:
        if chrome is not None:
            chrome.close()
        if next_process is not None:
            next_process.send_signal(signal.SIGTERM)
            try:
                next_process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                next_process.kill()
        if not args.keep:
            drop_database(database_name)
            log(f"dropped {database_name}")
        result["shots"] = SHOTS
        (HERE / "harness-result.json").write_text(json.dumps(result, indent=2, default=str))
        log("wrote harness-result.json")
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
