"""Favorites + quick request buttons against a REAL PostgreSQL (fake Telegram bot).
   DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_favorites.py"""
import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ui
from config import Config
from database import Database
from handlers import favorites as fh
from services.delivery_service import DeliveryService
from services.favorite_service import FavoriteService, MAX_PER_USER
from services.settings_service import SettingsService
from services.user_service import UserService
from services.video_service import VideoService


def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


class FakeBot:
    def __init__(self):
        self.sent, self._mid = [], 0

    async def send_video(self, chat_id, file_id, **kw):
        self.sent.append((chat_id, file_id, kw.get("reply_markup"), kw.get("protect_content")))
        self._mid += 1
        return types.SimpleNamespace(message_id=self._mid)

    async def send_message(self, *a, **k):
        pass


class FakeMsg:
    def __init__(self):
        self.chat = types.SimpleNamespace(id=1, type="private")
        self.markups, self.texts, self.answers = [], [], []

    async def edit_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.texts.append((text, reply_markup))

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append((text, reply_markup))


class FakeCb:
    def __init__(self, uid, data):
        self.from_user = types.SimpleNamespace(id=uid)
        self.data, self.message, self.popups = data, FakeMsg(), []

    async def answer(self, text=None, show_alert=False):
        self.popups.append((text, show_alert))


