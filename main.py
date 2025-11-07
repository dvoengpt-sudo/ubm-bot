import asyncio
import os
import sys
import platform
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# -------- env / paths ----------
BASE_DIR = Path(__file__).resolve().parent
# локально можно держать .env; на Render используем Environment Variables
load_dotenv(BASE_DIR / ".env", override=True)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_IDS = {int(x) for x in (os.getenv("ADMIN_IDS", "")).split(",") if x.strip().isdigit()}
BONUS_PER_REF = float(os.getenv("BONUS_PER_REF", "1.0"))
PAYOUT_TARGET = int(os.getenv("PAYOUT_TARGET", "600"))
SUB_CHANNELS_RAW = [ch.strip() for ch in os.getenv("SUB_CHANNELS", "").split(",") if ch.strip()]
DB_PATH = str(BASE_DIR / "refbot.sqlite3")

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
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    ref_by INTEGER,
    balance REAL DEFAULT 0,
    referrals_count INTEGER DEFAULT 0,
    joined_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL UNIQUE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pending_refs (
    referred_id INTEGER PRIMARY KEY,
    referrer_id INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
"""

# -------- db helpers ----------
def get_db():
    return aiosqlite.connect(DB_PATH)

async def init_db():
    async with get_db() as db:
        await db.executescript(INIT_SQL)
        await db.commit()

async def ensure_user(db: aiosqlite.Connection, tg_user) -> tuple[bool, User]:
    await db.execute(
        "INSERT OR IGNORE INTO users(user_id, username) VALUES (?, ?)",
        (tg_user.id, tg_user.username),
    )
    await db.execute("UPDATE users SET username=? WHERE user_id=?", (tg_user.username, tg_user.id))
    await db.commit()

    cur = await db.execute(
        "SELECT user_id, username, ref_by, balance, referrals_count, joined_at FROM users WHERE user_id=?",
        (tg_user.id,),
    )
    row = await cur.fetchone()
    u = User(*row)
    try:
        joined = dt.datetime.fromisoformat(u.joined_at.replace(" ", "T"))
        is_new = (dt.datetime.utcnow() - joined).total_seconds() < 30
    except Exception:
        is_new = False
    return is_new, u

async def apply_referral(db: aiosqlite.Connection, referrer_id: int, referred_id: int) -> bool:
    if referrer_id == referred_id:
        return False

    cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (referrer_id,))
    if await cur.fetchone() is None:
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (referrer_id,))

    try:
        await db.execute(
            "INSERT INTO referrals(referrer_id, referred_id) VALUES (?, ?)",
            (referrer_id, referred_id),
        )
    except aiosqlite.IntegrityError:
        return False  # уже засчитан

    # ref_by у приглашённого — только если ещё пусто
    await db.execute(
        "UPDATE users SET ref_by = COALESCE(ref_by, ?) WHERE user_id = ?",
        (referrer_id, referred_id),
    )
    # инкремент рефереру
    await db.execute(
        "UPDATE users SET referrals_count = referrals_count + 1, balance = balance + ? WHERE user_id = ?",
        (BONUS_PER_REF, referrer_id),
    )
    await db.commit()
    return True

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
    return all([await is_member_of(bot, ch, user_id) for ch in SUB_CHANNELS])

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
    async with get_db() as db:
        cur = await db.execute("SELECT referrer_id FROM pending_refs WHERE referred_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return
        referrer_id = row[0]
        applied = await apply_referral(db, referrer_id, user_id)
        await db.execute("DELETE FROM pending_refs WHERE referred_id=?", (user_id,))
        await db.commit()
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

    async with get_db() as db:
        is_new, u = await ensure_user(db, message.from_user)
        subscribed = await is_subscribed_everywhere(bot, u.user_id)
        ref_applied = False
        referrer_id: int | None = None

        if payload and payload.isdigit():
            referrer_id = int(payload)
            if referrer_id != u.user_id:
                if subscribed:
                    ref_applied = await apply_referral(db, referrer_id, u.user_id)
                else:
                    await db.execute(
                        "INSERT OR REPLACE INTO pending_refs(referred_id, referrer_id) VALUES (?, ?)",
                        (u.user_id, referrer_id),
                    )
                    await db.commit()

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
    async with get_db() as db:
        if subscribed:
            cur = await db.execute("SELECT referrer_id FROM pending_refs WHERE referred_id=?", (user_id,))
            row = await cur.fetchone()
            if row:
                referrer_id = row[0]
                applied = await apply_referral(db, referrer_id, user_id)
                await db.execute("DELETE FROM pending_refs WHERE referred_id=?", (user_id,))
                await db.commit()
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
    async with get_db() as db:
        if subscribed:
            cur = await db.execute("SELECT referrer_id FROM pending_refs WHERE referred_id=?", (user_id,))
            row = await cur.fetchone()
            if row:
                referrer_id = row[0]
                applied = await apply_referral(db, referrer_id, user_id)
                await db.execute("DELETE FROM pending_refs WHERE referred_id=?", (user_id,))
                await db.commit()
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
    async with get_db() as db:
        _, u = await ensure_user(db, message.from_user)
        bot_username = await get_bot_username(bot)
        link = f"https://t.me/{bot_username}?start={u.user_id}" if bot_username else "—"
        await message.answer(f"{profile_line(u)}\n\n🔗 Твоя реф-ссылка:\n<code>{link}</code>", parse_mode="HTML")

@dp.message(Command("me"))
async def cmd_me(message: Message):
    async with get_db() as db:
        _, u = await ensure_user(db, message.from_user)
        cur = await db.execute(
            "SELECT referred_id, created_at FROM referrals WHERE referrer_id=? ORDER BY created_at DESC",
            (u.user_id,),
        )
        rows = await cur.fetchall()
        last_lines = "\n".join([f"• <code>{rid}</code> ({created_at})" for rid, created_at in rows[:10]]) if rows else "пока никого"
        await message.answer(f"{profile_line(u)}\n\nПоследние приглашённые:\n{last_lines}", parse_mode="HTML")

@dp.message(Command("top"))
async def cmd_top(message: Message):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, username, referrals_count, balance FROM users ORDER BY referrals_count DESC, balance DESC LIMIT 10"
        )
        rows = await cur.fetchall()
        if not rows:
            await message.answer("Пока нет данных 👀")
            return
        lines = []
        for i, (uid, username, refs, bal) in enumerate(rows, start=1):
            uname = f"@{username}" if username else f"id:{uid}"
            lines.append(f"{i}. {uname} — 👥 {refs} | 💰 {bal:.2f}")
        await message.answer("🏆 Топ-10:\n" + "\n".join(lines))

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Эта команда только для админов.")
        return
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*), COALESCE(SUM(referrals_count),0), COALESCE(SUM(balance),0) FROM users")
        total_users, total_refs_by_sum, total_balance = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM referrals")
        total_ref_events = (await cur.fetchone())[0]
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

    await init_db()

    if not BOT_TOKEN:
        print("[boot] BOT_TOKEN is empty. Check Render → Environment.", flush=True)
        raise RuntimeError("Не задан BOT_TOKEN в .env / переменных окружения")

    # запускаем веб сразу, чтобы Render видел порт
    web_task = asyncio.create_task(run_web_app())

    bot = Bot(BOT_TOKEN)
    print("[boot] starting bot & web...", flush=True)

    await asyncio.gather(
        dp.start_polling(bot),
        web_task,
    )

if __name__ == "__main__":
    asyncio.run(main())
