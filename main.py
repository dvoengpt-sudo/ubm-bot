import asyncio
import os
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import aiosqlite
from dotenv import load_dotenv


# ====== БАЗОВЫЕ НАСТРОЙКИ / .env ======
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)  # грузим .env из папки скрипта

def _parse_list_env(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]

def _parse_int_set_env(value: str | None) -> set[int]:
    result: set[int] = set()
    if not value:
        return result
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            # пропускаем некорректные id
            continue
    return result


# ЧИТАЕМ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env")

DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "refbot.sqlite3"))
ADMIN_IDS: set[int] = _parse_int_set_env(os.getenv("ADMIN_IDS"))
BONUS_PER_REF = float(os.getenv("BONUS_PER_REF", "1.0"))
PAYOUT_TARGET = int(os.getenv("PAYOUT_TARGET", "10"))

# Каналы/группы для обязательной подписки:
# Пример: SUB_CHANNELS=@your_public_channel,-1001234567890
SUB_CHANNELS_RAW: list[str] = _parse_list_env(os.getenv("SUB_CHANNELS"))

# То же, но приведённое к типам для get_chat_member:
# публичные оставляем как строки "@name", числовые id приводим к int
SUB_CHANNELS: list[int | str] = []
for ch in SUB_CHANNELS_RAW:
    if ch.startswith("@"):
        SUB_CHANNELS.append(ch)
    else:
        try:
            SUB_CHANNELS.append(int(ch))
        except ValueError:
            # некорректный элемент пропускаем
            continue


# ====== МОДЕЛИ ======
@dataclass
class User:
    user_id: int
    username: str | None
    ref_by: int | None
    balance: float
    referrals_count: int
    joined_at: str


# ====== SQL ======
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

