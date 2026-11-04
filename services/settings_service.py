"""Persistent, admin-editable settings (table `bot_settings`) layered over the environment.

Effective value = saved override in the database, else the default taken from the environment /
built-in default. Everything an admin changes therefore survives Railway restarts. Reads are
served from an in-memory copy that is written through on every change (the bot runs as a single
instance), so the access gate does not hit the database for every message.

`apply_runtime()` copies the settings that other services read from `Config` (auto-delete time,
request limits, ordering, ...) onto the live config object.
"""
from __future__ import annotations

import html
import json
import logging
import re
from urllib.parse import urlparse

from config import Config
from database import Database
from logging_setup import log_event

log = logging.getLogger("settings")

ORDERS = ("asc", "desc", "random")

DEFAULT_MAINTENANCE_TITLE = "MAINTENANCE MODE"
DEFAULT_MAINTENANCE_EMOJI = "🛠️"
DEFAULT_MAINTENANCE_MESSAGE = (
    "VideoShare is temporarily under maintenance.\n\n"
    "Please try again later.\n\n"
    "Thank you for your patience. ❤️"
)
BOT_OFF_TEXT = "🔴 VideoShare is temporarily unavailable. Please try again later."
BANNED_TEXT = "⛔ You are currently unable to use VideoShare."
BANNED_REASON_MAX = 200

# Every other public message an admin can rewrite in Public Settings -> More Messages.
# key -> (title, icon, page slug, max length, shown as a pop-up alert?, placeholders help)
MESSAGES: dict[str, tuple[str, str, str, int, bool, str]] = {
    "main_menu_message": ("Main menu text", "🏠", "mainmenu", 600, False, ""),
    "choose_message": ("Choose collection text", "🎬", "choose", 800, False, ""),
    "invalid_message": ("Invalid number text", "⚠️", "invalid", 600, False, "{max}"),
    "preparing_message": ("Preparing text", "⏳", "preparing", 400, False, "{count}"),
    "delivered_message": ("Videos delivered text", "✅", "delivered", 800, False,
                          "{count} {auto_delete}"),
    "exhausted_message": ("No more videos text", "🏁", "exhausted", 800, False, "{collection}"),
    "empty_message": ("Library empty text", "📭", "empty", 600, False, ""),
    "busy_message": ("Request in progress alert", "⏳", "busy", 190, True, ""),
    "rate_message": ("Too many requests alert", "🚦", "rate", 190, True, ""),
    "expired_message": ("Expired request alert", "⌛", "expired", 190, True, ""),
    "unavailable_message": ("Option unavailable alert", "🚫", "unavail", 190, True, ""),
    "failed_message": ("Delivery failed text", "❌", "failed", 600, False, ""),
    "error_message": ("Generic error text", "🛟", "error", 600, False, ""),
    "botoff_message": ("Bot OFF text", "🔴", "botoff", 600, False, ""),
    "banned_message": ("Blocked user text", "⛔", "banned", 600, False, ""),
}


# Ready-made block reasons an admin can pick (stored in users.ban_reason as "preset:<key>").
# key -> (button label, full message shown to the blocked user). {contact} = developer handle.
BAN_PRESET_PREFIX = "preset:"
BAN_PRESETS: dict[str, tuple[str, str]] = {
    "rate": ("🚫 Too Many Requests",
             "🚫 Access Blocked! You exceeded the request limit. ⏳ Please try again later. "
             "📩 Contact Developer: {contact} for assistance."),
    "spam": ("🔒 Spam Detected",
             "🔒 Access Restricted! Spam or excessive requests were detected. 🚫 Stop sending "
             "repeated requests. 📩 Contact Developer: {contact} for help."),
    "suspicious": ("⚠️ Suspicious Activity",
                   "⚠️ Access Restricted! Unusual activity was detected on your account. 🔒 "
                   "Please wait or contact Developer: {contact} for assistance."),
    "limit": ("⛔ Usage Limit Exceeded",
              "⛔ Request Limit Reached! You have exceeded the allowed usage limit. ⏳ Try again "
              "later. 📩 Support: {contact}."),
    "admin": ("🚷 Admin Restriction",
              "🚷 Access Denied! Your account has been restricted by the bot administrator. "
              "📩 For clarification or an appeal, contact Developer: {contact}."),
}


