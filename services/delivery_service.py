import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

import ui
from config import Config
from database import Database
from logging_setup import log_event
from models.video import Video
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("delivery")

_INVALID_MARKERS = ("file identifier", "file_id", "file reference", "wrong remote file")
_GONE_MARKERS = ("message to copy not found", "message_id_invalid", "message can't be copied")


class _Pacer:
    """Staggers the START of every send. Several uploads are in flight at once (fast), yet the
    sends still begin in order, a fixed interval apart, so the chat order is kept and Telegram's
    per-chat flood limit is respected. If Telegram answers with a flood wait, the interval
    automatically grows for the rest of that delivery."""

    def __init__(self, svc: "DeliveryService", chat_id: int) -> None:
        self.svc, self.chat_id = svc, chat_id
        self.interval = max(0.0, float(getattr(svc.cfg, "delivery_interval", 0.2) or 0.0))
        self._next = 0.0
        self._seen = svc._flood_hits.get(chat_id, 0)
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            hits = self.svc._flood_hits.get(self.chat_id, 0)
            if hits != self._seen:          # Telegram asked us to slow down
                self._seen = hits
                self.interval = min(1.2, max(self.interval, 0.2) * 1.6)
            loop = asyncio.get_running_loop()
            now = loop.time()
            start = max(now, self._next)
            if start > now:
                await asyncio.sleep(start - now)
            self._next = start + self.interval


@dataclass
class DeliveryResult:
    delivered: int = 0
    failed: int = 0
    blocked: bool = False


