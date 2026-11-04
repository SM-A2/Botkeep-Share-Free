"""Per-user limits (60 s / 5 min / 1 h / 1 day + max videos per request) against a REAL PostgreSQL.
   DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_user_rate_limits.py"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from database import Database
from services.user_service import UserService, fmt_wait, rate_rule_text, RATE_WINDOWS


def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


R = lambda a=None, b=None, c=None, d=None: {"per_60s": a, "per_5m": b, "per_1h": c, "per_1d": d}


async def expire(db, uid, *wins):
    await db.pool.execute("UPDATE user_rate_counters SET window_start = now() - interval '2 days' "
                          "WHERE user_id = $1 AND win = ANY($2)", uid, list(wins))


async def main():
    db = Database(os.environ["DATABASE_URL"], 1, 5)
    await db.connect()
    await db.connect()          # schema is idempotent
    users = UserService(db, 30)
    G = (5, 60)                 # global default: 5 requests / 60 s
    ok("windows are 60s / 5m / 1h / 1d", [w[2] for w in RATE_WINDOWS] == [60, 300, 3600, 86400])

    # --- users without a rule keep the old global behaviour ---
    ok("default: first 5 allowed", all([await users.hit_rate_limit(1, *G) for _ in range(5)]))
    ok("default: 6th refused", not await users.hit_rate_limit(1, *G))
    ok("default: no hint text", users.rate_hint(1) == "")

    # --- 3 per 60 seconds ---
    await users.set_rate_rule(10, R(a=3))
    ok("60s: 3 allowed", all([await users.hit_rate_limit(10, *G) for _ in range(3)]))
    ok("60s: 4th refused", not await users.hit_rate_limit(10, *G))
    hint = users.rate_hint(10)
    ok("60s: hint names the limit", "3 per 60 seconds" in hint and "Try again in" in hint)
    await expire(db, 10, "60s")
    ok("60s: allowed again after the window", await users.hit_rate_limit(10, *G))

    # --- each user is independent ---
    await users.set_rate_rule(11, R(b=2))
    ok("independent: user 11 allowed", all([await users.hit_rate_limit(11, *G) for _ in range(2)]))
    ok("independent: user 11 3rd refused", not await users.hit_rate_limit(11, *G))
    ok("independent: user 10 unaffected", await users.hit_rate_limit(10, *G))
    ok("independent: user 12 (default) unaffected", await users.hit_rate_limit(12, *G))

    # --- several windows: the tightest wins, refused requests use no quota ---
    await users.set_rate_rule(20, R(100, 3, 5, 6))
    ok("multi: 3 allowed", all([await users.hit_rate_limit(20, *G) for _ in range(3)]))
    ok("multi: 5-minute window full -> refused", not await users.hit_rate_limit(20, *G))
    ok("multi: hint says 5 minutes", "3 per 5 minutes" in users.rate_hint(20))
    for _ in range(5):          # hammering must NOT burn the hour / day quota
        await users.hit_rate_limit(20, *G)
    rows = {r["win"]: r["hits"] for r in
            await db.pool.fetch("SELECT win, hits FROM user_rate_counters WHERE user_id = 20")}
    ok("multi: refused requests rolled back", rows == {"60s": 3, "5m": 3, "1h": 3, "1d": 3})
    await expire(db, 20, "60s", "5m")
    ok("multi: 5 minutes reopened", all([await users.hit_rate_limit(20, *G) for _ in range(2)]))
    ok("multi: hour limit (5) now hit", not await users.hit_rate_limit(20, *G))
    ok("multi: hint says 1 hour", "5 per 1 hour" in users.rate_hint(20))
    await expire(db, 20, "60s", "5m", "1h")
    ok("multi: 6th request of the day still fits", await users.hit_rate_limit(20, *G))
    ok("multi: day limit (6) hit after hour reopens", not await users.hit_rate_limit(20, *G))
    ok("multi: hint says 1 day", "6 per 1 day" in users.rate_hint(20))

    # --- unlimited ---
    await users.set_rate_rule(30, {})
    ok("no limits: always allowed", all([await users.hit_rate_limit(30, *G) for _ in range(50)]))

    # --- persistence + reload (restart) ---
    users2 = UserService(db, 30)
    await users2.load_rate_rules()
    ok("reload: rules restored", users2.rate_rule(20) == R(100, 3, 5, 6))
    ok("reload: text", rate_rule_text(users2.rate_rule(11)) == "2/5m")
    ok("reload: counters survive a restart", not await users2.hit_rate_limit(20, *G))

    # --- concurrency ---
    await users.set_rate_rule(40, R(b=5))
    res = await asyncio.gather(*(users.hit_rate_limit(40, *G) for _ in range(20)))
    ok("concurrent: exactly 5 allowed", sum(res) == 5)

    # --- admin changes a limit / goes back to default ---
    await users.set_rate_rule(40, R(b=8))
    ok("raise limit: 3 more allowed", all([await users.hit_rate_limit(40, *G) for _ in range(3)]))
    ok("raise limit: then refused", not await users.hit_rate_limit(40, *G))
    ok("clear: had a rule", await users.clear_rate_rule(40))
    ok("clear: second clear is a no-op", not await users.clear_rate_rule(40))
    ok("clear: back on the global limit", await users.hit_rate_limit(40, *G))
    ok("clear: counters removed",
       await db.pool.fetchval("SELECT COUNT(*) FROM user_rate_counters WHERE user_id = 40") == 0)
    ok("fmt_wait", [fmt_wait(0.2), fmt_wait(59.2), fmt_wait(190), fmt_wait(7500)]
       == ["1s", "1m", "3m 10s", "2h 5m"])

    # --- max videos per request, per user ---
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    from config import load_config
    cfg = load_config()                       # the REAL Config dataclass
    cfg.max_videos_per_request, cfg.group_max_videos = 50, 10
    ok("maxvid: default user gets the same cfg object", users.cfg_for(cfg, 50) is cfg)
    ok("maxvid: user id None", users.cfg_for(cfg, None) is cfg)
    await users.set_max_videos(50, 7)
    c50 = users.cfg_for(cfg, 50)
    ok("maxvid: personal max", c50.max_videos_per_request == 7 and c50.group_max_videos == 7)
    ok("maxvid: global cfg untouched", cfg.max_videos_per_request == 50 and cfg.group_max_videos == 10)
    ok("maxvid: other users unaffected", users.cfg_for(cfg, 51) is cfg)
    await users.set_max_videos(52, 120)
    c52 = users.cfg_for(cfg, 52)
    ok("maxvid: higher than global allowed, group stays capped",
       c52.max_videos_per_request == 120 and c52.group_max_videos == 10)
    users3 = UserService(db, 30)
    await users3.load_max_videos()
    ok("maxvid: survives restart", users3.max_videos_rule(50) == 7 and users3.max_videos_rule(52) == 120)
    ok("maxvid: clear", await users.clear_max_videos(50) and users.cfg_for(cfg, 50) is cfg)
    ok("maxvid: independent of rate rule", users.rate_rule(50) is None)
    print("ALL OK")
    await db.close()


asyncio.run(main())