def preset_key(reason: str | None) -> str | None:
    """'preset:rate' -> 'rate' (None if the reason is custom text or an unknown preset)."""
    if reason and reason.startswith(BAN_PRESET_PREFIX):
        key = reason[len(BAN_PRESET_PREFIX):]
        return key if key in BAN_PRESETS else None
    return None


def contact_handle(url: str | None) -> str:
    """Developer link (https://t.me/name) -> '@name'; 'the developer' if none can be derived."""
    if url:
        m = re.match(r"^(?:https?://)?(?:t|telegram)\.me/([A-Za-z0-9_]{3,})/?$", url.strip())
        if m:
            return "@" + m.group(1)
    return "the developer"


def ban_reason_label(reason: str | None) -> str:
    """Short plain-text description of a stored reason (for the admin panel)."""
    key = preset_key(reason)
    if key:
        return BAN_PRESETS[key][0]
    return reason or ""


def banned_text(reason: str | None = None, base: str | None = None,
                contact: str | None = None) -> str:
    """The notice a blocked user sees. A preset reason replaces the whole notice; a custom
    reason (HTML-escaped) is appended to the base text."""
    key = preset_key(reason)
    if key:
        return html.escape(BAN_PRESETS[key][1], quote=False).replace(
            "{contact}", html.escape(contact or "the developer", quote=False))
    text = base or BANNED_TEXT
    if reason and not reason.startswith(BAN_PRESET_PREFIX):
        text += f"\n\n📝 <b>Reason:</b> {html.escape(reason, quote=False)}"
    return text


NO_PERMISSION_TEXT = "⛔ You don't have permission to access the Admin Panel."

# Public buttons an admin may switch on/off. Main Menu / Back buttons are not configurable.
PUBLIC_BUTTONS = {
    "btn_request": "🎬 Request Videos",
    "btn_new": "🆕 New Videos",
    "btn_old": "📦 Old Videos",
    "btn_more": "🔥 More Videos",
    "btn_help": "ℹ️ Help",
    "btn_developer": "👨‍💻 Developer",
    "btn_favorites": "⭐ Favorites",
    "btn_activity": "📊 Activity",
    "btn_fresh": "✨ Fresh Batch",
    "btn_newreq": "🔄 New Request",
}
# Text on each public button. The admin can rename every one of them (Public Buttons -> Rename);
# an empty setting `lbl_<key>` means "use this built-in label". `btn_refer` has no switch of its
# own here: it is shown while the referral system is ON (Referral Manager / Public Buttons).
BUTTON_LABELS = {
    "btn_request": "🎬 Request Videos",
    "btn_new": "🆕 New",
    "btn_old": "📦 Old",
    "btn_more": "🔥 More",
    "btn_activity": "📊 Activity",
    "btn_favorites": "⭐ My Favorites",
    "btn_refer": "🎁 Refer & Earn",
    "btn_developer": "👨‍💻 Developer",
    "btn_help": "💡 Help",
    "btn_fresh": "✨ Fresh Batch",
    "btn_newreq": "🔄 New Request",
}
BUTTON_LABEL_MAX = 32
# At least one of these must stay enabled, otherwise users could not request anything.
REQUEST_BUTTONS = ("btn_request", "btn_new", "btn_old")

# The only callback actions a custom button may trigger (no free-form callback data).
CUSTOM_ACTIONS = {
    "request": ("🎬 Request Videos", "menu:request"),
    "new": ("🆕 New Videos", "menu:new"),
    "old": ("📦 Old Videos", "menu:old"),
    "help": ("ℹ️ Help", "help"),
    "developer": ("👨‍💻 Developer", "dev"),
    "activity": ("📊 My Requests", "activity"),
}

