"""All user-facing texts and keyboards in one place (HTML parse mode)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton as _Btn, InlineKeyboardMarkup as Kb

from config import Config
from services.settings_service import (BUTTON_LABELS, CUSTOM_ACTIONS, DEFAULT_QTY,
                                       MESSAGES, SettingsService, contact_handle, plain_text)

LINE = "▰▰▰▰▰▰▰▰▰▰▰▰▰▰"


def bar(done: int, total: int, width: int = 10) -> str:
    """Modern progress bar: ▰▰▰▰▱▱▱▱▱▱"""
    if total <= 0:
        return "▱" * width
    filled = max(0, min(width, round(width * done / total)))
    if done and not filled:
        filled = 1
    return "▰" * filled + "▱" * (width - filled)

# Telegram can colour an inline button: "primary" (blue), "success" (green), "danger" (red).
# Bot API / aiogram versions that do not know the field simply get a normal button, so an
# old deployment never breaks - it just looks plain.
PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"
_STYLE_SUPPORTED = "style" in _Btn.model_fields


def Btn(text: str, style: str | None = None, **kwargs) -> _Btn:
    """InlineKeyboardButton with an optional colour (`style=PRIMARY|SUCCESS|DANGER`)."""
    if style and _STYLE_SUPPORTED:
        kwargs["style"] = style
    return _Btn(text=text, **kwargs)


FOOTER = "<i>⚡ VideoShare · Fast · Unique · Private</i>"
LABEL = {"new": "New", "old": "Old"}
ICON = {"new": "🆕", "old": "📦"}


def other(category: str) -> str:
    return "old" if category == "new" else "new"


def fmt_duration(seconds: int) -> str:
    if seconds % 60 == 0:
        m = seconds // 60
        return f"{m} minute" + ("" if m == 1 else "s")
    return f"{seconds} seconds"


def videos_word(n: int) -> str:
    return "video" if n == 1 else "videos"


# ---------------------------------------------------------------- texts
def _fill(text: str, cfg: Config, **extra) -> str:
    """Placeholders an admin may use in custom texts."""
    text = (text.replace("{auto_delete}", fmt_duration(cfg.auto_delete_seconds))
                .replace("{max}", str(cfg.max_videos_per_request)))
    for key, value in extra.items():
        text = text.replace("{" + key + "}", str(value))
    return text


# The admin's custom texts for the messages listed in MESSAGES. Bound once at startup so the
# text helpers below keep their simple signatures; without it (tests) the defaults are used.
_settings: SettingsService | None = None
_cfg: Config | None = None


def bind(settings: SettingsService, cfg: Config) -> None:
    global _settings, _cfg
    _settings, _cfg = settings, cfg


def custom_message(key: str, value: str | None = None) -> str:
    """The admin's custom text for `key` ("" = use the built-in text)."""
    if value is not None:
        return value
    return _settings.get(key) if _settings is not None else ""