async def main():
    db = Database(os.environ["DATABASE_URL"], 2, 10)
    await db.connect()
    await db.pool.execute("TRUNCATE videos, user_favorites, user_states, delivered_messages, "
                          "user_rate_limits, bot_settings RESTART IDENTITY CASCADE")
    for i in range(1, 21):
        await db.pool.execute(
            "INSERT INTO videos (video_number, category, telegram_file_id, telegram_file_unique_id,"
            " is_active, channel_id, telegram_message_id, message_date) "
            "VALUES ($1,'new',$2,$3,TRUE,-100,$4,now())", i, f"f{i}", f"u{i}", i)
    cfg = Config.__new__(Config)
    cfg.__dict__.update(delivery_batch_size=1000, send_delay=0, auto_delete_seconds=120,
                        admin_ids=frozenset(), max_videos_per_request=50, rate_limit_requests=100,
                        rate_limit_window=60, new_video_order="desc", old_video_order="asc",
                        developer_name="d", developer_website="", developer_telegram="",
                        developer_instagram="", developer_github="", developer_bio="")
    users, videos, bot = UserService(db, 30), VideoService(db, "asc", 30), FakeBot()
    settings = SettingsService(db, cfg)
    await settings.load()
    delivery = DeliveryService(bot, db, videos, users, cfg, settings)
    favs = FavoriteService(db, videos, delivery, users, cfg, settings)
    U = 111

    # ---- service
    ok("add -> ok", await favs.add(U, 1) == "ok")
    ok("add again -> exists", await favs.add(U, 1) == "exists")
    ok("unknown video -> missing", await favs.add(U, 9999) == "missing")
    ok("name is optional (NULL)", (await favs.get(U, 1))["name"] is None)
    ok("set name", await favs.set_name(U, 1, "  Beach  ") and (await favs.get(U, 1))["name"] == "Beach")
    ok("name '-' / empty clears", await favs.set_name(U, 1, "") and (await favs.get(U, 1))["name"] is None)
    ok("name cut to 40", await favs.set_name(U, 1, "x" * 60) and len((await favs.get(U, 1))["name"]) == 40)
    ok("per-user: other user has none", await favs.count(222) == 0 and await favs.count(U) == 1)
    ok("other user can save the same video", await favs.add(222, 1) == "ok")
    ok("remove", await favs.remove(U, 1) and not await favs.remove(U, 1) and await favs.count(222) == 1)
    for v in range(1, 21):
        await favs.add(U, v)
    ok("list newest first + paging", [r["video_id"] for r in await favs.list(U, 8, 0)][0] == 20
       and len(await favs.list(U, 8, 16)) == 4)
    # ---- send again
    ok("send ok", await favs.send(U, 5, 3, ui.fav_saved_kb(3)) == "ok")
    ok("sent with its keyboard + protected", bot.sent[-1][1] == "f3" and bot.sent[-1][2] is not None and bot.sent[-1][3] is True)
    ok("auto-delete queued", await db.pool.fetchval("SELECT COUNT(*) FROM delivered_messages WHERE video_id=3") == 1)
    ok("send does NOT touch delivery history", await db.pool.fetchval("SELECT COUNT(*) FROM user_video_history") == 0)
    await db.pool.execute("UPDATE videos SET is_active=FALSE WHERE id=4")
    ok("inactive video -> missing", await favs.send(U, 5, 4) == "missing")
    await db.pool.execute("DELETE FROM videos WHERE id=2")
    ok("deleting a video removes the favorite (cascade)", not await favs.has(U, 2))
    for u in range(300, 300):
        pass

    # ---- normal delivery now carries the ⭐ button (private) and not in groups
    await delivery.deliver(777, 777, "new", 1)
    ok("delivered video has ⭐ Save button", bot.sent[-1][2].inline_keyboard[0][0].callback_data.startswith("fav:add:"))
    await delivery.deliver(778, -500, "new", 1, in_group=True)
    ok("group delivery has NO button", bot.sent[-1][2] is None)
    await settings.set("btn_favorites", "0")
    await delivery.deliver(779, 779, "new", 1)
    ok("admin switch hides the button", bot.sent[-1][2] is None)
    await settings.set("btn_favorites", "1")

    # ---- handlers
    cb = FakeCb(U, "fav:add:7")
    await favs.remove(U, 7)
    await fh.on_add(cb, favs, settings)
    ok("tap ⭐ saves + swaps keyboard", await favs.has(U, 7) and cb.message.markups[-1].inline_keyboard[0][0].callback_data == "fav:nm:7")
    cb = FakeCb(U, "fav:del:7")
    await fh.on_del(cb, favs, settings)
    ok("tap 💔 removes + restores ⭐", not await favs.has(U, 7) and cb.message.markups[-1].inline_keyboard[0][0].callback_data == "fav:add:7")
    cb = FakeCb(U, "fav:list:1")
    await fh.on_list(cb, favs, settings)
    txt, kb = cb.message.texts[-1]
    ok("list page 1 shows 8 items + nav", sum(1 for r in kb.inline_keyboard for b in r if b.callback_data.startswith("fav:v:")) == 8
       and any(b.callback_data == "fav:list:2" for r in kb.inline_keyboard for b in r))
    cb = FakeCb(U, "fav:nm:5:1")
    await fh.on_name(cb, favs, users, settings)
    state, data = await users.get_state(U)
    ok("name prompt sets state", state == "fav_name" and data["video_id"] == 5)
    ok("name filter matches only in that state", await fh.awaiting_fav_name(types.SimpleNamespace(from_user=types.SimpleNamespace(id=U)), users)
       and not await fh.awaiting_fav_name(types.SimpleNamespace(from_user=types.SimpleNamespace(id=999)), users))
    msg = FakeMsg(); msg.from_user = types.SimpleNamespace(id=U); msg.text = "Summer clip"
    await fh.on_name_input(msg, favs, users)
    ok("typed name is saved + state cleared", (await favs.get(U, 5))["name"] == "Summer clip" and (await users.get_state(U))[0] is None)
    ok("name shows in list label", ui.fav_label(await favs.get(U, 5)) == "Summer clip")
    await settings.set("btn_favorites", "0")
    cb = FakeCb(U, "fav:list:1")
    await fh.on_list(cb, favs, settings)
    ok("feature off -> popup, no list", cb.popups and cb.popups[0][1] is True and not cb.message.texts)
    await settings.set("btn_favorites", "1")

    # ---- quick buttons + admin
    ok("default numbers", settings.qty_numbers() == [1, 5, 10, 15, 20, 25, 30, 50])
    await settings.set("qty_buttons", "2,4,6")
    kb = ui.qty_kb(settings, cfg)
    ok("admin numbers used", [b.text for b in kb.inline_keyboard[0]] == ["⚡ 2", "⚡ 4", "⚡ 6"])
    cfg.max_videos_per_request = 5
    ok("numbers above the max are hidden", [b.text for b in ui.qty_kb(settings, cfg).inline_keyboard[0]] == ["⚡ 2", "⚡ 4"])
    await settings.reset("qty_buttons")
    ok("reset -> default", settings.qty_numbers()[-1] == 50)
    print("ALL FAVORITES TESTS PASSED")
    await db.close()

asyncio.run(main())
