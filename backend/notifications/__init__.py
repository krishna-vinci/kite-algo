"""Notifications package: message building and provider adapters.

Phase 1 scope: `message.build_message` plus Telegram/ntfy adapters in
`backend.notifications.adapters`. This package deliberately imports only the
standard library and httpx (no redis/sqlalchemy) so it is safe to import from
any process.
"""