def alert_text(text: str) -> str:
    """Pop-up alerts are plain text, max 200 characters."""
    return plain_text(text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")[:190]


def message(key: str, default: str, *, value: str | None = None, **extra) -> str:
    """Custom text (placeholders filled) if the admin set one, else `default`."""
    custom = custom_message(key, value)
    if not custom:
        return default
    cfg = _cfg
    return _fill(custom, cfg, **extra) if cfg is not None else custom


# Sample values used when the admin previews a text.
SAMPLE = {"count": 5, "collection": "New"}


def default_message(key: str, cfg: Config) -> str:
    """The built-in text of every message in MESSAGES (placeholders already filled)."""
    return {
        "main_menu_message": DEFAULT_MAIN_MENU,
        "choose_message": DEFAULT_CHOOSE,
        "invalid_message": default_invalid(cfg),
        "preparing_message": default_preparing(5),
        "delivered_message": default_delivered(cfg, 5),
        "exhausted_message": default_exhausted("new", False),
        "empty_message": DEFAULT_EMPTY,
        "busy_message": DEFAULT_BUSY,
        "rate_message": DEFAULT_RATE,
        "expired_message": DEFAULT_EXPIRED,
        "unavailable_message": DEFAULT_UNAVAILABLE,
        "failed_message": DEFAULT_FAILED,
        "error_message": DEFAULT_ERROR,
        "botoff_message": DEFAULT_BOT_OFF,
        "banned_message": DEFAULT_BANNED,
    }[key]


def preview_message(key: str, cfg: Config, value: str | None = None) -> str:
    """What a user sees for `key`, optionally with a not-yet-saved `value`."""
    default = default_message(key, cfg)
    custom = custom_message(key, value)
    text = _fill(custom, cfg, **SAMPLE) if custom else default
    return alert_text(text) if MESSAGES[key][4] else text


def default_welcome(cfg: Config, name: str | None = None) -> str:
    """The /start welcome card. `name` must already be HTML-escaped.
    The long intro line wraps on every phone, which makes Telegram stretch the message to its
    full width - so the inline buttons under it span the whole chat width too."""
    hello = f"Hey <b>{name}</b> 👋\n\n" if name else ""
    return (
        "🔥 <b>WELCOME TO VIDEOSHARE</b>\n"
        "<i>Instant • Unique • Private</i>\n\n"
        f"{hello}"
        "🚀 Welcome to <b>VideoShare</b> — your simple and fast video collection.\n\n"
        "<blockquote>"
        "🆕 <b>New</b> — Latest video drops\n"
        "📦 <b>Old</b> — Explore the classic collection\n"
        "⭐ <b>Favorites</b> — Save your favorite videos\n"
        "📊 <b>Activity</b> — Check your recent activity"
        "</blockquote>\n\n"
        "👇 <b>Pick an option below to get started</b>"
    )


def welcome_text(cfg: Config, s: SettingsService | None = None, name: str | None = None) -> str:
    """`name` must already be HTML-escaped. Only the built-in welcome greets by name."""
    custom = s.get("welcome_message") if s else ""
    return _fill(custom, cfg, name=name or "there") if custom else default_welcome(cfg, name)


DEFAULT_MAIN_MENU = ("🏠 <b>MAIN MENU</b>\n"
                     "<i>Pick what you want to do</i>")


def main_menu_text() -> str:
    return message("main_menu_message", DEFAULT_MAIN_MENU)


ASK_KEYS = {"new": "ask_new_message", "old": "ask_old_message", None: "ask_any_message"}


def custom_ask_text(s: SettingsService | None, category: str | None) -> str:
    """The admin's prompt for this entry point. New/Old fall back to the Request text when
    they have no text of their own, so editing the Request text changes every prompt."""
    if not s:
        return ""
    return s.get(ASK_KEYS[category]) or s.get(ASK_KEYS[None])


def default_ask_count(cfg: Config, category: str | None = None) -> str:
    what = f"{LABEL[category]} videos" if category else "videos"
    return (f"🎬 <b>How many {what}?</b>\n\n"
            f"<blockquote>Send a number <code>1</code> – <code>{cfg.max_videos_per_request}</code>\n"
            "Quick picks: <code>5</code> · <code>10</code> · <code>20</code></blockquote>\n\n"
            "⌨️ <i>Type your number below</i>")


# Shown ONLY to users whose Save / Forward is OFF (the default for everybody).
SAVE_CONTACT = "@DarkSMBot"
_FOOTER_LINE = LINE


def save_contact() -> str:
    """Who to contact for save / forward access: the developer's Telegram handle from the admin
    panel (Developer Settings), else the built-in contact."""
    if _settings is not None:
        handle = contact_handle(_settings.developer()["telegram"])
        if handle.startswith("@"):
            return handle
    return SAVE_CONTACT


def protected_footer() -> str:
    return (f"{_FOOTER_LINE}\n"
            "🔒 <b>Want to save or forward videos?</b>\n"
            f"💬 Contact owner: {save_contact()}")


def with_protection_footer(text: str, protected: bool) -> str:
    """Add the contact footer for protected users. If an admin already typed the footer into a
    custom text (it contains the contact name) nothing is added twice."""
    if not protected or save_contact().lower() in text.lower():
        return text
    return f"{text}\n\n{protected_footer()}"


def ask_count_text(cfg: Config, category: str | None = None, fresh: bool = False,
                   s: SettingsService | None = None, protected: bool = False) -> str:
    head = "🔄 <b>NEW REQUEST</b>\n\n" if fresh else ""
    custom = custom_ask_text(s, category)
    body = head + (_fill(custom, cfg) if custom else default_ask_count(cfg, category))
    return with_protection_footer(body, protected)


def default_invalid(cfg: Config) -> str:
    return ("⚠️ <b>Invalid number</b>\n\n"
            f"<blockquote>Enter a number from <code>1</code> to "
            f"<code>{cfg.max_videos_per_request}</code></blockquote>")


def invalid_text(cfg: Config) -> str:
    return message("invalid_message", default_invalid(cfg))


DEFAULT_CHOOSE = ("🎬 <b>Choose a collection</b>\n\n"
                  "<blockquote>🆕 <b>New</b>  ·  latest unique videos\n"
                  "📦 <b>Old</b>  ·  older unique videos</blockquote>")


def choose_collection_text() -> str:
    return message("choose_message", DEFAULT_CHOOSE)


def default_help(cfg: Config) -> str:
    return (
        "💡 <b>HOW IT WORKS</b>\n\n"
        "<blockquote>"
        "① Enter how many videos you want\n"
        "② Choose <b>New</b> or <b>Old</b>\n"
        "③ Videos arrive straight in this chat\n"
        "④ Tap <b>More New / More Old</b> to continue\n"
        "⑤ You never get the same video twice"
        "</blockquote>\n\n"
        f"⏱ Videos vanish after <b>{fmt_duration(cfg.auto_delete_seconds)}</b>"
    )


# Every command a normal user can type. Single source of truth for /help and the "/" menu.
PRIVATE_COMMANDS = (
    ("start", "Open the main menu"),
    ("menu", "Show the main menu"),
    ("help", "How the bot works + all commands"),
    ("refer", "Your referral link"),
    ("stats", "Your referral stats"),
    ("rewards", "Your referral rewards"),
)
GROUP_COMMANDS = (
    ("video", "Request videos - choose New or Old"),
    ("new", "Get unseen NEW videos (/new 5)"),
    ("old", "Get unseen OLD videos (/old 5)"),
    ("progress", "See your progress"),
    ("help", "Show all commands"),
)


def commands_text(group_limit: int | None = None) -> str:
    """The 'all public commands' block that /help shows (private chat and groups)."""
    lines = [f"/{c} – {d}" for c, d in PRIVATE_COMMANDS]
    glines = [f"/{c} – {d}" for c, d in GROUP_COMMANDS]
    cap = f" (max {group_limit} videos per request)" if group_limit else ""
    return ("📜 <b>COMMANDS</b>\n\n"
            "💬 <b>Private chat</b>\n<blockquote expandable>" + "\n".join(lines) + "</blockquote>"
            + f"\n\n👥 <b>Groups</b>{cap}\n<blockquote expandable>" + "\n".join(glines) + "</blockquote>")


def help_text(cfg: Config, s: SettingsService | None = None) -> str:
    custom = s.get("help_message") if s else ""
    base = _fill(custom, cfg) if custom else default_help(cfg)
    return f"{base}\n\n{commands_text(getattr(cfg, 'group_max_videos', None))}"


def default_delete_notice(cfg: Config) -> str:
    return ("⏱ <b>AUTO-DELETE ON</b>\n\n"
            "<blockquote>Videos sent by this bot are removed automatically after "
            f"<b>{fmt_duration(cfg.auto_delete_seconds)}</b>.</blockquote>")


def delete_notice_text(cfg: Config, s: SettingsService | None = None) -> str:
    custom = s.get("delete_notice") if s else ""
    return _fill(custom, cfg) if custom else default_delete_notice(cfg)


def developer_text(s: SettingsService, bio: str | None = None) -> str:
    d = s.developer()
    bio = (d["bio"] if bio is None else bio) or "Creator &amp; Developer of VideoShare"
    has_links = any(d[k] for k in ("website", "instagram", "telegram", "github"))
    text = (f"👨‍💻 <b>DEVELOPER</b>\n{LINE}\n\n<b>{html_escape(d['name'])}</b>\n"
            f"<blockquote>{bio}</blockquote>")
    if has_links:
        text += "\n\n🔗 <i>Connect with the developer</i>"
    return text


def html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def video_caption(cfg: Config, video, position: int, source_caption: str | None = None) -> str:
    """Short, clean caption. Only our own label is used unless the admin opted in to keep the
    source caption (channel captions can contain channel names or links)."""
    number = video.video_number or position
    collection = {"new": "🆕 New", "old": "📦 Old"}.get(video.category or "", "")
    text = f"🎬 <b>Video #{number}</b>"
    if collection:
        text += f"  ·  {collection}"
    text += f"\n<i>⏱ Auto-removed in {fmt_duration(cfg.auto_delete_seconds)}</i>"
    if source_caption:
        extra = html_escape(source_caption.strip())[:300]
        if extra:
            text += f"\n\n{extra}"
    return text


def default_preparing(n: int | None = None) -> str:
    if n is None:
        return "⏳ <b>Preparing your videos…</b>\n<i>▰▰▱▱▱▱▱▱▱▱</i>"
    return f"⏳ <b>Preparing {n} unique {videos_word(n)}…</b>\n<i>▰▰▱▱▱▱▱▱▱▱</i>"


def preparing_text() -> str:
    return message("preparing_message", default_preparing(), count="your")


def preparing_n_text(n: int, category: str, short_of: int | None = None) -> str:
    text = message("preparing_message", default_preparing(n), count=n)
    if short_of is not None:
        text = (f"⚠️ <b>Only {n} {category} {videos_word(n)} left that you "
                f"haven't received yet.</b>\n\n") + text
    return text


def _short_note(n: int, category: str, short_of: int) -> str:
    return (f"⚠️ <i>Only {n} unseen {LABEL[category]} {videos_word(n)} were left "
            f"(you asked for {short_of}).</i>")


def default_delivered(cfg: Config, n: int, category: str = "new",
                      short_of: int | None = None) -> str:
    text = f"✅ <b>{n} unique {videos_word(n)} delivered</b>"
    if short_of is not None:
        text += "\n" + _short_note(n, category, short_of)
    text += (f"\n\n<blockquote>⏱ Auto-removed after "
             f"{fmt_duration(cfg.auto_delete_seconds)}</blockquote>")
    return text


def delivered_text(cfg: Config, n: int, category: str, short_of: int | None = None,
                   protected: bool = False) -> str:
    custom = custom_message("delivered_message")
    if not custom:
        text = default_delivered(cfg, n, category, short_of)
    else:
        text = _fill(custom, cfg, count=n, collection=LABEL.get(category, ""))
        if short_of is not None:      # the shortfall warning is always kept
            text += "\n\n" + _short_note(n, category, short_of)
    return with_protection_footer(text, protected)


def default_exhausted(category: str, other_left: bool = False) -> str:
    where = "newest to oldest" if category == "new" else "oldest to newest"
    icon = ICON.get(category, "🎬")
    tail = ("👉 You can still continue with the other collection." if other_left else
            "🔔 New videos will appear here as soon as they are added.")
    return (f"{icon} <b>End of {LABEL.get(category, '')} Videos</b>\n"
            f"{bar(1, 1)}\n\n"
            f"<blockquote>You've received every available unique video ({where}).</blockquote>\n\n{tail}")


def exhausted_text(category: str, other_left: bool = False) -> str:
    return message("exhausted_message", default_exhausted(category, other_left),
                   collection=LABEL.get(category, ""))


DEFAULT_EMPTY = ("📭 <b>No videos yet</b>\n\n"
                 "<blockquote>The library is empty right now. Please check back soon.</blockquote>")


def empty_text() -> str:
    return message("empty_message", DEFAULT_EMPTY)


def progress_text(p: dict) -> str:
    """The 'Continue' screen: where each collection stands for this user."""
    def line(mode: str) -> str:
        d = p[mode]
        total = d["received"] + d["left"]
        state = "✅ finished" if d["left"] == 0 and d["received"] else f"{d['left']:,} left"
        return (f"{ICON[mode]} <b>{LABEL[mode]}</b>\n"
                f"<code>{bar(d['received'], total)}</code>  {d['received']:,} received · {state}")
    return ("🔥 <b>CONTINUE WHERE YOU STOPPED</b>\n\n<blockquote>" + line("new") + "\n\n"
            + line("old") + "</blockquote>\n\n<i>Each collection remembers your place.</i>")


def activity_text(a: dict) -> str:
    return (
        "📊 <b>YOUR ACTIVITY</b>\n"
        f"{LINE}\n\n"
        f"🎬 <b>{a['total']:,}</b> videos received   ·   🔄 <b>{a['requests']:,}</b> requests\n\n"
        "<blockquote>"
        f"🆕 <b>New</b>\n<code>{bar(a['new'], a['new'] + a['left_new'])}</code>  "
        f"{a['new']:,} received · {a['left_new']:,} left\n\n"
        f"📦 <b>Old</b>\n<code>{bar(a['old'], a['old'] + a['left_old'])}</code>  "
        f"{a['old']:,} received · {a['left_old']:,} left"
        "</blockquote>\n\n"
        f"{FOOTER}"
    )


DEFAULT_BUSY = "⏳ Your last request is still being processed."
DEFAULT_RATE = "⏳ Please wait a moment before requesting another batch."
DEFAULT_FAILED = "⚠️ I couldn't send the videos right now. Please try again in a moment."
DEFAULT_ERROR = "⚠️ Something went wrong. Please try again later."
DEFAULT_EXPIRED = "This request has expired. Send a number again."
DEFAULT_UNAVAILABLE = "This option is currently unavailable."
DEFAULT_BOT_OFF = "🔴 <b>VideoShare is paused</b>\n<i>Please try again later.</i>"
DEFAULT_BANNED = "⛔ <b>Access restricted</b>\n<i>You can't use VideoShare right now.</i>"
BUSY_TEXT, RATE_TEXT, FAILED_TEXT = DEFAULT_BUSY, DEFAULT_RATE, DEFAULT_FAILED
ERROR_TEXT, EXPIRED_TEXT = DEFAULT_ERROR, DEFAULT_EXPIRED   # defaults; see the functions below


def busy_text() -> str:
    return message("busy_message", DEFAULT_BUSY)


def rate_text() -> str:
    return message("rate_message", DEFAULT_RATE)


def failed_text() -> str:
    return message("failed_message", DEFAULT_FAILED)


def error_text() -> str:
    return message("error_message", DEFAULT_ERROR)


def expired_text() -> str:
    return message("expired_message", DEFAULT_EXPIRED)


def unavailable_text() -> str:
    return message("unavailable_message", DEFAULT_UNAVAILABLE)


def bot_off_text() -> str:
    return message("botoff_message", DEFAULT_BOT_OFF)


def banned_base_text() -> str:
    return message("banned_message", DEFAULT_BANNED)


# ---------------------------------------------------------------- keyboards
def custom_button(s: SettingsService) -> tuple[Btn, str] | None:
    """The admin-defined public button (if enabled and valid) and where it goes."""
    c = s.custom_button()
    if not c["enabled"] or not s.custom_ready(c):
        return None
    label = f"{c['emoji']} {c['name']}".strip()
    if c["kind"] == "url":
        return Btn(text=label, url=c["target"]), c["position"]
    return Btn(text=label, callback_data=CUSTOM_ACTIONS[c["target"]][1]), c["position"]


def _lbl(s: SettingsService | None, key: str) -> str:
    """Button text: the admin's own label (Public Buttons -> Rename) or the built-in one."""
    return s.label(key) if s is not None else BUTTON_LABELS[key]


def _public_rows(s: SettingsService, *, with_activity: bool) -> list[list[Btn]]:
    """Modern 3-tier layout: one hero action, 2-column rows, colour-coded."""
    on = s.button_on
    rows: list[list[Btn]] = []
    if on("btn_request"):
        rows.append([Btn(_lbl(s, "btn_request"), PRIMARY, callback_data="menu:request")])
    pair = []
    if on("btn_new"):
        pair.append(Btn(_lbl(s, "btn_new"), SUCCESS, callback_data="menu:new"))
    if on("btn_old"):
        pair.append(Btn(_lbl(s, "btn_old"), callback_data="menu:old"))
    if pair:
        rows.append(pair)
    pair = []
    if on("btn_more"):
        pair.append(Btn(_lbl(s, "btn_more"), callback_data="menu:more"))
    if with_activity and on("btn_activity"):
        pair.append(Btn(_lbl(s, "btn_activity"), callback_data="activity"))
    if pair:
        rows.append(pair)
    if on("btn_favorites"):
        rows.append([Btn(_lbl(s, "btn_favorites"), callback_data="fav:list:1")])
    if s.flag("ref_enabled"):
        rows.append([Btn(_lbl(s, "btn_refer"), SUCCESS, callback_data="ref:home")])
    pair = []
    if on("btn_developer"):
        pair.append(Btn(_lbl(s, "btn_developer"), callback_data="dev"))
    if on("btn_help"):
        pair.append(Btn(_lbl(s, "btn_help"), callback_data="help"))
    if pair:
        rows.append(pair)
    custom = custom_button(s)
    if custom:
        btn, position = custom
        rows.insert(0, [btn]) if position == "top" else rows.append([btn])
    return rows


def _admin_row(is_admin: bool) -> list[list[Btn]]:
    return [[Btn("🛠 Admin Panel", DANGER, callback_data="adm:home")]] if is_admin else []


def _welcome_rows(s: SettingsService) -> list[list[Btn]]:
    """Welcome screen layout (every button obeys the same admin on/off switches as before):
         [ 🆕 New       ][ 📦 Old      ]
         [ 🔥 More      ][ 📊 Activity ]
         [        ⭐ My Favorites       ]
         [        🎁 Refer & Earn       ]
    A row with a single visible button automatically uses the full width. Optional buttons the
    admin has switched on (Request Videos, Developer, Help, custom button) follow below."""
    on = s.button_on
    rows: list[list[Btn]] = []
    pair = []
    if on("btn_new"):
        pair.append(Btn(_lbl(s, "btn_new"), SUCCESS, callback_data="menu:new"))
    if on("btn_old"):
        pair.append(Btn(_lbl(s, "btn_old"), callback_data="menu:old"))
    if pair:
        rows.append(pair)
    pair = []
    if on("btn_more"):
        pair.append(Btn(_lbl(s, "btn_more"), callback_data="menu:more"))
    if on("btn_activity"):
        pair.append(Btn(_lbl(s, "btn_activity"), callback_data="activity"))
    if pair:
        rows.append(pair)
    if on("btn_favorites"):
        rows.append([Btn(_lbl(s, "btn_favorites"), callback_data="fav:list:1")])
    if s.flag("ref_enabled"):
        rows.append([Btn(_lbl(s, "btn_refer"), SUCCESS, callback_data="ref:home")])
    if on("btn_request"):
        rows.append([Btn(_lbl(s, "btn_request"), PRIMARY, callback_data="menu:request")])
    pair = []
    if on("btn_developer"):
        pair.append(Btn(_lbl(s, "btn_developer"), callback_data="dev"))
    if on("btn_help"):
        pair.append(Btn(_lbl(s, "btn_help"), callback_data="help"))
    if pair:
        rows.append(pair)
    custom = custom_button(s)
    if custom:
        btn, position = custom
        rows.insert(0, [btn]) if position == "top" else rows.append([btn])
    return rows


def welcome_kb(s: SettingsService, is_admin: bool = False) -> Kb:
    rows = _welcome_rows(s) + _admin_row(is_admin)
    return Kb(inline_keyboard=rows or [[Btn("🏠 Main Menu", callback_data="menu")]])


def main_menu_kb(s: SettingsService, is_admin: bool = False) -> Kb:
    return Kb(inline_keyboard=_public_rows(s, with_activity=True) + _admin_row(is_admin))


def more_kb() -> Kb:
    return Kb(inline_keyboard=[
        [Btn("🔥 More New", SUCCESS, callback_data="mmore:new"),
         Btn("♻️ More Old", PRIMARY, callback_data="mmore:old")],
        [Btn("‹ Back", callback_data="menu")],
    ])


def help_kb(s: SettingsService) -> Kb:
    rows = []
    if s.button_on("btn_request"):
        rows.append([Btn(text=_lbl(s, "btn_request"), callback_data="menu:request")])
    elif s.button_on("btn_new") or s.button_on("btn_old"):
        rows.append([b for b in (
            Btn(text="🆕 New Videos", callback_data="menu:new") if s.button_on("btn_new") else None,
            Btn(text="📦 Old Videos", callback_data="menu:old") if s.button_on("btn_old") else None,
        ) if b])
    if s.button_on("btn_developer"):
        rows.append([Btn(text=_lbl(s, "btn_developer"), callback_data="dev")])
    rows.append([Btn(text="⬅️ Back", callback_data="menu")])
    return Kb(inline_keyboard=rows)


def developer_kb(s: SettingsService) -> Kb:
    d = s.developer()
    links = [("🌐 Website", d["website"]), ("📸 Instagram", d["instagram"]),
             ("💬 Telegram", d["telegram"]), ("🐙 GitHub", d["github"])]
    buttons = [Btn(text=label, url=url) for label, url in links if url]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn(text="⬅️ Back", callback_data="menu")])
    return Kb(inline_keyboard=rows)


