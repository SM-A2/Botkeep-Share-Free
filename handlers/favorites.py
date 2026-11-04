"""⭐ Favorites: save a delivered video, give it an optional name, send it again later.

Callbacks (private chat only):
    fav:add:<video>            ⭐ Save to Favorites (button under a delivered video)
    fav:del:<video>            💔 Remove (same message)
    fav:nm:<video>[:<page>]    ✏️ name prompt (the name is optional)
    fav:nmx                    cancel the name prompt
    fav:list:<page>            My Favorites list
    fav:v:<video>:<page>       one favorite
    fav:send:<video>:<page>    ▶️ send it again (content-protected, auto-deleted as usual)
    fav:rm:<video>:<page>      🗑 remove from the list
The admin can hide the whole feature: Admin Panel -> Public Settings -> Public Buttons -> Favorites.
"""
import logging
import math

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import ui
from config import Config
from logging_setup import log_event
from services.favorite_service import MAX_PER_USER, NAME_MAX, FavoriteService
from services.settings_service import SettingsService
from services.user_service import UserService

log = logging.getLogger("favorites")
router = Router(name="favorites")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
HTML = "HTML"
STATE = "fav_name"
_SENDING: set[tuple[int, int]] = set()      # (user, video) being sent right now: ignores double taps


# ------------------------------------------------------------------ helpers
def _ids(data: str) -> tuple[int, int]:
    """'fav:x:<video>:<page>' -> (video, page); missing / bad parts -> 0 / 1."""
    parts = data.split(":")
    video = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    return video, max(1, page)


async def _show(cb: CallbackQuery, text: str, kb=None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode=HTML)
    except TelegramAPIError as e:
        if "not modified" in str(e).lower():
            return
        try:
            await cb.message.answer(text, reply_markup=kb, parse_mode=HTML)
        except TelegramAPIError:
            pass


async def _markup(cb: CallbackQuery, kb) -> None:
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except TelegramAPIError:
        pass      # message already deleted by the auto-delete, or unchanged


async def _enabled(cb: CallbackQuery, settings: SettingsService) -> bool:
    if settings.button_on("btn_favorites"):
        return True
    await cb.answer(ui.alert_text(ui.unavailable_text()), show_alert=True)
    return False


async def _page(cb: CallbackQuery, favs: FavoriteService, page: int) -> None:
    total = await favs.count(cb.from_user.id)
    if total == 0:
        await _show(cb, ui.fav_empty_text(), ui.fav_done_kb())
        return
    pages = max(1, math.ceil(total / ui.FAV_PAGE))
    page = min(max(1, page), pages)
    rows = await favs.list(cb.from_user.id, ui.FAV_PAGE, (page - 1) * ui.FAV_PAGE)
    await _show(cb, ui.fav_list_text(total, page, pages), ui.fav_list_kb(rows, page, pages))


# ------------------------------------------------------------------ save / remove under a video
@router.callback_query(F.data.startswith("fav:add:"))
async def on_add(cb: CallbackQuery, favs: FavoriteService, settings: SettingsService) -> None:
    if not await _enabled(cb, settings):
        return
    video_id, _ = _ids(cb.data)
    result = await favs.add(cb.from_user.id, video_id) if video_id else "missing"
    if result == "full":
        await cb.answer(f"Your favorites are full ({MAX_PER_USER}). Remove one first.",
                        show_alert=True)
        return
    if result == "missing":
        await cb.answer("This video is no longer available.", show_alert=True)
        return
    await cb.answer("⭐ Saved to your favorites" if result == "ok" else "⭐ Already in your favorites")
    await _markup(cb, ui.fav_saved_kb(video_id))
    if result == "ok":
        log_event(log, logging.INFO, "favorite_added", user_id=cb.from_user.id, video_id=video_id)


@router.callback_query(F.data.startswith("fav:del:"))
async def on_del(cb: CallbackQuery, favs: FavoriteService, settings: SettingsService) -> None:
    if not await _enabled(cb, settings):
        return
    video_id, _ = _ids(cb.data)
    await favs.remove(cb.from_user.id, video_id)
    await cb.answer("💔 Removed from favorites")
    await _markup(cb, ui.fav_save_kb(video_id))


# ------------------------------------------------------------------ list / one favorite
@router.callback_query(F.data.startswith("fav:list:"))
async def on_list(cb: CallbackQuery, favs: FavoriteService, settings: SettingsService) -> None:
    if not await _enabled(cb, settings):
        return
    await cb.answer()
    tail = cb.data.rsplit(":", 1)[-1]
    await _page(cb, favs, int(tail) if tail.isdigit() else 1)


