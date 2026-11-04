"""Favorite videos: a user saves a video he received; the name is optional.

Everything lives in PostgreSQL (`user_favorites`), per user. Nothing about the delivery
pipeline, history or categories is touched: a favorite is just (user, video, optional name).
Sending a favorite again goes through the same sender as normal deliveries (file_id, then
copy from the channel), is content-protected like any other video and is auto-deleted after
the usual time. It is NOT written to the delivery history (the user already received it).
"""
from __future__ import annotations

import logging

import asyncpg

from database import Database
from models.video import Video

log = logging.getLogger("favorites")

MAX_PER_USER = 200
NAME_MAX = 40


class FavoriteService:
    def __init__(self, db: Database, videos, delivery, users, cfg, settings=None) -> None:
        self.db, self.videos, self.delivery = db, videos, delivery
        self.users, self.cfg, self.settings = users, cfg, settings

    # ---------- reads ----------
    async def count(self, user_id: int) -> int:
        return await self.db.pool.fetchval(
            "SELECT COUNT(*) FROM user_favorites WHERE user_id = $1", user_id)

    async def has(self, user_id: int, video_id: int) -> bool:
        return bool(await self.db.pool.fetchval(
            "SELECT 1 FROM user_favorites WHERE user_id = $1 AND video_id = $2",
            user_id, video_id))

    _SELECT = ("SELECT f.video_id, f.name, f.created_at, v.video_number, v.category, "
               "v.is_active FROM user_favorites f JOIN videos v ON v.id = f.video_id ")

    async def get(self, user_id: int, video_id: int):
        return await self.db.pool.fetchrow(
            self._SELECT + "WHERE f.user_id = $1 AND f.video_id = $2", user_id, video_id)

    async def list(self, user_id: int, limit: int, offset: int = 0) -> list:
        return await self.db.pool.fetch(
            self._SELECT + "WHERE f.user_id = $1 ORDER BY f.created_at DESC, f.video_id DESC "
            "LIMIT $2 OFFSET $3", user_id, limit, offset)

    # ---------- writes ----------
    async def add(self, user_id: int, video_id: int) -> str:
        """'ok' | 'exists' | 'full' | 'missing'."""
        if await self.has(user_id, video_id):
            return "exists"
        if await self.count(user_id) >= MAX_PER_USER:
            return "full"
        try:
            row = await self.db.pool.fetchval(
                "INSERT INTO user_favorites (user_id, video_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING RETURNING 1", user_id, video_id)
        except asyncpg.ForeignKeyViolationError:
            return "missing"
        return "ok" if row else "exists"

    async def remove(self, user_id: int, video_id: int) -> bool:
        row = await self.db.pool.fetchval(
            "DELETE FROM user_favorites WHERE user_id = $1 AND video_id = $2 RETURNING 1",
            user_id, video_id)
        return row is not None

    async def set_name(self, user_id: int, video_id: int, name: str | None) -> bool:
        name = (name or "").strip()[:NAME_MAX] or None
        row = await self.db.pool.fetchval(
            "UPDATE user_favorites SET name = $3 WHERE user_id = $1 AND video_id = $2 "
            "RETURNING 1", user_id, video_id, name)
        return row is not None

    # ---------- send a saved video again ----------
    async def send(self, user_id: int, chat_id: int, video_id: int, markup=None) -> str:
        """'ok' | 'missing' | 'failed' | 'blocked'."""
        video: Video | None = await self.videos.get(video_id)
        if video is None or not video.is_active:
            return "missing"
        protect = self.delivery.protect_for(user_id)
        outcome, message_id = await self.delivery._send_one(chat_id, video, 1, protect, None, markup)
        if outcome is None:
            await self.users.mark_blocked(user_id)
            return "blocked"
        if not outcome:
            return "failed"
        if message_id is not None:      # same restart-safe auto-delete queue as normal videos
            try:
                await self.db.pool.execute(
                    "INSERT INTO delivered_messages "
                    "(user_id, chat_id, video_id, telegram_message_id, delete_at) "
                    "VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5::double precision))",
                    user_id, chat_id, video.id, message_id, float(self.cfg.auto_delete_seconds))
            except Exception:
                log.exception("favorite_autodelete_queue_failed")
        return "ok"