def collection_kb() -> Kb:
    return Kb(inline_keyboard=[[
        Btn("🆕 New Videos", SUCCESS, callback_data="type:new"),
        Btn("📦 Old Videos", PRIMARY, callback_data="type:old"),
    ]])


def after_delivery_kb(category: str, count: int, s: SettingsService | None = None) -> Kb:
    def shown(key: str) -> bool:
        return s is None or s.button_on(key)
    rows: list[list[Btn]] = []
    if shown("btn_more"):
        rows.append([Btn("🔥 More New", SUCCESS, callback_data=f"more:new:{count}"),
                     Btn("♻️ More Old", PRIMARY, callback_data=f"more:old:{count}")])
    if shown("btn_fresh"):
        rows.append([Btn(_lbl(s, "btn_fresh"), PRIMARY, callback_data=f"fresh:{category}:{count}")])
    last = []
    if shown("btn_newreq"):
        last.append(Btn(_lbl(s, "btn_newreq"), callback_data="again"))
    if shown("btn_developer"):
        last.append(Btn(_lbl(s, "btn_developer"), callback_data="dev"))
    if last:
        rows.append(last)
    rows.append([Btn("🏠 Main Menu", callback_data="menu")])
    return Kb(inline_keyboard=rows)


def try_other_kb(category: str, count: int) -> Kb:
    o = other(category)
    label = "📦 Try Old Videos" if o == "old" else "🆕 Try New Videos"
    return Kb(inline_keyboard=[
        [Btn(text=label, callback_data=f"more:{o}:{count}"),
         Btn(text="🔄 Request Again", callback_data="again")],
        [Btn(text="🏠 Main Menu", callback_data="menu")],
    ])


