# main.py — aiogram v3 + aiohttp + asyncpg (PostgreSQL)

import asyncio
import os
import sys
import platform
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# -------- env / paths ----------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_IDS = {int(x) for x in (os.getenv("ADMIN_IDS", "")).split(",") if x.strip().isdigit()}
BONUS_PER_REF = float(os.getenv("BONUS_PER_REF", "1.0"))
PAYOUT_TARGET = int(os.getenv("PAYOUT_TARGET", "600"))
SUB_CHANNELS_RAW = [ch.strip() for ch in os.getenv("SUB_CHANNELS", "").split(",") if ch.strip()]

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()  # postgresql://...sslmode=require

def _to_chat_id(val: str) -> int | str:
    if val.startswith("@"):
        return val
    try:
        return int(val)
    except ValueError:
        return val

SUB_CHANNELS = [_to_chat_id(v) for v in SUB_CHANNELS_RAW]

# -------- models ----------
@dataclass
class User:
    user_id: int
    username: str | None
    ref_by: int | None
    balance: float
    referrals_count: int
    joined_at: str

# -------- schema ----------
INIT_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    ref_by BIGINT,
    balance DOUBLE PRECISION DEFAULT 0,
    referrals_count INTEGER DEFAULT 0,
    joined_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS referrals (
    id BIGSERIAL PRIMARY KEY,
    referrer_id BIGINT NOT NULL,
    referred_id BIGINT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pending_refs (
    referred_id BIGINT PRIMARY KEY,
    referrer_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
"""

# -------- db pool ----------
_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL не задан (Render → Environment).")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(INIT_SQL)

# -------- db ops ----------
async def ensure_user(tg_user) -> tuple[bool, User]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        # upsert
        await conn.execute(
            """
            INSERT INTO users(user_id, username) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username
            """,
            tg_user.id, tg_user.username
        )
        row = await conn.fetchrow(
            "SELECT user_id, username, ref_by, balance, referrals_count, joined_at FROM users WHERE user_id=$1",
            tg_user.id
        )
    u = User(*row)
    try:
        joined = u.joined_at if isinstance(u.joined_at, dt.datetime) else dt.datetime.fromisoformat(str(u.joined_at))
        is_new = (dt.datetime.utcnow() - joined.replace(tzinfo=None)).total_seconds() < 30
    except Exception:
        is_new = False
    return is_new, u

async def apply_referral(referrer_id: int, referred_id: int) -> bool:
    if referrer_id == referred_id:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # гарантируем реферера
            await conn.execute(
                "INSERT INTO users(user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                referrer_id
            )
            # вставка события (уникальность по referred_id)
            try:
                await conn.execute(
                    "INSERT INTO referrals(referrer_id, referred_id) VALUES ($1, $2)",
                    referrer_id, referred_id
                )
            except asyncpg.UniqueViolationError:
                return False

            # ref_by у приглашённого — если ещё не привязан
            await conn.execute(
                "UPDATE users SET ref_by = COALESCE(ref_by, $1) WHERE user_id = $2",
                referrer_id, referred_id
            )
            # начисление рефереру
            await conn.execute(
                "UPDATE users SET referrals_count = referrals_count + 1, balance = balance + $1 WHERE user_id = $2",
                BONUS_PER_REF, referrer_id
            )
    return True

async def get_user(user_id: int) -> User | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, username, ref_by, balance, referrals_count, joined_at FROM users WHERE user_id=$1",
            user_id
        )
        return User(*row) if row else None

async def add_pending_ref(referred_id: int, referrer_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pending_refs(referred_id, referrer_id) VALUES ($1, $2) "
            "ON CONFLICT (referred_id) DO UPDATE SET referrer_id=EXCLUDED.referrer_id",
            referred_id, referrer_id
        )

async def pop_pending_ref(referred_id: int) -> int | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT referrer_id FROM pending_refs WHERE referred_id=$1", referred_id)
        if not row:
            return None
        await conn.execute("DELETE FROM pending_refs WHERE referred_id=$1", referred_id)
        return row["referrer_id"]

async def get_top10():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, username, referrals_count, balance "
            "FROM users ORDER BY referrals_count DESC, balance DESC LIMIT 10"
        )
        return rows

async def get_stats():
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_ref_events = await conn.fetchval("SELECT COUNT(*) FROM referrals")
        total_refs_by_sum = await conn.fetchval("SELECT COALESCE(SUM(referrals_count),0) FROM users")
        total_balance = await conn.fetchval("SELECT COALESCE(SUM(balance),0) FROM users")
        return total_users, total_ref_events, total_refs_by_sum, float(total_balance or 0)

# -------- bot helpers ----------
async def get_bot_username(bot: Bot) -> str:
    me = await bot.get_me()
    return me.username or ""

def profile_line(u: User) -> str:
    need = max(0, PAYOUT_TARGET - u.referrals_count)
    return (
        f"👤 Вы: <code>{u.user_id}</code> (@{u.username or '—'})\n"
        f"👥 Рефералов: <b>{u.referrals_count}</b>\n"
        f"💰 Баланс: <b>{u.balance:.2f}</b>\n"
        f"🎯 До цели {PAYOUT_TARGET}: <b>{need}</b>"
    )

async def is_member_of(bot: Bot, chat_id: int | str, user_id: int) -> bool:
    try:
        cm = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception:
        return False
    return getattr(cm, "status", None) in ("member", "administrator", "creator")

async def is_subscribed_everywhere(bot: Bot, user_id: int) -> bool:
    if not SUB_CHANNELS:
        return True
    for ch in SUB_CHANNELS:
        if not await is_member_of(bot, ch, user_id):
            return False
    return True

def sub_keyboard() -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for ch in SUB_CHANNELS_RAW:
        url = f"https://t.me/{ch[1:]}" if ch.startswith("@") else "https://t.me/"
        buttons.append([InlineKeyboardButton(text=f"Подписаться: {ch}", url=url)])
    buttons.append([InlineKeyboardButton(text="✅ Проверил подписку", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass

# -------- auto-check (15s) ----------
async def auto_check_after_delay(bot: Bot, user_id: int) -> None:
    await asyncio.sleep(15)
    if not await is_subscribed_everywhere(bot, user_id):
        return
    referrer_id = await pop_pending_ref(user_id)
    if referrer_id is None:
        return
    applied = await apply_referral(referrer_id, user_id)
    if applied:
        try:
            await bot.send_message(user_id, "✅ Подписка подтверждена автоматически, рефералка начислена!")
        except Exception:
            pass
        await notify_admins(
            bot,
            f"🎉 Реферал (автопроверка 15с):\nРеферер: <code>{referrer_id}</code>\nПриглашённый: <code>{user_id}</code>"
        )

# -------- aiohttp web (health) ----------
async def health(request: web.Request):
    return web.json_response({"ok": True})

async def run_web_app():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    port = int(os.environ.get("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[web] started on 0.0.0.0:{port}", flush=True)
    # держим задачу живой
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        print("[web] shutting down...", flush=True)
        raise

# -------- dispatcher / handlers ----------
dp = Dispatcher()

@dp.message(CommandStart())
async def on_start(message: Message, bot: Bot):
    payload = ""
    if message.text:
        rest = message.text.strip()
        if rest.startswith("/start"):
            payload = rest.replace("/start", "", 1).strip()

    is_new, u = await ensure_user(message.from_user)
    subscribed = await is_subscribed_everywhere(bot, u.user_id)
    ref_applied = False
    referrer_id: int | None = None

    if payload and payload.isdigit():
        referrer_id = int(payload)
        if referrer_id != u.user_id:
            if subscribed:
                ref_applied = await apply_referral(referrer_id, u.user_id)
            else:
                await add_pending_ref(u.user_id, referrer_id)

    bot_username = await get_bot_username(bot)
    link = f"https://t.me/{bot_username}?start={u.user_id}" if bot_username else "—"

    parts = ["👋 Добро пожаловать!"]
    if not subscribed and SUB_CHANNELS:
        parts += ["Чтобы пользоваться ботом и получить реферал-бонус — подпишись на каналы ниже:", ""]
    else:
        parts.append("Готово, ты можешь пользоваться ботом.")

    if ref_applied:
        parts.append("✅ Твоя рефералка засчитана!")
        if referrer_id is not None:
            await notify_admins(
                bot,
                f"🎉 Новый реферал!\nРеферер: <code>{referrer_id}</code>\nПриглашённый: <code>{u.user_id}</code>"
            )
    elif payload and payload.isdigit() and not subscribed and SUB_CHANNELS:
        parts.append("ℹ️ Рефералка будет засчитана после подписки и автопроверки/кнопки.")
    else:
        parts.append("ℹ️ Начисление по реф-ссылке происходит один раз при первом старте.")

    parts += ["", profile_line(u), "", f"🔗 Твоя реф-ссылка:\n<code>{link}</code>", "",
              "Команды:\n• /ref — моя ссылка и счёт\n• /me — личная статистика\n• /top — топ-10\n• /stats — общая статистика (для админов)\n• /check — проверить подписку"]

    text = "\n".join(parts)
    if not subscribed and SUB_CHANNELS:
        await message.answer(text, parse_mode="HTML", reply_markup=sub_keyboard())
    else:
        await message.answer(text, parse_mode="HTML")

    if not subscribed and SUB_CHANNELS:
        asyncio.create_task(auto_check_after_delay(bot, u.user_id))

@dp.message(Command("check"))
async def cmd_check(message: Message, bot: Bot):
    user_id = message.from_user.id
    subscribed = await is_subscribed_everywhere(bot, user_id)
    if subscribed:
        referrer_id = await pop_pending_ref(user_id)
        if referrer_id is not None:
            applied = await apply_referral(referrer_id, user_id)
            if applied:
                await message.answer("✅ Подписка подтверждена, рефералка начислена!")
                await notify_admins(
                    bot,
                    f"🎉 Реферал (после проверки):\nРеферер: <code>{referrer_id}</code>\nПриглашённый: <code>{user_id}</code>"
                )
            else:
                await message.answer("✅ Подписка подтверждена. Рефералка уже была начислена ранее.")
        else:
            await message.answer("✅ Подписка подтверждена.")
    else:
        await message.answer("❌ Пока не вижу подписки на все обязательные каналы. Подпишись и жми /check ещё раз.")

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id
    subscribed = await is_subscribed_everywhere(bot, user_id)
    if subscribed:
        referrer_id = await pop_pending_ref(user_id)
        if referrer_id is not None:
            applied = await apply_referral(referrer_id, user_id)
            if applied:
                await call.message.edit_text("✅ Подписка подтверждена, рефералка начислена!")
                await notify_admins(
                    bot,
                    f"🎉 Реферал (после кнопки):\nРеферер: <code>{referrer_id}</code>\nПриглашённый: <code>{user_id}</code>"
                )
            else:
                await call.message.edit_text("✅ Подписка подтверждена. Рефералка уже была начислена ранее.")
        else:
            await call.message.edit_text("✅ Подписка подтверждена. (Реферер не найден в ожидании)")
    else:
        await call.answer("Подписка не найдена. Проверь, что ты вступил(а) во все каналы.", show_alert=True)

@dp.message(Command("ref"))
async def cmd_ref(message: Message, bot: Bot):
    u = await get_user(message.from_user.id)
    if not u:
        is_new, u = await ensure_user(message.from_user)
    bot_username = await get_bot_username(bot)
    link = f"https://t.me/{bot_username}?start={u.user_id}" if bot_username else "—"
    await message.answer(f"{profile_line(u)}\n\n🔗 Твоя реф-ссылка:\n<code>{link}</code>", parse_mode="HTML")

@dp.message(Command("me"))
async def cmd_me(message: Message):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # ensure user
        await conn.execute(
            "INSERT INTO users(user_id, username) VALUES ($1,$2) ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username",
            message.from_user.id, message.from_user.username
        )
        row = await conn.fetchrow("SELECT user_id, username, ref_by, balance, referrals_count, joined_at FROM users WHERE user_id=$1", message.from_user.id)
        u = User(*row)
        rows = await conn.fetch(
            "SELECT referred_id, created_at FROM referrals WHERE referrer_id=$1 ORDER BY created_at DESC",
            u.user_id
        )
    last_lines = "\n".join([f"• <code>{r['referred_id']}</code> ({r['created_at']})" for r in rows[:10]]) if rows else "пока никого"
    await message.answer(f"{profile_line(u)}\n\nПоследние приглашённые:\n{last_lines}", parse_mode="HTML")

@dp.message(Command("top"))
async def cmd_top(message: Message):
    rows = await get_top10()
    if not rows:
        await message.answer("Пока нет данных 👀")
        return
    lines = []
    for i, r in enumerate(rows, start=1):
        uid, username, refs, bal = r["user_id"], r["username"], r["referrals_count"], r["balance"]
        uname = f"@{username}" if username else f"id:{uid}"
        lines.append(f"{i}. {uname} — 👥 {refs} | 💰 {bal:.2f}")
    await message.answer("🏆 Топ-10:\n" + "\n".join(lines))

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Эта команда только для админов.")
        return
    total_users, total_ref_events, total_refs_by_sum, total_balance = await get_stats()
    await message.answer(
        "📊 Общая статистика:\n"
        f"Пользователей: <b>{total_users}</b>\n"
        f"Реферал-событий (уникальных): <b>{total_ref_events}</b>\n"
        f"Сумма рефералов по пользователям: <b>{total_refs_by_sum}</b>\n"
        f"Начислено всего: <b>{total_balance:.2f}</b>",
        parse_mode="HTML",
    )

# -------- run ----------
async def main():
    print("[boot] python:", sys.version, flush=True)
    print("[boot] platform:", platform.platform(), flush=True)
    print("[boot] BASE_DIR:", BASE_DIR, flush=True)

    if not BOT_TOKEN:
        print("[boot] BOT_TOKEN is empty", flush=True)
        raise RuntimeError("BOT_TOKEN не задан")

    await init_db()  # создадим таблицы, если их ещё нет

    # стартуем веб (порт для Render)
    web_task = asyncio.create_task(run_web_app())

    bot = Bot(BOT_TOKEN)
    print("[boot] starting bot & web...", flush=True)

    await asyncio.gather(
        dp.start_polling(bot),
        web_task,
    )

if __name__ == "__main__":
    asyncio.run(main())
