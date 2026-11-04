"""Per-user rate limits (second / minute / hour / day) against a REAL PostgreSQL.
   DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_user_rate_limits.py"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import Database
from services.user_service import UserService, fmt_wait, rate_rule_text


def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


async def main():
    db = Database(os.environ["DATABASE_URL"], 1, 5)
    await db.connect()
    await db.connect()          # schema is idempotent
    users = UserService(db, 30)
    G = (5, 60)                 # global default: 5 requests / 60 s

    # --- users without a rule keep the old global behaviour ---
    ok("default: first 5 allowed", all([await users.hit_rate_limit(1, *G) for _ in range(5)]))
    ok("default: 6th refused", not await users.hit_rate_limit(1, *G))
    ok("default: no hint text", users.rate_hint(1) == "")

    # --- per-second limit 2, others unlimited ---
    await users.set_rate_rule(10, {"per_second": 2})
    ok("sec: 1st", await users.hit_rate_limit(10, *G))
    ok("sec: 2nd", await users.hit_rate_limit(10, *G))
    ok("sec: 3rd refused", not await users.hit_rate_limit(10, *G))
    hint = users.rate_hint(10)
    ok("sec: hint names the limit", "2 per second" in hint and "Try again" in hint)
    await asyncio.sleep(1.1)
    ok("sec: allowed again after the window", await users.hit_rate_limit(10, *G))

    # --- each user is independent ---
    await users.set_rate_rule(11, {"per_minute": 3})
    ok("independent: user 11 allowed", all([await users.hit_rate_limit(11, *G) for _ in range(3)]))
    ok("independent: user 11 4th refused", not await users.hit_rate_limit(11, *G))
    ok("independent: user 10 unaffected", await users.hit_rate_limit(10, *G))
    ok("independent: user 12 (default) unaffected", await users.hit_rate_limit(12, *G))

    # --- several windows: the tightest one wins, refused requests use no quota ---
    await users.set_rate_rule(20, {"per_second": 100, "per_minute": 3, "per_hour": 5, "per_day": 6})
    ok("multi: 3 allowed", all([await users.hit_rate_limit(20, *G) for _ in range(3)]))
    ok("multi: minute full -> refused", not await users.hit_rate_limit(20, *G))
    ok("multi: hint says minute", "3 per minute" in users.rate_hint(20))
    for _ in range(5):          # hammering must NOT burn the hour / day quota
        await users.hit_rate_limit(20, *G)
    rows = {r["win"]: r["hits"] for r in
            await db.pool.fetch("SELECT win, hits FROM user_rate_counters WHERE user_id = 20")}
    ok("multi: refused requests rolled back", rows == {"s": 3, "m": 3, "h": 3, "d": 3})
    # expire the minute window only
    await db.pool.execute("UPDATE user_rate_counters SET window_start = now() - interval '2 minutes' "
                          "WHERE user_id = 20 AND win IN ('s','m')")
    ok("multi: minute reopened", all([await users.hit_rate_limit(20, *G) for _ in range(2)]))
    ok("multi: hour limit (5) now hit", not await users.hit_rate_limit(20, *G))
    ok("multi: hint says hour", "5 per hour" in users.rate_hint(20))

    # --- unlimited windows / rule without limits ---
    await users.set_rate_rule(30, {})
    ok("no limits: always allowed", all([await users.hit_rate_limit(30, *G) for _ in range(50)]))

    # --- persistence + reload (restart) ---
    users2 = UserService(db, 30)
    await users2.load_rate_rules()
    ok("reload: rules restored", users2.rate_rule(20) == {"per_second": 100, "per_minute": 3,
                                                          "per_hour": 5, "per_day": 6})
    ok("reload: text", rate_rule_text(users2.rate_rule(11)) == "3/min")
    ok("reload: counters survive a restart", not await users2.hit_rate_limit(20, *G))

    # --- concurrency: 20 parallel requests, limit 5 -> exactly 5 pass ---
    await users.set_rate_rule(40, {"per_minute": 5})
    res = await asyncio.gather(*(users.hit_rate_limit(40, *G) for _ in range(20)))
    ok("concurrent: exactly 5 allowed", sum(res) == 5)

    # --- admin changes a limit / goes back to default ---
    await users.set_rate_rule(40, {"per_minute": 8})
    ok("raise limit: 3 more allowed", all([await users.hit_rate_limit(40, *G) for _ in range(3)]))
    ok("raise limit: then refused", not await users.hit_rate_limit(40, *G))
    ok("clear: had a rule", await users.clear_rate_rule(40))
    ok("clear: second clear is a no-op", not await users.clear_rate_rule(40))
    ok("clear: back on the global limit", await users.hit_rate_limit(40, *G))
    ok("clear: counters removed",
       await db.pool.fetchval("SELECT COUNT(*) FROM user_rate_counters WHERE user_id = 40") == 0)

    ok("fmt_wait", [fmt_wait(0.2), fmt_wait(59.2), fmt_wait(190), fmt_wait(7500)]
       == ["1s", "1m", "3m 10s", "2h 5m"])
    print("ALL OK")
    await db.close()


asyncio.run(main())