def finished_kb() -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text="🔄 Request Again", callback_data="again"),
         Btn(text="🏠 Main Menu", callback_data="menu")],
    ])


def retry_kb(category: str, count: int) -> Kb:
    return Kb(inline_keyboard=[
        [Btn("🔄 Try Again", PRIMARY, callback_data=f"more:{category}:{count}"),
         Btn("🏠 Main Menu", callback_data="menu")],
    ])


def back_kb(s: SettingsService | None = None) -> Kb:
    rows = []
    if s is None or s.button_on("btn_request"):
        rows.append(Btn(text="🎬 Request Videos", callback_data="menu:request"))
    rows.append(Btn(text="🏠 Main Menu", callback_data="menu"))
    return Kb(inline_keyboard=[rows])


# ---------------------------------------------------------------- 18+ age gate
def age_gate_text(name: str | None = None) -> str:
    """`name` must already be HTML-escaped."""
    hello = f"Hey <b>{name}</b>,\n\n" if name else ""
    return (
        "🔞 <b>AGE VERIFICATION</b>\n"
        f"{LINE}\n\n"
        f"{hello}"
        "This bot contains <b>adult (18+) content</b>.\n\n"
        "<blockquote>By continuing you confirm that:\n"
        "✔ You are <b>18 years or older</b>\n"
        "✔ Adult content is legal where you live\n"
        "✔ You choose to view it yourself</blockquote>\n\n"
        "👇 <b>Tap Confirm to start</b>"
    )


