"""Personal max videos per request through the real private-chat handler (REAL PostgreSQL).
   DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_user_max_videos_handler.py"""
import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:abc")
from config import load_config
from database import Database
from handlers import user as uh
from services.settings_service import SettingsService
from services.user_service import UserService


def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


class Msg:
    def __init__(self, uid, text):
        self.text, self.out = text, []
        self.from_user = types.SimpleNamespace(id=uid, username="u", first_name="U", is_bot=False)
        self.chat = types.SimpleNamespace(id=uid, type="private")

    async def answer(self, text, **kw):
        self.out.append(text)


async def main():
    db = Database(os.environ["DATABASE_URL"], 1, 5)
    await db.connect()
    cfg = load_config()
    cfg.max_videos_per_request = 50
    users, settings = UserService(db, 30), SettingsService(db, cfg)
    await settings.load()
    calls = []

    async def fake_run(bot, users, delivery, videos, cfg, **kw):
        calls.append((kw["user_id"], kw["count"], cfg.max_videos_per_request))
    uh.run_request = fake_run

    async def send(uid, text):
        m = Msg(uid, text)
        await users.set_state(uid, "waiting_for_video_count", {"category": "new"})
        await uh.on_number(m, None, users, cfg, None, None, settings)
        return m

    await users.set_max_videos(900, 3)
    m = await send(900, "5")
    ok("limited user: 5 refused, message shows 1-3", not calls and "<code>3</code>" in m.out[0])
    await send(900, "3")
    ok("limited user: 3 accepted", calls == [(900, 3, 3)])
    m = await send(901, "30")
    ok("normal user: 30 accepted (bot-wide max 50)", calls[-1] == (901, 30, 50))
    m = await send(901, "51")
    ok("normal user: 51 refused", len(calls) == 2 and "<code>50</code>" in m.out[0])
    await users.set_max_videos(902, 120)
    await send(902, "100")
    ok("raised user: 100 accepted", calls[-1] == (902, 100, 120))
    await users.clear_max_videos(900)
    await send(900, "20")
    ok("after reset the bot-wide max applies", calls[-1] == (900, 20, 50))
    print("ALL OK")
    await db.close()


asyncio.run(main())
