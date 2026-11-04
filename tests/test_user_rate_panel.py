"""Admin panel: per-user rate limit page + inputs (REAL PostgreSQL, fake Telegram).
   DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_user_rate_panel.py"""
import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from database import Database
from handlers.admin_panel import Panel
from services.user_service import UserService


def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


class Ev:
    async def record(self, *a, **k):
        pass


class Msg:
    def __init__(self):
        self.out = []
        self.from_user = types.SimpleNamespace(id=1)

    async def answer(self, text, **kw):
        self.out.append(text)


async def main():
    db = Database(os.environ["DATABASE_URL"], 1, 5)
    await db.connect()
    users = UserService(db, 30)
    cfg = Config.__new__(Config)
    cfg.rate_limit_requests, cfg.rate_limit_window, cfg.admin_ids = 5, 60, [1]
    p = Panel(None, cfg, db, users, None, None, None, None, Ev())
    T = 777

    text, _ = await p.p_usr_rate(T)
    ok("page: default shown", "default" in text and "5 requests" in text)
    ok("page: via dispatcher", (await p.page(f"adm:usr:rate:{T}"))[0] == text)

    async def send(kind, value):
        await users.set_state(1, "admin_input", {"kind": kind, "back": "x", "arg": str(T)})
        m = Msg()
        await p.on_input(types.SimpleNamespace(text=value, from_user=m.from_user, answer=m.answer))
        return m.out[-1]

    out = await send("rlm", "10")
    ok("one window saved", users.rate_rule(T) == {"per_second": None, "per_minute": 10,
                                                  "per_hour": None, "per_day": None})
    ok("saved message", "saved" in out and "10" in out)
    await send("rls", "2")
    await send("rld", "500")
    ok("windows merge", users.rate_rule(T) == {"per_second": 2, "per_minute": 10,
                                               "per_hour": None, "per_day": 500})
    await send("rlm", "0")
    ok("0 = unlimited", users.rate_rule(T)["per_minute"] is None)
    out = await send("rlm", "abc")
    ok("bad value rejected, rule kept", "whole number" in out and users.rate_rule(T)["per_second"] == 2)
    out = await send("rla", "1 10 100 500")
    ok("set all 4", users.rate_rule(T) == {"per_second": 1, "per_minute": 10,
                                           "per_hour": 100, "per_day": 500})
    out = await send("rla", "1 2 3")
    ok("all-4 needs 4 numbers", "exactly 4" in out)
    await send("rla", "0, 5, -, 50")
    ok("all-4 with separators / unlimited", users.rate_rule(T) == {"per_second": None, "per_minute": 5,
                                                                   "per_hour": None, "per_day": 50})
    text, markup = await p.p_usr_rate(T)
    ok("page: custom shown", "custom" in text and "5" in text and "50" in text)
    ok("page: reset button", any("rlreset" in b.callback_data for r in markup.inline_keyboard for b in r))
    await users.clear_rate_rule(T)
    ok("reset", users.rate_rule(T) is None and "Default" in p._rate_line(T))
    print("ALL OK")
    await db.close()


asyncio.run(main())