def age_gate_kb() -> Kb:
    return Kb(inline_keyboard=[
        [Btn("✅  I'm 18+  ·  Confirm & Start", SUCCESS, callback_data="age:yes")],
        [Btn("✖  I'm under 18  ·  Exit", DANGER, callback_data="age:no")],
    ])


AGE_DENIED = (
    "🚫 <b>ACCESS DENIED</b>\n"
    f"{LINE}\n\n"
    "This bot is only for people aged <b>18 or older</b>.\n\n"
    "<i>Please close this chat. Stay safe!</i>"
)


def age_denied_kb() -> Kb:
    return Kb(inline_keyboard=[[Btn("↩️ I tapped by mistake", callback_data="age:back")]])


# ---------------------------------------------------------------- quick request buttons
QTY_PER_ROW = 4     # 4 per row keeps every button small


def qty_kb(s: SettingsService, cfg: Config) -> Kb:
    """Small number buttons under "How many videos?" (admin: Public Settings -> Quick Buttons).
    Only numbers up to the current maximum per request are shown. With the feature switched
    off the screen still gets its Back button."""
    if not s.flag("qty_enabled"):
        return Kb(inline_keyboard=[[Btn("‹ Back", callback_data="menu")]])
    top = cfg.max_videos_per_request
    nums = [n for n in s.qty_numbers() if n <= top]
    if not nums:      # the admin's list is all above the maximum: never show an empty row
        nums = [n for n in DEFAULT_QTY if n <= top] or [1]
    rows = [[Btn(f"⚡ {n}", callback_data=f"qty:{n}") for n in nums[i:i + QTY_PER_ROW]]
            for i in range(0, len(nums), QTY_PER_ROW)]
    if s.flag("qty_custom"):
        rows.append([Btn("✏️ Custom number", PRIMARY, callback_data="qty:custom")])
    rows.append([Btn("‹ Back", callback_data="menu")])
    return Kb(inline_keyboard=rows)