@router.callback_query(F.data == "fav:noop")
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data.startswith("fav:v:"))
async def on_view(cb: CallbackQuery, favs: FavoriteService, settings: SettingsService) -> None:
    if not await _enabled(cb, settings):
        return
    video_id, page = _ids(cb.data)
    row = await favs.get(cb.from_user.id, video_id)
    if row is None:
        await cb.answer("This favorite no longer exists.", show_alert=True)
        await _page(cb, favs, page)
        return
    await cb.answer()
    await _show(cb, ui.fav_detail_text(row), ui.fav_detail_kb(video_id, page))


@router.callback_query(F.data.startswith("fav:rm:"))
async def on_remove(cb: CallbackQuery, favs: FavoriteService, settings: SettingsService) -> None:
    if not await _enabled(cb, settings):
        return
    video_id, page = _ids(cb.data)
    await favs.remove(cb.from_user.id, video_id)
    await cb.answer("🗑 Removed")
    await _page(cb, favs, page)


@router.callback_query(F.data.startswith("fav:send:"))
async def on_send(cb: CallbackQuery, favs: FavoriteService, users: UserService, cfg: Config,
                  settings: SettingsService) -> None:
    if not await _enabled(cb, settings):
        return
    video_id, _ = _ids(cb.data)
    user_id = cb.from_user.id
    if await favs.get(user_id, video_id) is None:
        await cb.answer("This favorite no longer exists.", show_alert=True)
        return
    key = (user_id, video_id)
    if key in _SENDING:
        await cb.answer()
        return
    if not await users.hit_rate_limit(user_id, cfg.rate_limit_requests, cfg.rate_limit_window):
        await cb.answer(ui.alert_text(ui.rate_text() + users.rate_hint(user_id)), show_alert=True)
        return
    _SENDING.add(key)
    try:
        await cb.answer("📤 Sending your video...")
        result = await favs.send(user_id, cb.message.chat.id, video_id, ui.fav_saved_kb(video_id))
    finally:
        _SENDING.discard(key)
    if result == "missing":
        await cb.answer("This video is not available right now.", show_alert=True)
    elif result == "failed":
        try:
            await cb.message.answer(ui.failed_text(), parse_mode=HTML)
        except TelegramAPIError:
            pass


# ------------------------------------------------------------------ optional name
@router.callback_query(F.data.startswith("fav:nm:"))
async def on_name(cb: CallbackQuery, favs: FavoriteService, users: UserService,
                  settings: SettingsService) -> None:
    if not await _enabled(cb, settings):
        return
    video_id, page = _ids(cb.data)
    if await favs.get(cb.from_user.id, video_id) is None:
        await cb.answer("Save the video to favorites first.", show_alert=True)
        return
    ok = await users.set_state(cb.from_user.id, STATE, {"video_id": video_id, "page": page})
    if not ok:
        await cb.answer(ui.alert_text(ui.busy_text()), show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.answer(ui.fav_name_prompt(), parse_mode=HTML,
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                    ui.Btn("❌ Cancel", callback_data="fav:nmx")]]))
    except TelegramAPIError:
        await users.clear_state(cb.from_user.id)


@router.callback_query(F.data == "fav:nmx")
async def on_name_cancel(cb: CallbackQuery, users: UserService) -> None:
    state, _ = await users.get_state(cb.from_user.id)
    if state == STATE:
        await users.clear_state(cb.from_user.id)
    await cb.answer("Cancelled")
    await _show(cb, "Cancelled.", ui.fav_done_kb())


async def awaiting_fav_name(message: Message, users: UserService) -> bool:
    state, _ = await users.get_state(message.from_user.id)
    return state == STATE


@router.message(F.text, ~F.text.startswith("/"), awaiting_fav_name)
async def on_name_input(message: Message, favs: FavoriteService, users: UserService) -> None:
    user_id = message.from_user.id
    state, data = await users.get_state(user_id)
    if state != STATE:
        return
    text = (message.text or "").strip()
    if len(text) > NAME_MAX:
        await message.answer(f"⚠️ Too long ({len(text)}/{NAME_MAX} characters). Send a shorter name.")
        return
    name = None if text == "-" else text
    if name is not None and not name:
        await message.answer("⚠️ Send a name, or <code>-</code> to remove it.", parse_mode=HTML)
        return
    await users.clear_state(user_id)
    saved = await favs.set_name(user_id, int(data.get("video_id", 0)), name)
    if not saved:
        await message.answer("This favorite no longer exists.", reply_markup=ui.fav_done_kb())
        return
    await message.answer(("✅ <b>Name saved:</b> " + ui.html_escape(name)) if name
                         else "✅ <b>Name removed.</b>",
                         parse_mode=HTML, reply_markup=ui.fav_done_kb())