class DeliveryService:
    def __init__(self, bot: Bot, db: Database, videos: VideoService,
                 users: UserService, cfg: Config, settings=None, events=None) -> None:
        self.settings = settings   # SettingsService (optional): caption options
        self.events = events       # EventService (optional): admin Logs panel
        self.bot = bot
        self.db = db
        self.videos = videos
        self.users = users
        self.cfg = cfg
        self._access_warned = False
        self._flood_hits: dict[int, int] = {}      # chat_id -> flood waits seen (see _Pacer)
        self._bg: set[asyncio.Task] = set()        # strong refs to background tasks

    def _concurrency(self) -> int:
        """Videos in flight at once for a private chat (1 = the old one-by-one delivery)."""
        return max(1, int(getattr(self.cfg, "delivery_concurrency", 1) or 1))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def deliver(self, user_id: int, chat_id: int, category: str, count: int,
                      shuffle: bool = False, in_group: bool = False,
                      thread_id: int | None = None) -> DeliveryResult:
        """Send up to `count` videos this user has never received, in the order of their mode
        (`category` is the mode: new = newest -> oldest, old = oldest -> newest).
        Every sent video is written to user_video_history and queued for auto-deletion, and the
        user's own session row (mode / position / progress) is advanced. Nothing is shared
        between users.

        `in_group=True`: the videos go to a group chat. They are ALWAYS content-protected
        there (everybody in the group could otherwise save them) and a refused send is never
        counted as "this user blocked the bot". `thread_id` keeps forum-topic replies in
        their topic."""
        result = DeliveryResult()
        tried_ids: set[int] = set()
        sent_unique: set[str] = set()
        # Telegram lets a bot post only ~20 messages per minute into ONE group, so videos going
        # into a group are spaced out (a flood wait would otherwise stall the whole delivery).
        gap = float(getattr(self.cfg, "group_send_gap", 0.0) or 0.0) if in_group else 0.0
        sent_any = False

        try:   # the user's own session: remember the chosen mode
            await self.users.start_session(user_id, category)
        except Exception:
            log.exception("session_start_failed")

        if not shuffle:   # statistics only; runs in the background so it never delays a delivery
            self._spawn(self._count_prevented(user_id, category, count))

        if not in_group and self._concurrency() > 1:
            try:
                return await self._deliver_fast(user_id, chat_id, category, count, shuffle,
                                                thread_id, result)
            finally:
                self._flood_hits.pop(chat_id, None)

        for _ in range(5):  # extra rounds replace videos that failed
            need = count - result.delivered
            if need <= 0:
                break
            batch = await self.videos.fetch_session_batch(user_id, category, need,
                                                          list(tried_ids), shuffle)
            if not batch:
                break
            for video in batch:
                tried_ids.add(video.id)
                if (not self.videos.allow_duplicates
                        and video.telegram_file_unique_id in sent_unique):
                    continue
                protect = True if in_group else self.protect_for(user_id)
                if gap and sent_any:
                    await asyncio.sleep(gap)
                sent_any = True
                markup = None
                if not in_group and self._favorites_on():
                    markup = ui.fav_save_kb(video.id)    # ⭐ Save to Favorites under the video
                outcome, message_id = await self._send_one(chat_id, video, result.delivered + 1,
                                                           protect, thread_id, markup)
                if outcome is None:  # user blocked the bot (private) / bot cannot post (group)
                    result.blocked = True
                    if not in_group:
                        await self.users.mark_blocked(user_id)
                    await self._record(user_id, category, count, result)
                    return result
                if outcome:
                    sent_unique.add(video.telegram_file_unique_id)
                    result.delivered += 1
                    await self._record_delivery(user_id, chat_id, video, message_id, category)
                    await self._advance_session(user_id, category, video)
                    if result.delivered % max(1, self.cfg.delivery_batch_size) == 0:
                        await asyncio.sleep(1.0)   # short breather between batches (flood-friendly)
                else:
                    result.failed += 1

        await self._record(user_id, category, count, result)
        return result

    async def _count_prevented(self, user_id: int, category: str, count: int) -> None:
        try:
            prevented = await self.videos.count_seen_ahead_all(user_id, category, count)
            await self.videos.bump_counter("delivery_duplicates_prevented", prevented)
        except Exception:
            log.exception("prevented_counter_failed")

    async def _advance_many(self, user_id: int, mode: str, videos: list[Video],
                            after: asyncio.Task | None = None) -> None:
        """Progress bookkeeping for a batch, in order, in the background."""
        if after is not None:
            await asyncio.gather(after, return_exceptions=True)
        for video in videos:
            await self._advance_session(user_id, mode, video)

    async def _deliver_fast(self, user_id: int, chat_id: int, category: str, count: int,
                            shuffle: bool, thread_id: int | None,
                            result: DeliveryResult) -> DeliveryResult:
        """Pipelined delivery for private chats: several videos in flight at once, the history
        row of each video written the moment it is sent (so nothing repeats), progress
        bookkeeping done in the background. Same rules as `deliver`: unique videos only,
        mode order, failed videos replaced in the next round."""
        tried_ids: set[int] = set()
        sent_unique: set[str] = set()
        pacer = _Pacer(self, chat_id)
        sem = asyncio.Semaphore(self._concurrency())
        advance: asyncio.Task | None = None

        for _ in range(5):  # extra rounds replace videos that failed
            need = count - result.delivered
            if need <= 0:
                break
            batch = await self.videos.fetch_session_batch(user_id, category, need,
                                                          list(tried_ids), shuffle)
            if not batch:
                break
            todo: list[Video] = []
            queued: set[str] = set()
            for video in batch:
                tried_ids.add(video.id)
                uid = video.telegram_file_unique_id
                if not self.videos.allow_duplicates and (uid in sent_unique or uid in queued):
                    continue
                queued.add(uid)
                todo.append(video)
            if not todo:
                continue

            protect = self.protect_for(user_id)
            favorites = self._favorites_on()
            base = result.delivered
            stop = asyncio.Event()      # set when the user blocked the bot: stop sending

            async def one(i: int, video: Video) -> str:
                async with sem:
                    if stop.is_set():
                        return "skip"
                    await pacer.wait()
                    if stop.is_set():
                        return "skip"
                    markup = ui.fav_save_kb(video.id) if favorites else None
                    outcome, message_id = await self._send_one(
                        chat_id, video, base + i + 1, protect, thread_id, markup, pace=False)
                if outcome is None:
                    stop.set()
                    return "blocked"
                if not outcome:
                    return "fail"
                await self._record_delivery(user_id, chat_id, video, message_id, category)
                return "ok"

            outcomes = await asyncio.gather(*(one(i, v) for i, v in enumerate(todo)),
                                            return_exceptions=True)
            sent: list[Video] = []
            blocked = False
            for video, state in zip(todo, outcomes):
                if isinstance(state, BaseException):
                    log.error("fast_send_crashed", exc_info=state)
                    state = "fail"
                if state == "ok":
                    result.delivered += 1
                    sent_unique.add(video.telegram_file_unique_id)
                    sent.append(video)
                elif state == "fail":
                    result.failed += 1
                elif state == "blocked":
                    blocked = True
            if sent:
                advance = self._spawn(self._advance_many(user_id, category, sent, advance))
            if blocked:
                result.blocked = True
                await self.users.mark_blocked(user_id)
                if advance is not None:
                    await asyncio.gather(advance, return_exceptions=True)
                await self._record(user_id, category, count, result)
                return result

        if advance is not None:
            await asyncio.gather(advance, return_exceptions=True)
        await self._record(user_id, category, count, result)
        return result

    async def _advance_session(self, user_id: int, mode: str, video: Video) -> None:
        """Position/progress bookkeeping and lock heartbeat. Best effort: the delivery history
        (written separately, in its own transaction) is what guarantees no repeats."""
        try:
            await self.users.advance_session(
                user_id, mode, video.id, video.message_date or video.created_at)
            await self.users.refresh_delivery_lock(user_id)
        except Exception:
            log.exception("session_advance_failed")

    def _favorites_on(self) -> bool:
        """⭐ button under delivered videos (admin: Public Buttons -> Favorites)."""
        check = getattr(self.settings, "button_on", None)
        return True if check is None else bool(check("btn_favorites"))

    def protect_for(self, user_id: int) -> bool:
        """Telegram content protection for the videos sent to ONE user: no forwarding, no saving
        or downloading from the chat. Every user is protected until an admin allows Save/Forward
        for that user (User Manager). The global switch in Bot Settings stays a master switch:
        if an admin turns it OFF, nobody is protected."""
        if self.settings is not None and not self.settings.flag("protect_content"):
            return False
        return self.users.protect_for(user_id)

    async def _record_delivery(self, user_id: int, chat_id: int, video: Video,
                               message_id: int | None, mode: str) -> None:
        """History rows (global + this mode's) + auto-delete job, in one transaction, right
        after each send: if the process dies mid-batch, everything already sent is recorded and
        the rest is simply still unseen when the user continues."""
        for attempt in range(3):
            try:
                async with self.db.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO user_video_history (user_id, video_id, category) "
                            "VALUES ($1, $2, $3) ON CONFLICT (user_id, video_id) DO NOTHING",
                            user_id, video.id, video.category,
                        )
                        await conn.execute(
                            "INSERT INTO user_mode_history (user_id, mode, video_id) "
                            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                            user_id, mode, video.id,
                        )
                        if message_id is not None:
                            await conn.execute(
                                "INSERT INTO delivered_messages "
                                "(user_id, chat_id, video_id, telegram_message_id, delete_at) "
                                "VALUES ($1, $2, $3, $4, "
                                "now() + make_interval(secs => $5::double precision))",
                                user_id, chat_id, video.id, message_id,
                                float(self.cfg.auto_delete_seconds),
                            )
                return
            except Exception:
                log.exception("record_delivery_failed")
                await asyncio.sleep(0.5 * (attempt + 1))
        log_event(log, logging.ERROR, "record_delivery_gave_up", user_id=user_id,
                  video_id=video.id)

    async def _record(self, user_id: int, category: str, count: int, r: DeliveryResult) -> None:
        if r.delivered:
            try:
                await self.users.finish_batch(user_id, category)
            except Exception:
                log.exception("session_finish_failed")
        await self.db.pool.execute(
            "INSERT INTO requests (user_id, category, requested, delivered, failed) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id, category, count, r.delivered, r.failed,
        )
        log_event(log, logging.INFO, "delivery_finished", user_id=user_id, category=category,
                  requested=count, delivered=r.delivered, failed=r.failed, blocked=r.blocked)
        if self.events is not None:
            await self.events.record(
                "delivery", f"{r.delivered}/{count} {category} videos delivered"
                + (f", {r.failed} failed" if r.failed else "")
                + (", user blocked the bot" if r.blocked else ""), user_id)
            if r.failed:
                await self.events.record("error", f"{r.failed} video(s) could not be sent", user_id)

    async def _send_one(self, chat_id: int, video: Video, position: int,
                        protect: bool = True,
                        thread_id: int | None = None,
                        reply_markup=None, pace: bool = True) -> tuple[bool | None, int | None]:
        """(True, message_id) = sent, (False, None) = failed, (None, None) = user blocked the bot.

        Delivery modes, tried in order: the stored Bot API file_id, then copying the original
        post out of the source channel (no "forwarded from", so the channel is never exposed).
        Videos found by the history scanner have no file_id and always use the copy."""
        # Clean caption of our own. The channel caption is only added if the admin opted in
        # (Bot Settings): channel captions can carry channel names or links.
        keep = self.settings is not None and self.settings.flag("keep_source_caption")
        caption = ui.video_caption(self.cfg, video, position, video.caption if keep else None)
        modes = []
        if video.telegram_file_id:
            modes.append("file")
        if video.channel_id is not None and video.telegram_message_id is not None:
            modes.append("copy")
        invalid_reasons = []
        for mode in modes:
            outcome, reason, message_id = await self._attempt(mode, chat_id, video, caption, protect,
                                                              thread_id, reply_markup, pace)
            if outcome == "ok":
                return True, message_id
            if outcome == "blocked":
                return None, None
            if outcome == "error":      # transient / access problem: keep the video active
                return False, None
            invalid_reasons.append(reason)
        if len(invalid_reasons) == len(modes):   # every way of sending it is dead
            await self.videos.mark_invalid(video.id, "; ".join(invalid_reasons) or "no way to send")
            log_event(log, logging.ERROR, "video_deactivated", video_id=video.id)
            if self.events is not None:
                await self.events.record("error", f"Video ID {video.id} can no longer be sent "
                                                  "and was deactivated")
            await self._notify_admins(
                f"⚠️ Video ID {video.id} (#{video.video_number}, {video.category}) "
                "can no longer be sent and was deactivated. Re-scan the channel with /scan "
                "or send the video to the bot again to restore it."
            )
        return False, None

    async def _attempt(self, mode: str, chat_id: int, video: Video,
                       caption: str, protect: bool = True,
                       thread_id: int | None = None,
                       reply_markup=None, pace: bool = True) -> tuple[str, str, int | None]:
        """One delivery mode. Returns ("ok"|"blocked"|"error"|"invalid", reason, message_id)."""
        for attempt in range(5):
            try:
                if mode == "file":
                    # Original dimensions/duration are passed through untouched so Telegram's own
                    # viewer shows portrait / landscape / square exactly as posted; no
                    # thumbnail, re-encode, crop or padding is ever added.
                    sent = await self.bot.send_video(
                        chat_id, video.telegram_file_id, caption=caption, parse_mode="HTML",
                        width=video.width, height=video.height, duration=video.duration,
                        supports_streaming=True, protect_content=protect,
                        message_thread_id=thread_id, reply_markup=reply_markup,
                    )
                else:
                    # copy_message keeps the original media (size, ratio, streaming) as is.
                    sent = await self.bot.copy_message(
                        chat_id, video.channel_id, video.telegram_message_id,
                        caption=caption, parse_mode="HTML",
                        protect_content=protect, message_thread_id=thread_id,
                        reply_markup=reply_markup,
                    )
                if pace:   # the pipelined delivery paces itself (see _Pacer)
                    await asyncio.sleep(self.cfg.send_delay)
                return "ok", "", sent.message_id
            except TelegramRetryAfter as e:
                log_event(log, logging.WARNING, "flood_wait", seconds=e.retry_after)
                self._flood_hits[chat_id] = self._flood_hits.get(chat_id, 0) + 1
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError as e:
                if "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                    return "blocked", "", None
                if mode == "copy":  # the BOT has no access to the channel: not the user's fault
                    await self._warn_channel_access(str(e))
                    return "error", str(e), None
                return "blocked", "", None
            except TelegramBadRequest as e:
                msg = str(e).lower()
                if mode == "file" and any(m in msg for m in _INVALID_MARKERS):
                    log_event(log, logging.WARNING, "invalid_file_id", video_id=video.id)
                    return "invalid", str(e)[:100], None
                if mode == "copy" and any(m in msg for m in _GONE_MARKERS):
                    log_event(log, logging.WARNING, "source_message_gone", video_id=video.id)
                    return "invalid", str(e)[:100], None
                if mode == "copy" and "chat not found" in msg:
                    await self._warn_channel_access(str(e))
                    return "error", str(e), None
                log_event(log, logging.ERROR, "telegram_bad_request", video_id=video.id, error=str(e))
                return "error", str(e), None
            except TelegramAPIError as e:  # network / server hiccups
                log_event(log, logging.WARNING, "telegram_error", video_id=video.id,
                          attempt=attempt + 1, error=str(e))
                await asyncio.sleep(2 ** attempt)
            except Exception:
                log.exception("unexpected_send_error")
                return "error", "unexpected", None
        return "error", "retries exhausted", None

    async def _warn_channel_access(self, detail: str) -> None:
        if self._access_warned:
            return
        self._access_warned = True
        log_event(log, logging.ERROR, "channel_access_denied", error=detail[:200])
        await self._notify_admins(
            "⚠️ The bot cannot read the source channel, so scanned videos cannot be delivered. "
            "Add the bot to the channel as an administrator."
        )

    async def _notify_admins(self, text: str) -> None:
        for admin_id in self.cfg.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except TelegramAPIError:
                pass