# ---------------------------------------------------------------- favorites
FAV_PAGE = 8
FAV_NAME_MAX = 40


def fav_label(row, limit: int = 28) -> str:
    name = (row["name"] or "").strip()
    text = name or (f"Video #{row['video_number']}" if row["video_number"] else "Video")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def fav_save_kb(video_id: int) -> Kb:
    """Under every video delivered in a private chat."""
    return Kb(inline_keyboard=[[Btn("⭐ Save", SUCCESS, callback_data=f"fav:add:{video_id}")]])


def fav_saved_kb(video_id: int) -> Kb:
    return Kb(inline_keyboard=[[Btn("✏️ Add name", callback_data=f"fav:nm:{video_id}"),
                                Btn("💔 Remove", DANGER, callback_data=f"fav:del:{video_id}")]])


def fav_empty_text() -> str:
    return ("⭐ <b>MY FAVORITES</b>\n\n"
            "<blockquote>Nothing saved yet.\n"
            "Tap <b>⭐ Save</b> under any video to keep it here.</blockquote>")


def fav_list_text(total: int, page: int, pages: int) -> str:
    return (f"⭐ <b>MY FAVORITES</b>  <code>{total:,}</code>\n\n"
            "<i>Tap a video to send it again, rename or remove it.</i>"
            + (f"\n\n<code>Page {page}/{pages}</code>" if pages > 1 else ""))


