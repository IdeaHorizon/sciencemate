"""Durable acquisition state for each user's explicitly selected domain."""
from __future__ import annotations
import sqlite3
from datetime import UTC, datetime
from app.config import the_data_root


def _connect():
    root = the_data_root()
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'feed-domain-progress.sqlite3', timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS progress (user_id TEXT, domain TEXT, state TEXT, updated TEXT, PRIMARY KEY(user_id, domain))')
    return db


def mark(user_id, domains, state):
    with _connect() as db:
        db.executemany('INSERT OR REPLACE INTO progress VALUES (?, ?, ?, ?)',
                       [(str(user_id), str(code), state, datetime.now(UTC).isoformat()) for code in domains])


def read(user_id):
    with _connect() as db:
        return {code: {'state': state, 'updated_at': updated}
                for code, state, updated in db.execute('SELECT domain, state, updated FROM progress WHERE user_id=?', (str(user_id),))}


def phase(*, active, acquisition, collected, pending, recommendable, translation_pending):
    if active:
        return 'searching'
    if acquisition == 'searching':
        return 'interrupted'  # A restart must not leave a permanent running label.
    if acquisition == 'failed':
        return 'failed'
    if pending:
        return 'preparing'
    if translation_pending:
        return 'translating'
    if recommendable:
        return 'ready'
    if acquisition == 'completed':
        return 'empty'
    return 'not_started' if not collected else 'awaiting_recommendation'