_ALLOWED_TAGS = ("b", "i", "u", "s", "code", "pre", "blockquote", "tg-spoiler", "a")
# One alternation over the (already HTML-escaped) text: a closing tag, a plain opening tag
# (<blockquote expandable> too), or <a href="..."> with a quoted link.
_TAG_RE = re.compile(
    r'&lt;(?:(/)(b|i|u|s|code|pre|blockquote|tg-spoiler|a)'
    r'|(b|i|u|s|code|pre|tg-spoiler|blockquote(?: expandable)?)'
    r'|a href="([^"&]*(?:&amp;[^"&]*)*)")&gt;', re.I)
_QUOTE_RE = re.compile(r"&lt;/?blockquote(?: expandable)?&gt;", re.I)
_LINK_OK = re.compile(r"(?:https?|tg)://\S+\Z", re.I)


class ValidationError(ValueError):
    """Message is safe to show to the admin."""


# ----------------------------------------------------------------------- validators
def validate_url(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > 256 or re.search(r"\s", value):
        raise ValidationError("Send a valid link starting with https:// (no spaces, max 256 characters).")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or "." not in parsed.netloc:
        raise ValidationError("The link must start with http:// or https:// and contain a domain.")
    if "@" in parsed.netloc:
        raise ValidationError("Links with embedded credentials are not allowed.")
    return value


# Quick request buttons (1 5 10 ...) shown under "How many videos?". Admin-editable.
DEFAULT_QTY = (1, 5, 10, 15, 20, 25, 30, 50)
QTY_MAX_BUTTONS = 12
QTY_HIGHEST = 200


def parse_qty_list(raw: str) -> list[int]:
    """'1, 5 10;20' -> [1, 5, 10, 20] (unique, ascending). Raises ValidationError."""
    tokens = [t for t in re.split(r"[\s,;|/]+", raw.strip()) if t]
    if not tokens:
        raise ValidationError("Send the numbers separated by commas, e.g. 1,5,10,20,50.")
    nums: list[int] = []
    for t in tokens:
        if not re.fullmatch(r"[0-9]{1,4}", t):
            raise ValidationError(f"“{t[:12]}” is not a whole number. Example: 1,5,10,20,50.")
        n = int(t)
        if not 1 <= n <= QTY_HIGHEST:
            raise ValidationError(f"Every number must be between 1 and {QTY_HIGHEST}.")
        if n not in nums:
            nums.append(n)
    if len(nums) > QTY_MAX_BUTTONS:
        raise ValidationError(f"At most {QTY_MAX_BUTTONS} buttons (you sent {len(nums)}).")
    return sorted(nums)


def _handle(raw: str, base: str, label: str, hosts: tuple[str, ...]) -> str:
    """Accept a bare username (optionally with @) or a full URL on one of `hosts`."""
    value = raw.strip()
    if re.fullmatch(r"@?[A-Za-z0-9_.\-]{1,39}", value):
        return f"{base}{value.lstrip('@')}"
    url = validate_url(value)
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host not in hosts:
        raise ValidationError(f"Send a {label} username or a link on {hosts[0]}.")
    return url


def validate_telegram(raw: str) -> str:
    return _handle(raw, "https://t.me/", "Telegram", ("t.me", "telegram.me"))


def validate_instagram(raw: str) -> str:
    return _handle(raw, "https://instagram.com/", "Instagram", ("instagram.com",))


def validate_github(raw: str) -> str:
    return _handle(raw, "https://github.com/", "GitHub", ("github.com",))


_MD_RULES = (
    (re.compile(r"\*\*(.+?)\*\*", re.S), "b"),     # **bold**
    (re.compile(r"__(.+?)__", re.S), "u"),         # __underline__
    (re.compile(r"~~(.+?)~~", re.S), "s"),         # ~~strike~~
    (re.compile(r"`([^`\n]+)`"), "code"),          # `code`
)


def _markdown_to_tags(text: str) -> str:
    """Admins often paste Markdown (**bold**, `code`). Turn it into the allowed tags so it
    does not show up as literal asterisks. Text inside `code` is left untouched."""
    parts = re.split(r"(`[^`\n]+`)", text)
    for i, part in enumerate(parts):
        if i % 2:
            inner = part[1:-1].replace("<", "\x00l").replace(">", "\x00g")   # shown literally
            parts[i] = f"<code>{inner}</code>"
            continue
        for rx, tag in _MD_RULES[:3]:
            part = rx.sub(lambda m, t=tag: f"<{t}>{m.group(1)}</{t}>", part)
        parts[i] = part
    return "".join(parts)


def sanitize_html(text: str, max_len: int = 3500, allow_quote: bool = True) -> str:
    """Admin text -> safe Telegram HTML. Everything is escaped except <b> <i> <u> <s> <code>
    <pre> <blockquote> <tg-spoiler> and <a href="https://...">, which must be properly closed.
    `allow_quote=False` drops <blockquote> (it cannot be nested inside another quote).
    Raises ValidationError otherwise."""
    text = text.strip()
    if not text:
        raise ValidationError("The text cannot be empty.")
    if len(text) > max_len:
        raise ValidationError(f"Too long ({len(text)}/{max_len} characters).")
    text = _markdown_to_tags(text)
    escaped = html.escape(text, quote=False)
    if not allow_quote:
        escaped = _QUOTE_RE.sub("", escaped)
    stack: list[str] = []
    out: list[str] = []
    pos = 0
    for m in _TAG_RE.finditer(escaped):
        out.append(escaped[pos:m.start()])
        if m.group(2):                                   # closing tag
            tag = m.group(2).lower()
            if not stack or stack[-1] != tag:
                raise ValidationError(f"Unbalanced formatting: unexpected </{tag}>.")
            stack.pop()
            out.append(f"</{tag}>")
        elif m.group(3):                                 # plain opening tag
            opened = m.group(3).lower()
            stack.append(opened.split(" ")[0])
            out.append(f"<{opened}>")
        else:                                            # <a href="...">
            if not _LINK_OK.match(html.unescape(m.group(4))):
                raise ValidationError("Links must start with https:// (or http://).")
            stack.append("a")
            out.append(f'<a href="{m.group(4)}">')
        pos = m.end()
    out.append(escaped[pos:])
    if stack:
        raise ValidationError(f"Unbalanced formatting: <{stack[-1]}> is not closed.")
    return "".join(out).replace("\x00l", "&lt;").replace("\x00g", "&gt;")


def _safe_link(validator, value: str) -> str:
    """Environment-provided link: normalised (e.g. @name -> https://t.me/name); an invalid
    value is dropped instead of producing a button Telegram would reject."""
    if not value:
        return ""
    try:
        return validator(value)
    except ValidationError:
        log.warning("invalid_developer_link_in_environment")
        return ""


def _safe_html(text: str) -> str:
    """Environment-provided text: keep allowed formatting, escape the rest, never raise."""
    if not text:
        return ""
    try:
        return sanitize_html(text, 300, allow_quote=False)
    except ValidationError:
        return html.escape(text[:300], quote=False)


def plain_text(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


# ----------------------------------------------------------------------- service
class SettingsService:
    def __init__(self, db: Database, cfg: Config) -> None:
        self.db = db
        self.cfg = cfg
        self._saved: dict[str, str] = {}
        self._defaults: dict[str, str] = {
            "bot_enabled": "1",
            "maintenance_mode": "0",
            "maintenance_custom": "0",
            "maintenance_title": DEFAULT_MAINTENANCE_TITLE,
            "maintenance_emoji": DEFAULT_MAINTENANCE_EMOJI,
            "maintenance_message": DEFAULT_MAINTENANCE_MESSAGE,
            "welcome_message": "",           # empty = built-in welcome screen
            "welcome_enabled": "1",
            "age_gate_enabled": "1",         # 🔞 users must confirm 18+ before they can use the bot
            "welcome_animation": "1",        # 🔥 fire effect on the /start welcome
            "group_mode": "1",               # 1 = the bot also answers commands in group chats
            "help_message": "",              # empty = built-in help page
            "delete_notice": "",             # empty = built-in auto-delete notice
            "ask_new_message": "",           # empty = built-in "How many New videos?" prompt
            "ask_old_message": "",           # empty = built-in "How many Old videos?" prompt
            "ask_any_message": "",           # empty = built-in generic "How many videos?" prompt
            **{key: "" for key in MESSAGES},  # empty = built-in text (see MESSAGES)
            "delete_notice_enabled": "1",
            "keep_source_caption": "0",
            "protect_content": "1",           # 1 = delivered videos cannot be forwarded or saved
            "allow_duplicate_videos": "0",    # 1 = also send re-posted copies of the same video
            "auto_delete_seconds": str(cfg.auto_delete_seconds),
            "max_videos_per_request": str(cfg.max_videos_per_request),
            "new_video_order": cfg.new_video_order,
            "old_video_order": cfg.old_video_order,
            "delivery_batch_size": str(cfg.delivery_batch_size),
            "rate_limit_requests": str(cfg.rate_limit_requests),
            "developer_name": cfg.developer_name,
            "developer_website": _safe_link(validate_url, cfg.developer_website),
            "developer_telegram": _safe_link(validate_telegram, cfg.developer_telegram),
            "developer_instagram": _safe_link(validate_instagram, cfg.developer_instagram),
            "developer_github": _safe_link(validate_github, cfg.developer_github),
            "developer_bio": _safe_html(cfg.developer_bio),
            "developer_enabled": "1",
            "custom_button": "",
            "remaining_new_display": "",     # admin-set "Unique videos remaining: New" (empty = automatic)
            "remaining_old_display": "",     # admin-set "Unique videos remaining: Old" (empty = automatic)
            "qty_enabled": "1",              # quick number buttons under "How many videos?"
            "qty_custom": "1",               # ✏️ Custom button next to them
            "qty_buttons": ",".join(str(n) for n in DEFAULT_QTY),
            "ref_enabled": "1",              # referral system on/off
            "ref_target": "20",              # eligible referrals needed per reward
            "ref_per_pool": "5",             # reward videos per pool (English / Desi)
            "ref_repeat": "0",               # 1 = a new reward for every further `ref_target`
            **{key: "1" for key in PUBLIC_BUTTONS},
            **{f"lbl_{key}": "" for key in BUTTON_LABELS},   # empty = built-in label
        }
        # developer_enabled and btn_developer are the same switch (one source of truth).
        self._alias = {"developer_enabled": "btn_developer"}

    # ---------- loading / persistence ----------
    async def load(self) -> None:
        rows = await self.db.pool.fetch("SELECT key, value FROM bot_settings")
        self._saved = {r["key"]: r["value"] for r in rows}
        log_event(log, logging.INFO, "settings_loaded", overrides=len(self._saved))

    async def set(self, key: str, value: str) -> None:
        key = self._alias.get(key, key)
        if key not in self._defaults:
            raise KeyError(key)
        await self.db.pool.execute(
            "INSERT INTO bot_settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            key, value)
        self._saved[key] = value

    async def reset(self, key: str) -> None:
        key = self._alias.get(key, key)
        await self.db.pool.execute("DELETE FROM bot_settings WHERE key = $1", key)
        self._saved.pop(key, None)

    # ---------- typed reads (cache only, no I/O) ----------
    def get(self, key: str) -> str:
        key = self._alias.get(key, key)
        if key in self._saved:
            return self._saved[key]
        return self._defaults[key]

    def is_custom(self, key: str) -> bool:
        return self._alias.get(key, key) in self._saved

    def flag(self, key: str) -> bool:
        return self.get(key).strip().lower() in ("1", "true", "yes", "on")

    def number(self, key: str) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return int(self._defaults[key])

    async def set_bool(self, key: str, value: bool) -> None:
        await self.set(key, "1" if value else "0")

    # ---------- "Unique videos remaining" shown on the activity screen ----------
    def remaining_override(self, category: str) -> int | None:
        """Number an admin typed in for `category` ('new' / 'old'); None = show the real count."""
        raw = self.get(f"remaining_{category}_display").strip()
        return int(raw) if raw.isdigit() else None

    # ---------- bot state ----------
    @property
    def bot_enabled(self) -> bool:
        return self.flag("bot_enabled")

    @property
    def maintenance(self) -> bool:
        return self.flag("maintenance_mode")

    def status_label(self) -> str:
        if not self.bot_enabled:
            return "🔴 OFFLINE"
        if self.maintenance:
            return "🟡 MAINTENANCE"
        return "🟢 ONLINE"

    # ---------- public buttons ----------
    def button_on(self, key: str) -> bool:
        return self.flag(key)

    def label(self, key: str) -> str:
        """Text on a public button: the admin's own label, else the built-in one."""
        custom = self.get(f"lbl_{key}").strip()
        return custom or BUTTON_LABELS[key]

    def can_disable(self, key: str) -> bool:
        """Switching `key` off must leave users a way to request videos."""
        if key not in REQUEST_BUTTONS:
            return True
        return any(self.flag(k) for k in REQUEST_BUTTONS if k != key)

    # ---------- custom button ----------
    def custom_button(self) -> dict:
        raw = self.get("custom_button")
        base = {"name": "", "emoji": "", "kind": "url", "target": "", "enabled": False,
                "position": "bottom"}
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    base.update({k: data[k] for k in base if k in data})
            except ValueError:
                log.warning("custom_button_corrupt")
        return base

    async def save_custom_button(self, data: dict) -> None:
        await self.set("custom_button", json.dumps(data, ensure_ascii=False))

    @staticmethod
    def custom_ready(btn: dict) -> bool:
        if not btn["name"] or not btn["target"]:
            return False
        if btn["kind"] == "url":
            return True
        return btn["kind"] == "action" and btn["target"] in CUSTOM_ACTIONS

    # ---------- quick request buttons ----------
    def qty_numbers(self) -> list[int]:
        """Numbers on the quick buttons (admin list, else the default). Never raises."""
        try:
            return parse_qty_list(self.get("qty_buttons"))
        except ValidationError:
            return list(DEFAULT_QTY)

    # ---------- developer ----------
    def developer(self) -> dict:
        return {
            "name": self.get("developer_name") or "Developer",
            "bio": self.get("developer_bio"),
            "website": self.get("developer_website"),
            "telegram": self.get("developer_telegram"),
            "instagram": self.get("developer_instagram"),
            "github": self.get("developer_github"),
            "enabled": self.flag("btn_developer"),
        }

    # ---------- maintenance text ----------
    def maintenance_text(self) -> str:
        if self.flag("maintenance_custom"):
            title, msg, emoji = (self.get("maintenance_title"), self.get("maintenance_message"),
                                 self.get("maintenance_emoji"))
        else:
            title, msg, emoji = (DEFAULT_MAINTENANCE_TITLE, DEFAULT_MAINTENANCE_MESSAGE,
                                 DEFAULT_MAINTENANCE_EMOJI)
        return f"{emoji} <b>{title}</b>\n\n{msg}"

    # ---------- runtime application ----------
    def apply_runtime(self, videos=None) -> None:
        """Copy the values other services read from Config / VideoService onto the live objects."""
        c = self.cfg
        c.auto_delete_seconds = self.number("auto_delete_seconds")
        c.max_videos_per_request = self.number("max_videos_per_request")
        c.rate_limit_requests = self.number("rate_limit_requests")
        c.delivery_batch_size = self.number("delivery_batch_size")
        c.developer_name = self.get("developer_name")
        order_old, order_new = self.get("old_video_order"), self.get("new_video_order")
        c.old_video_order = order_old if order_old in ORDERS else c.old_video_order
        c.new_video_order = order_new if order_new in ORDERS else c.new_video_order
        if videos is not None:
            videos.old_order = c.old_video_order
            videos.new_order = c.new_video_order
            videos.allow_duplicates = self.flag("allow_duplicate_videos")