def fav_list_kb(rows, page: int, pages: int) -> Kb:
    items = [Btn("▶ " + fav_label(r), callback_data=f"fav:v:{r['video_id']}:{page}") for r in rows]
    kb_rows = [items[i:i + 2] for i in range(0, len(items), 2)]
    if pages > 1:
        nav = []
        if page > 1:
            nav.append(Btn("⬅️ Prev", callback_data=f"fav:list:{page - 1}"))
        nav.append(Btn(f"{page}/{pages}", callback_data="fav:noop"))
        if page < pages:
            nav.append(Btn("Next ➡️", callback_data=f"fav:list:{page + 1}"))
        kb_rows.append(nav)
    kb_rows.append([Btn("🏠 Main Menu", callback_data="menu")])
    return Kb(inline_keyboard=kb_rows)


def fav_detail_text(row) -> str:
    named = (row["name"] or "").strip()
    number = f"Video #{row['video_number']}" if row["video_number"] else "Video"
    head = f"⭐ <b>{html_escape(named)}</b>\n{number}" if named else f"⭐ <b>{number}</b>"
    note = "" if row["is_active"] else "\n\n⚠️ This video is not available right now."
    return f"{head}\n\n{LINE}{note}"


def fav_detail_kb(video_id: int, page: int) -> Kb:
    return Kb(inline_keyboard=[
        [Btn("▶️ Send again", PRIMARY, callback_data=f"fav:send:{video_id}:{page}")],
        [Btn("✏️ Name", callback_data=f"fav:nm:{video_id}:{page}"),
         Btn("🗑 Remove", DANGER, callback_data=f"fav:rm:{video_id}:{page}")],
        [Btn("⬅️ Back", callback_data=f"fav:list:{page}")],
    ])


def fav_name_prompt() -> str:
    return (f"✏️ <b>Name this favorite</b>\n\n"
            f"<blockquote>Send a name (up to {FAV_NAME_MAX} characters).\n"
            "Send <code>-</code> to remove the name. Optional.</blockquote>")


def fav_done_kb() -> Kb:
    return Kb(inline_keyboard=[[Btn("⭐ My Favorites", callback_data="fav:list:1"),
                                Btn("🏠 Main Menu", callback_data="menu")]])