-- Ожидающие рефералы (когда нет подписки)
CREATE TABLE IF NOT EXISTS pending_refs (
    referred_id INTEGER PRIMARY KEY,
    referrer_id INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
"""


def get_db() -> aiosqlite.Connection:
    # aiosqlite.connect поддерживает async with
    return aiosqlite.connect(DB_PATH)


async def init_db() -> None:
    async with get_db() as db:
        await db.executescript(INIT_SQL)
        await db.commit()


# ====== УТИЛИТЫ ======
async def ensure_user(db: aiosqlite.Connection, tg_user) -> tuple[bool, User]:
    await db.execute(
        "INSERT OR IGNORE INTO users(user_id, username) VALUES (?, ?)",
        (tg_user.id, tg_user.username),
    )
    await db.execute(
        "UPDATE users SET username=? WHERE user_id=?",
        (tg_user.username, tg_user.id),
    )
    await db.commit()

    cur = await db.execute(
        "SELECT user_id, username, ref_by, balance, referrals_count, joined_at "
        "FROM users WHERE user_id=?",
        (tg_user.id,),
    )
    row = await cur.fetchone()
    u = User(*row)
    try:
        # SQLite CURRENT_TIMESTAMP = 'YYYY-MM-DD HH:MM:SS'
        joined = dt.datetime.fromisoformat(u.joined_at.replace(" ", "T"))
        is_new = (dt.datetime.utcnow() - joined).total_seconds() < 30
    except Exception:
        is_new = False
    return is_new, u


async def apply_referral(db: aiosqlite.Connection, referrer_id: int, referred_id: int) -> bool:
    """
    Начисляет реферальное событие:
      - создаёт запись в referrals (уникальна по referred_id)
      - +1 реферал и +BONUS_PER_REF на балансе у РЕФЕРЕРА
      - проставляет ref_by у ПРИГЛАШЁННОГО (если ещё не стоял)
    """
    if referrer_id == referred_id:
        return False

    # убедимся, что обе стороны существуют в users
    await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (referrer_id,))
    await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (referred_id,))

    try:
        await db.execute(
            "INSERT INTO referrals(referrer_id, referred_id) VALUES (?, ?)",
            (referrer_id, referred_id),
        )
    except aiosqlite.IntegrityError:
        # Уже засчитан ранее (unique по referred_id)
        return False

    # +статистика рефереру
    await db.execute(
        "UPDATE users SET referrals_count = referrals_count + 1, balance = balance + ? "
        "WHERE user_id = ?",
        (BONUS_PER_REF, referrer_id),
    )
    # отметим, кто пригласил, у приглашённого (если ещё пусто)
    await db.execute(
        "UPDATE users SET ref_by = COALESCE(ref_by, ?) WHERE user_id = ?",
        (referrer_id, referred_id),
    )
    await db.commit()
    return True


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


# --- Проверка подписки ---
async def is_member_of(bot: Bot, chat_id: int | str, user_id: int) -> bool:
    """
    True если юзер состоит в канале/группе.
    Для приватных каналов бот должен быть админом. Для публичных — хотя бы членом.
    """
    try:
        cm = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception:
        return False

    status = getattr(cm, "status", None)
    # Aiogram v3 возвращает ChatMember с полем status ('member', 'administrator', 'creator', 'left', 'kicked')
    return status in ("member", "administrator", "creator")


async def is_subscribed_everywhere(bot: Bot, user_id: int) -> bool:
    if not SUB_CHANNELS:
        return True
    results: list[bool] = []
    for ch in SUB_CHANNELS:
        ok = await is_member_of(bot, ch, user_id)
        results.append(ok)
    return all(results)


def sub_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура с кнопками подписки (по сырой строке), плюс кнопка "Проверил".
    Для '@public' даём прямую ссылку, для приватных/числовых — заглушка на t.me.
    """
    buttons: list[list[InlineKeyboardButton]] = []
    for ch in SUB_CHANNELS_RAW:
        if ch.startswith("@"):
            url = f"https://t.me/{ch[1:]}"
        else:
            url = "https://t.me/"
        buttons.append([InlineKeyboardButton(text=f"Подписаться: {ch}", url=url)])
    buttons.append([InlineKeyboardButton(text="✅ Проверил подписку", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ====== ХЭНДЛЕРЫ ======
dp = Dispatcher()


@dp.message(CommandStart())
async def on_start(message: Message, bot: Bot):
    # Разбор payload: /start <payload>
    payload = ""
    if message.text:
        rest = message.text.strip()
        if rest.startswith("/start"):
            payload = rest.replace("/start", "", 1).strip()

    async with get_db() as db:
        is_new, u = await ensure_user(db, message.from_user)

        # Сначала проверим подписку
        subscribed = await is_subscribed_everywhere(bot, u.user_id)

        # Если есть payload — сохраним потенциального реферера в pending, пока нет подписки
        ref_applied = False
        if payload and payload.isdigit():
            referrer_id = int(payload)
            if referrer_id == u.user_id:
                ref_applied = False  # самореферал не засчитываем
            elif subscribed:
                ref_applied = await apply_referral(db, referrer_id, u.user_id)
            else:
                await db.execute(
                    "INSERT OR REPLACE INTO pending_refs(referred_id, referrer_id) VALUES (?, ?)",
                    (u.user_id, referrer_id),
                )
                await db.commit()

        bot_username = await get_bot_username(bot)
        link = f"https://t.me/{bot_username}?start={u.user_id}" if bot_username else "—"

        parts: list[str] = ["👋 Добро пожаловать!"]
        if not subscribed and SUB_CHANNELS:
            parts += [
                "Чтобы пользоваться ботом и получить реферал-бонус — подпишись на каналы ниже:",
                "",
            ]
        else:
            parts.append("Готово, ты можешь пользоваться ботом.")

        if ref_applied:
            parts.append("✅ Твоя рефералка засчитана!")
        elif payload and payload.isdigit() and not subscribed and SUB_CHANNELS:
            parts.append("ℹ️ Рефералка будет засчитана после подписки и нажатия «Проверил подписку».")
        else:
            parts.append("ℹ️ Начисление по реф-ссылке происходит один раз при первом старте.")

        parts += [
            "",
            profile_line(u),
            "",
            f"🔗 Твоя реф-ссылка:\n<code>{link}</code>",
            "",
            "Команды:\n"
            "• /ref — моя ссылка и счёт\n"
            "• /me — личная статистика\n"
            "• /top — топ-10\n"
            "• /stats — общая статистика (для админов)\n"
            "• /check — проверить подписку",
        ]

        text = "\n".join(parts)
        if not subscribed and SUB_CHANNELS:
            await message.answer(text, parse_mode="HTML", reply_markup=sub_keyboard())
        else:
            await message.answer(text, parse_mode="HTML")


@dp.message(Command("check"))
async def cmd_check(message: Message, bot: Bot):
    user_id = message.from_user.id
    subscribed = await is_subscribed_everywhere(bot, user_id)
    async with get_db() as db:
        if subscribed:
            # если была в pending — начислим
            cur = await db.execute("SELECT referrer_id FROM pending_refs WHERE referred_id=?", (user_id,))
            row = await cur.fetchone()
            if row:
                referrer_id = row[0]
                applied = await apply_referral(db, referrer_id, user_id)
                await db.execute("DELETE FROM pending_refs WHERE referred_id=?", (user_id,))
                await db.commit()
                if applied:
                    await message.answer("✅ Подписка подтверждена, рефералка начислена!")
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
        await message.answer(
            f"{profile_line(u)}\n\n🔗 Твоя реф-ссылка:\n<code>{link}</code>",
            parse_mode="HTML",
        )


@dp.message(Command("me"))
async def cmd_me(message: Message):
    async with get_db() as db:
        _, u = await ensure_user(db, message.from_user)
        cur = await db.execute(
            "SELECT referred_id, created_at FROM referrals WHERE referrer_id=? ORDER BY created_at DESC",
            (u.user_id,),
        )
        rows = await cur.fetchall()
        if rows:
            last_lines = "\n".join([f"• <code>{rid}</code> ({created_at})" for rid, created_at in rows[:10]])
        else:
            last_lines = "пока никого"
        await message.answer(
            f"{profile_line(u)}\n\nПоследние приглашённые:\n{last_lines}",
            parse_mode="HTML",
        )


@dp.message(Command("top"))
async def cmd_top(message: Message):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, username, referrals_count, balance "
            "FROM users ORDER BY referrals_count DESC, balance DESC LIMIT 10"
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


# ====== ЗАПУСК ======
async def main():
    await init_db()
    bot = Bot(BOT_TOKEN)
    print("Bot started.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
