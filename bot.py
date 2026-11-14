"""
bot.py — бот предложки новостного канала (aiogram 3, один файл, SQLite)

Сценарии:
  • Пост = $500 в казну: текст → скриншот чека → топик в чате админов
  • На каждую заявку создаётся форум-топик: пост и чек закрепляются,
    внизу — сообщение управления (Опубликовать / Заблокировать)
  • Ответы админов в топике приходят пользователю от имени бота
    (сообщения с "!" остаются внутренними)
  • Восстановление: альбом доказательств из старого бота одним топиком
  • Набор в персонал: /recruit on — рассылка всем + кнопка Google Docs,
    анкета-визард на 4 вопроса прямо в боте
  • /ids (только владелец): ID чата, топика и премиум-эмодзи

Роли: системный владелец и системный админ — константы ниже;
сисадмин/владелец выдают и снимают админов, блокируют пользователей.

Подготовка:
  1) чат предложки = супергруппа с включёнными ТОПИКАМИ;
     бот — админ с правами: Manage Topics + Pin Messages
  2) новостной канал: бот — админ с правом Post Messages
  3) pip install aiogram
  4) заполнить блок НАСТРОЙКИ и:  python bot.py

  Точный ID чата/топика покажет команда /ids с аккаунта владельца —
  прямо внутри нужного чата. При каждом старте бот сам проверит
  оба чата и напишет в лог, что он видит и какие у него права.
  ВАЖНО: если топики включали в обычной группе — Telegram превратил
  её в супергруппу, и её ID СМЕНИЛСЯ на -100xxxxxxxxxx.

  При старте бот печатает абсолютный путь запущенного файла и все
  загруженные константы — так сразу видно, тот ли bot.py запущен
  и не остались ли значения-заглушки. Пока OWNER_ID — заглушка,
  команда /ids работает для всех (режим первичной настройки).
"""

import asyncio
import logging
import os
import sqlite3
import sys
from datetime import datetime

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    ReactionTypeEmoji,
    User,
)
from aiogram.types.inline_keyboard_button import InlineKeyboardButton as Btn

# ═════════════════════════ НАСТРОЙКИ ═════════════════════════
BOT_TOKEN      = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER")
OWNER_ID       = int(os.getenv("OWNER_ID", "111111111"))         # системный владелец
SYSADMIN_ID    = int(os.getenv("SYSADMIN_ID", "222222222"))      # системный администратор
CHANNEL_ID     = int(os.getenv("CHANNEL_ID", "-1001234567890"))  # новостной канал (бот = админ)
ADMIN_CHAT_ID  = int(os.getenv("ADMIN_CHAT_ID", "-1001234567890"))  # чат предложки с топиками
GOOGLE_DOC_URL = os.getenv("GOOGLE_DOC_URL", "https://docs.google.com/document/d/ВСТАВЬ_ID/edit")
POST_PRICE     = int(os.getenv("POST_PRICE", "500"))             # $ за пост в казну
# На bothost.ru задан DATA_DIR=/app/data — база ляжет туда и переживёт
# «Обновить из Git». Локально DATA_DIR нет — база будет рядом с bot.py
DATA_DIR       = os.getenv("DATA_DIR", ".")
DB_PATH        = os.getenv("DB_PATH", os.path.join(DATA_DIR, "suggest_bot.db"))
BOT_BUILD      = "v5-clean-emoji"  # метка сборки: видна в логе при старте

# ═══════════════ ПРЕМИУМ-ЭМОДЗИ ═══════════════
# ID эмодзи берите командой /ids: отправьте боту премиум-эмодзи —
# он вернёт их custom_emoji_id. Пустая строка = фича выключена,
# всё продолжит работать обычным текстом.
EMOJI_MENU     = ""   # главное меню /start
EMOJI_POST     = ""   # «пришлите текст поста»
EMOJI_CHECK    = ""   # «жду скриншот чека $500»
EMOJI_DONE     = ""   # финальное «готово!» после заявки
EMOJI_RECRUIT  = ""   # рассылка «объявлен набор в персонал»
# Иконки форум-топиков заявок (Bot API 9.0+; если API не примет — просто пропустится)
TOPIC_ICON_POST    = ""   # иконка топика «Пост»
TOPIC_ICON_RESTORE = ""   # иконка топика «Восстановление»
TOPIC_ICON_APPLY   = ""   # иконка топика «Анкета»

# Значения-заглушки: нужны, чтобы отличить «забыли заполнить» от реального ID
_PLACEHOLDERS = {
    "OWNER_ID": 111111111,
    "SYSADMIN_ID": 222222222,
    "CHANNEL_ID": -1001234567890,
    "ADMIN_CHAT_ID": -1001234567890,
}
# ══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("suggest")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    user_id   INTEGER PRIMARY KEY,
    username  TEXT,
    full_name TEXT,
    added_at  TEXT
);
CREATE TABLE IF NOT EXISTS admins(
    user_id  INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS blocked(
    user_id    INTEGER PRIMARY KEY,
    blocked_by INTEGER,
    reason     TEXT,
    blocked_at TEXT
);
CREATE TABLE IF NOT EXISTS topics(
    thread_id   INTEGER PRIMARY KEY,
    user_id     INTEGER,
    kind        TEXT,
    title       TEXT,
    status      TEXT DEFAULT 'open',
    post_msg_id INTEGER,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS settings(
    key   TEXT PRIMARY KEY,
    value TEXT
);
""")
db.commit()

now = lambda: datetime.now().isoformat(timespec="seconds")


def db_user_upsert(u: User) -> None:
    db.execute(
        "INSERT INTO users(user_id, username, full_name, added_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
        "full_name=excluded.full_name",
        (u.id, u.username or "", u.full_name, now()),
    )
    db.commit()


def db_all_users() -> list[int]:
    return [r["user_id"] for r in db.execute("SELECT user_id FROM users")]


def db_admin_add(uid: int, by: int) -> None:
    db.execute(
        "INSERT OR REPLACE INTO admins(user_id, added_by, added_at) VALUES(?,?,?)",
        (uid, by, now()),
    )
    db.commit()


def db_admin_del(uid: int) -> None:
    db.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    db.commit()


def db_admins() -> list[int]:
    return [r["user_id"] for r in db.execute("SELECT user_id FROM admins")]


def db_block(uid: int, by: int, reason: str = "") -> None:
    db.execute(
        "INSERT OR REPLACE INTO blocked(user_id, blocked_by, reason, blocked_at) "
        "VALUES(?,?,?,?)",
        (uid, by, reason, now()),
    )
    db.commit()


def db_unblock(uid: int) -> None:
    db.execute("DELETE FROM blocked WHERE user_id=?", (uid,))
    db.commit()


def db_blocked() -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM blocked ORDER BY blocked_at DESC").fetchall()


def db_is_blocked(uid: int) -> bool:
    return db.execute("SELECT 1 FROM blocked WHERE user_id=?", (uid,)).fetchone() is not None


def db_topic_save(thread_id: int, user_id: int, kind: str, title: str,
                  post_msg_id: int | None = None) -> None:
    db.execute(
        "INSERT OR REPLACE INTO topics(thread_id, user_id, kind, title, status, "
        "post_msg_id, created_at) VALUES(?,?,?,?, 'open', ?, ?)",
        (thread_id, user_id, kind, title, post_msg_id, now()),
    )
    db.commit()


def db_topic(thread_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM topics WHERE thread_id=?", (thread_id,)).fetchone()


def db_topic_status(thread_id: int, status: str) -> None:
    db.execute("UPDATE topics SET status=? WHERE thread_id=?", (status, thread_id))
    db.commit()


def get_setting(key: str, default: str = "0") -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, value))
    db.commit()

# ───────────────────────── РОЛИ ─────────────────────────

def is_staff(uid: int) -> bool:
    """Владелец, сисадмин или делегированный админ."""
    return uid in (OWNER_ID, SYSADMIN_ID) or uid in db_admins()


def can_manage(uid: int) -> bool:
    """Назначать/снимать админов, блокировать: владелец и сисадмин."""
    return uid in (OWNER_ID, SYSADMIN_ID)


# ───────────────────────── FSM ─────────────────────────

class PostSG(StatesGroup):
    text = State()     # ждём текст поста
    check = State()    # ждём скриншот чека $500


class RestoreSG(StatesGroup):
    collecting = State()  # собираем доказательства из старого бота


class ApplySG(StatesGroup):
    q = State()        # анкета сотрудника


class IdsSG(StatesGroup):
    emoji = State()    # ловим премиум-эмодзи после /ids


APPLY_QUESTIONS = [
    "Как вас зовут и сколько вам лет?",
    "Город и часовой пояс?",
    "Был ли опыт в администрировании каналов / предложек? Расскажите.",
    "Сколько часов в день готовы уделять предложке и почему именно вы?",
]

# ───────────────────────── КЛАВИАТУРЫ ─────────────────────────

def kb_menu() -> InlineKeyboardMarkup:
    rows = [
        [Btn(text=f"Предложить пост · ${POST_PRICE}", callback_data="flow:post")],
        [Btn(text="Восстановление (старый бот)", callback_data="flow:restore")],
    ]
    if get_setting("recruiting") == "1":
        rows.append([Btn(text="Подать анкету в персонал", callback_data="flow:apply")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[Btn(text="Отмена", callback_data="flow:cancel")]]
    )


def kb_ready() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="Готово — передать администрации", callback_data="flow:ready")],
        [Btn(text="Отмена", callback_data="flow:cancel")],
    ])


def kb_manage_post(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="Опубликовать в канал", callback_data=f"mng:pub:{tid}")],
        [Btn(text="Заблокировать автора", callback_data=f"mng:ban:{tid}")],
    ])


def kb_manage_restore(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="Закрыть обращение", callback_data=f"mng:close:{tid}")],
        [Btn(text="Заблокировать автора", callback_data=f"mng:ban:{tid}")],
    ])


def kb_manage_apply(tid: int, uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="Назначить администратором", callback_data=f"mng:mkadm:{tid}:{uid}")],
        [Btn(text="Закрыть анкету", callback_data=f"mng:close:{tid}")],
    ])

# ───────────────────────── ХЕЛПЕРЫ ─────────────────────────

def head_html(user: User, kind: str) -> str:
    uname = f"@{user.username} · " if user.username else ""
    return (
        f"Новая заявка · <b>{kind}</b>\n"
        f"<a href=\"tg://user?id={user.id}\">{html.quote(user.full_name)}</a>"
        f" · {uname}<code>{user.id}</code>"
    )


async def open_topic(user: User, kind: str) -> tuple[int, str]:
    title = f"{kind} | {user.full_name[:24]} | {user.id}"[:128]
    topic = await bot.create_forum_topic(ADMIN_CHAT_ID, name=title)
    icon = {"Пост": TOPIC_ICON_POST,
            "Восстановление": TOPIC_ICON_RESTORE,
            "Анкета": TOPIC_ICON_APPLY}.get(kind, "")
    if icon:
        res = await raw_api("editForumTopic", {
            "chat_id": ADMIN_CHAT_ID,
            "message_thread_id": topic.message_thread_id,
            "icon_custom_emoji_id": icon,
        })
        if not res.get("ok"):
            log.info("Иконка топика не установилась (%s) — едем без неё",
                     res.get("description"))
    return topic.message_thread_id, title


async def try_open_topic(user: User, kind: str) -> tuple[int, str] | None:
    """open_topic с человекочитаемой диагностикой вместо падения."""
    try:
        return await open_topic(user, kind)
    except TelegramBadRequest as e:
        log.error(
            "create_forum_topic: %s — проверьте ADMIN_CHAT_ID=%r. "
            "Точный ID покажет /ids с аккаунта владельца прямо в чате; "
            "чат должен быть супергруппой с топиками, бот — админом",
            e.message, ADMIN_CHAT_ID,
        )
        return None


async def raw_api(method: str, payload: dict) -> dict:
    """Прямой вызов Bot API — для параметров, которых нет в старом aiogram."""
    import aiohttp
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload) as r:
                return await r.json()
    except Exception as e:
        log.warning("raw_api %s: %s", method, e)
        return {"ok": False, "description": str(e)}


async def safe_pin(chat_id: int, message_id: int, thread_id: int) -> None:
    """Закреп внутри топика. На старом aiogram (<3.7), который не знает
    message_thread_id, уходим в Bot API напрямую — aiohttp и так с нами."""
    try:
        await bot.pin_chat_message(
            chat_id, message_id,
            disable_notification=True, message_thread_id=thread_id,
        )
        return
    except TypeError:
        pass  # старый aiogram — fallback ниже
    except TelegramBadRequest as e:
        log.warning("Не удалось закрепить: %s", e.message)
        return
    data = await raw_api("pinChatMessage", {
        "chat_id": chat_id,
        "message_id": message_id,
        "message_thread_id": thread_id,
        "disable_notification": True,
    })
    if data.get("ok"):
        log.info("pin через прямой Bot API ок (message %s, топик %s)",
                 message_id, thread_id)
    else:
        log.warning("pinChatMessage API: %s", data.get("description"))


# ── Антиспам: чистим чат за мастером заявки ──

async def sweep(chat_id: int, ids: list) -> None:
    """Молча удаляет сообщения (старше 48 ч API не даст — просто пропустим)."""
    for mid in set(x for x in ids if x):
        try:
            await bot.delete_message(chat_id, mid)
        except TelegramBadRequest:
            pass


# ── Премиум-эмодзи в сообщениях бота ──

async def send_fx(chat_id: int, emoji_id: str, text: str, **kw) -> Message:
    """Сообщение с премиум-эмодзи в начале. Нет ID — обычный текст."""
    if emoji_id:
        ent = MessageEntity(type="custom_emoji", offset=0, length=1,
                            custom_emoji_id=emoji_id)
        return await bot.send_message(chat_id, f"⚡ {text}",
                                      entities=[ent], **kw)
    return await bot.send_message(chat_id, text, **kw)


async def finalize_topic(row: sqlite3.Row, mark: str, status: str,
                         admin: User, verb: str, user_note: str) -> None:
    """Переименовать → закрыть топик, обновить статус, уведомить автора."""
    tid = row["thread_id"]
    try:
        await bot.edit_forum_topic(ADMIN_CHAT_ID, tid, name=f"{mark} {row['title']}"[:128])
    except TelegramBadRequest as e:
        log.warning("edit_forum_topic: %s", e.message)
    try:
        await bot.close_forum_topic(ADMIN_CHAT_ID, tid)
    except TelegramBadRequest as e:
        log.warning("close_forum_topic: %s", e.message)
    db_topic_status(tid, status)
    try:
        await send_fx(row["user_id"], EMOJI_DONE, user_note)
    except TelegramForbiddenError:
        log.info("Пользователь %s недоступен для уведомления", row["user_id"])
    log.info("Заявка #%s: %s (%s)", tid, verb, html.quote(admin.full_name))

# ───────────────────────── MIDDLEWARE: ИГНОР ЗАБЛОКИРОВАННЫХ ─────────────────────────
from aiogram import BaseMiddleware
from typing import Any, Awaitable, Callable

class BlockMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict], Awaitable[Any]],
        event: Any,
        data: dict,
    ) -> Any:
        user = data.get("event_from_user")
        if user and db_is_blocked(user.id) and not can_manage(user.id):
            log.info("Игнор сообщения от заблокированного %s", user.id)
            return  # бот молча не принимает сообщения
        return await handler(event, data)


dp.message.middleware(BlockMiddleware())
dp.callback_query.middleware(BlockMiddleware())

# ═════════════════════ ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ ═════════════════════

@dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(m: Message, state: FSMContext) -> None:
    db_user_upsert(m.from_user)
    await state.clear()
    await sweep(m.chat.id, [m.message_id])  # стираем сам /start
    await send_fx(
        m.chat.id, EMOJI_MENU,
        f"Привет, <b>{html.quote(m.from_user.first_name)}</b>!\n"
        "Я предложка новостного канала.\n\n"
        f"1 пост = <b>${POST_PRICE}</b> переводом в казну —\n"
        "после текста попрошу скриншот чека.\n"
        "Ответ администрации придёт от моего имени.",
        reply_markup=kb_menu(),
    )


@dp.message(Command("help"), F.chat.type == ChatType.PRIVATE)
async def cmd_help(m: Message) -> None:
    text = (
        "<b>Пользователю</b>\n"
        "/start — главное меню\n"
        "/cancel — отменить ввод\n"
        "/help — эта справка"
    )
    uid = m.from_user.id
    if is_staff(uid):
        text += (
            "\n\n<b>Администрации</b>\n"
            "/recruit on|off — набор в персонал (+ рассылка)\n"
            "/admins — список администраторов\n"
            "Ответ в топике = сообщение автору от имени бота\n"
            "Сообщение с «!» — остаётся внутренним"
        )
    if can_manage(uid):
        text += (
            "\n\n<b>Владелец / сисадмин</b>\n"
            "/addadmin id — назначить админа\n"
            "/deladmin id — снять админа\n"
            "/block id [причина] — заблокировать\n"
            "/unblock id — разблокировать\n"
            "/blocked — список заблокированных"
        )
    if uid == OWNER_ID:
        text += "\n\n<b>Только владелец</b>\n/ids — ID чата, топика и премиум-эмодзи"
    await m.answer(text)


@dp.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
async def cmd_cancel(m: Message, state: FSMContext) -> None:
    if await state.get_state():
        data = await state.get_data()
        await state.clear()
        # стираем подсказки бота, ответы пользователя и саму команду
        await sweep(m.chat.id, [data.get("prompt"), *data.get("in", []),
                                m.message_id])
        await send_fx(m.chat.id, EMOJI_MENU,
                      "Действие отменено.", reply_markup=kb_menu())
    else:
        await m.answer("Нечего отменять.")


@dp.callback_query(F.data == "flow:cancel")
async def cb_flow_cancel(c: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await sweep(c.message.chat.id, [data.get("prompt"), *data.get("in", []),
                                    c.message.message_id])
    await c.answer("Отменено")
    await send_fx(c.message.chat.id, EMOJI_MENU,
                  "Действие отменено.", reply_markup=kb_menu())

# ── Сцена 1: пост за $500 ──

@dp.callback_query(F.data == "flow:post")
async def cb_new_post(c: CallbackQuery, state: FSMContext) -> None:
    db_user_upsert(c.from_user)
    await state.set_state(PostSG.text)
    await sweep(c.message.chat.id, [c.message.message_id])  # стираем меню
    prompt = await send_fx(
        c.message.chat.id, EMOJI_POST,
        "<b>Новый пост</b>\nПришлите текст (или фото с подписью) для канала.",
        reply_markup=kb_cancel(),
    )
    await state.update_data(prompt=prompt.message_id, **{"in": []})
    await c.answer()


@dp.message(PostSG.text, F.chat.type == ChatType.PRIVATE)
async def post_got_text(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await sweep(m.chat.id, [data.get("prompt")])  # стираем вопрос про текст
    await state.set_state(PostSG.check)
    prompt = await send_fx(
        m.chat.id, EMOJI_CHECK,
        f"Принято. Теперь оплата: <b>1 пост = ${POST_PRICE} в казну</b>.\n"
        "Пришлите <u>скриншот перевода</u> одним фото.",
        reply_markup=kb_cancel(),
    )
    # текст поста НЕ удаляем, пока не скопирован в топик — чистим в финале
    await state.update_data(post_msg=m.message_id, prompt=prompt.message_id,
                            **{"in": [m.message_id]})


@dp.message(PostSG.check, F.chat.type == ChatType.PRIVATE)
async def post_got_check(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not m.photo:
        await sweep(m.chat.id, [data.get("prompt"), m.message_id])
        prompt = await send_fx(
            m.chat.id, EMOJI_CHECK,
            "Нужен именно скриншот — отправьте фото чека.",
            reply_markup=kb_cancel(),
        )
        await state.update_data(prompt=prompt.message_id)
        return
    await state.clear()
    topic = await try_open_topic(m.from_user, "Пост")
    if topic is None:
        await m.answer("Техническая пауза — попробуйте через минуту. /start")
        return
    tid, title = topic

    post_copy = await bot.copy_message(
        ADMIN_CHAT_ID, m.chat.id, data["post_msg"], message_thread_id=tid)
    proof_copy = await bot.copy_message(
        ADMIN_CHAT_ID, m.chat.id, m.message_id, message_thread_id=tid)
    await bot.send_message(ADMIN_CHAT_ID, head_html(m.from_user, "Пост"),
                           message_thread_id=tid)
    await safe_pin(ADMIN_CHAT_ID, post_copy.message_id, tid)
    await safe_pin(ADMIN_CHAT_ID, proof_copy.message_id, tid)
    mgmt = await bot.send_message(
        ADMIN_CHAT_ID,
        f"Управление заявкой · ${POST_PRICE} чек выше",
        message_thread_id=tid, reply_markup=kb_manage_post(tid),
    )
    await safe_pin(ADMIN_CHAT_ID, mgmt.message_id, tid)
    db_topic_save(tid, m.from_user.id, "post", title, post_copy.message_id)

    # всё скопировано в топик — чистим диалог: чек, текст поста, подсказки
    await sweep(m.chat.id, [data.get("prompt"), *data.get("in", []),
                            m.message_id])
    await send_fx(
        m.chat.id, EMOJI_DONE,
        "<b>Готово!</b> Пост и чек переданы администрации.\n"
        "Решение и вопросы придут от меня — оставайтесь на связи.",
        reply_markup=kb_menu(),
    )
    log.info("Заявка #%s: пост от %s", tid, m.from_user.id)

# ── Сцена 2: восстановление ──

@dp.callback_query(F.data == "flow:restore")
async def cb_restore(c: CallbackQuery, state: FSMContext) -> None:
    db_user_upsert(c.from_user)
    await state.set_state(RestoreSG.collecting)
    await sweep(c.message.chat.id, [c.message.message_id])  # стираем меню
    prompt = await send_fx(
        c.message.chat.id, EMOJI_POST,
        "<b>Восстановление</b>\n"
        "Перешлите сюда скриншоты доказательств и продублируйте "
        "сообщения из старого бота (до 20 штук, по одному).\n"
        "Каждое сообщение отмечу 👍. Когда всё — нажмите «Готово».",
        reply_markup=kb_ready(),
    )
    await state.update_data(items=[], prompt=prompt.message_id, **{"in": []})
    await c.answer()


@dp.message(RestoreSG.collecting, F.chat.type == ChatType.PRIVATE)
async def restore_collect(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    items: list = data.get("items", [])
    if len(items) >= 20:
        await m.answer("Уже 20 материалов — нажмите «Готово».", reply_markup=kb_ready())
        return
    items.append({"chat": m.chat.id, "msg": m.message_id})
    inputs = data.get("in", [])
    inputs.append(m.message_id)
    await state.update_data(items=items, **{"in": inputs})
    try:
        await m.react([ReactionTypeEmoji(emoji="👍")])
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data == "flow:ready", RestoreSG.collecting)
async def restore_done(c: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    items: list = data.get("items", [])
    if not items:
        await c.answer("Сначала отправьте материалы", show_alert=True)
        return
    await state.clear()
    topic = await try_open_topic(c.from_user, "Восстановление")
    if topic is None:
        await c.answer("Техническая пауза — попробуйте через минуту", show_alert=True)
        return
    tid, title = topic
    head = await bot.send_message(
        ADMIN_CHAT_ID,
        head_html(c.from_user, "Восстановление") + f"\nМатериалов: {len(items)}",
        message_thread_id=tid,
    )
    await safe_pin(ADMIN_CHAT_ID, head.message_id, tid)
    for it in items:
        try:
            await bot.copy_message(ADMIN_CHAT_ID, it["chat"], it["msg"],
                                   message_thread_id=tid)
        except TelegramBadRequest as e:
            log.warning("restore copy: %s", e.message)
    mgmt = await bot.send_message(
        ADMIN_CHAT_ID, "Управление обращением",
        message_thread_id=tid, reply_markup=kb_manage_restore(tid),
    )
    await safe_pin(ADMIN_CHAT_ID, mgmt.message_id, tid)
    db_topic_save(tid, c.from_user.id, "restore", title)
    # материалы уже в топике — стираем всю подборку и приглашение
    await sweep(c.message.chat.id, [data.get("prompt"), *data.get("in", []),
                                    c.message.message_id])
    await send_fx(
        c.message.chat.id, EMOJI_DONE,
        f"<b>Готово!</b> Передал администрации {len(items)} материал(а/ов).\n"
        "Ответ придёт от меня.",
        reply_markup=kb_menu(),
    )
    await c.answer("Готово")

# ── Сцена 3: анкета в персонал ──

@dp.callback_query(F.data == "flow:apply")
async def cb_apply(c: CallbackQuery, state: FSMContext) -> None:
    if get_setting("recruiting") != "1":
        await c.answer("Набор сейчас закрыт", show_alert=True)
        return
    db_user_upsert(c.from_user)
    await state.set_state(ApplySG.q)
    await sweep(c.message.chat.id, [c.message.message_id])  # стираем меню
    prompt = await c.message.answer(
        f"<b>Анкета сотрудника</b> · вопрос 1/{len(APPLY_QUESTIONS)}\n"
        f"{APPLY_QUESTIONS[0]}",
        reply_markup=kb_cancel(),
    )
    await state.update_data(a=[], step=0, prompt=prompt.message_id, **{"in": []})
    await c.answer()


@dp.message(ApplySG.q, F.chat.type == ChatType.PRIVATE)
async def apply_step(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    answers: list = data.get("a", [])
    answers.append(html.quote((m.text or m.caption or "[медиа]")[:600]))
    step = data.get("step", 0) + 1
    inputs = data.get("in", []) + [m.message_id]
    # стираем предыдущий вопрос — чат остаётся чистым, анкета уже в памяти
    await sweep(m.chat.id, [data.get("prompt")])
    if step < len(APPLY_QUESTIONS):
        prompt = await send_fx(
            m.chat.id, EMOJI_POST,
            f"Вопрос {step + 1}/{len(APPLY_QUESTIONS)}\n{APPLY_QUESTIONS[step]}",
            reply_markup=kb_cancel(),
        )
        await state.update_data(a=answers, step=step, prompt=prompt.message_id,
                                **{"in": inputs})
        return
    await state.update_data(**{"in": inputs})
    await state.clear()
    topic = await try_open_topic(m.from_user, "Анкета")
    if topic is None:
        await m.answer("Техническая пауза — попробуйте позже. /start")
        return
    tid, title = topic
    qa = "\n\n".join(
        f"<b>{i + 1}. {APPLY_QUESTIONS[i]}</b>\n{answers[i]}"
        for i in range(len(APPLY_QUESTIONS))
    )
    form_msg = await bot.send_message(
        ADMIN_CHAT_ID,
        head_html(m.from_user, "Анкета") + "\n\n" + qa,
        message_thread_id=tid,
    )
    await safe_pin(ADMIN_CHAT_ID, form_msg.message_id, tid)
    mgmt = await bot.send_message(
        ADMIN_CHAT_ID, "Управление анкетой",
        message_thread_id=tid, reply_markup=kb_manage_apply(tid, m.from_user.id),
    )
    await safe_pin(ADMIN_CHAT_ID, mgmt.message_id, tid)
    db_topic_save(tid, m.from_user.id, "apply", title)
    # анкета уже собрана в топике — стираем все вопросы и ответы из диалога
    await sweep(m.chat.id, [data.get("prompt"), *inputs])
    await send_fx(
        m.chat.id, EMOJI_DONE,
        "<b>Анкета отправлена!</b> Если кандидатура подойдёт — "
        "администрация напишет вам от моего имени.",
        reply_markup=kb_menu(),
    )

# ═════════════════════ УПРАВЛЕНИЕ В ТОПИКАХ ═════════════════════

@dp.callback_query(F.data.startswith("mng:pub:"))
async def cb_publish(c: CallbackQuery) -> None:
    if not is_staff(c.from_user.id):
        await c.answer("Только для администрации", show_alert=True)
        return
    tid = int(c.data.rsplit(":", 1)[1])
    row = db_topic(tid)
    if not row or row["status"] != "open":
        await c.answer("Заявка уже закрыта", show_alert=True)
        return
    if row["kind"] != "post":
        await c.answer("Это не пост", show_alert=True)
        return
    try:
        await bot.copy_message(CHANNEL_ID, ADMIN_CHAT_ID, row["post_msg_id"])
    except TelegramBadRequest as e:
        await c.answer(f"Канал недоступен: {e.message}", show_alert=True)
        return
    await c.answer("Опубликовано")
    await c.message.edit_text(
        f"<b>Опубликовано в канал</b>\n{html.quote(c.from_user.full_name)}"
    )
    await finalize_topic(
        row, "✅", "published", c.from_user, "опубликована",
        "Ваш пост опубликован в канале! Спасибо за поддержку казны.",
    )


@dp.callback_query(F.data.startswith("mng:ban:"))
async def cb_ban(c: CallbackQuery) -> None:
    if not is_staff(c.from_user.id):
        await c.answer("Только для администрации", show_alert=True)
        return
    tid = int(c.data.rsplit(":", 1)[1])
    row = db_topic(tid)
    if not row or row["status"] != "open":
        await c.answer("Заявка уже закрыта", show_alert=True)
        return
    uid = row["user_id"]
    if can_manage(uid):
        await c.answer("Системные роли блокировать нельзя", show_alert=True)
        return
    db_block(uid, c.from_user.id, "через заявку")
    await c.answer("Пользователь заблокирован")
    await c.message.edit_text(
        f"<b>Автор заблокирован</b>\n{html.quote(c.from_user.full_name)}\n"
        "Сообщения от него больше не принимаются."
    )
    await finalize_topic(
        row, "🚫", "banned", c.from_user, "заблокирована",
        "Вы заблокированы в предложке. Сообщения больше не принимаются.",
    )


@dp.callback_query(F.data.startswith("mng:close:"))
async def cb_close(c: CallbackQuery) -> None:
    if not is_staff(c.from_user.id):
        await c.answer("Только для администрации", show_alert=True)
        return
    tid = int(c.data.rsplit(":", 1)[1])
    row = db_topic(tid)
    if not row or row["status"] != "open":
        await c.answer("Уже закрыто", show_alert=True)
        return
    await c.answer("Заявка закрыта")
    await c.message.edit_text(
        f"<b>Заявка закрыта</b>\n{html.quote(c.from_user.full_name)}"
    )
    await finalize_topic(
        row, "🗂", "closed", c.from_user, "закрыта",
        "Ваша заявка закрыта администрацией.",
    )


@dp.callback_query(F.data.startswith("mng:mkadm:"))
async def cb_make_admin(c: CallbackQuery) -> None:
    if not can_manage(c.from_user.id):
        await c.answer("Назначать админов могут владелец и сисадмин", show_alert=True)
        return
    _, _, tid, uid = c.data.split(":")
    tid, uid = int(tid), int(uid)
    row = db_topic(tid)
    if not row or row["status"] != "open":
        await c.answer("Анкета уже закрыта", show_alert=True)
        return
    db_admin_add(uid, c.from_user.id)
    await c.answer("Администратор назначен")
    await c.message.edit_text(
        f"<b>Кандидат принят в админы</b>\n{html.quote(c.from_user.full_name)}"
    )
    try:
        await send_fx(
            uid, EMOJI_DONE,
            "<b>Поздравляем!</b> Вы приняты в команду предложки.\n"
            "Команды администратора: /help",
        )
    except TelegramForbiddenError:
        pass
    await finalize_topic(
        row, "✅", "closed", c.from_user, "принята",
        "Ваша анкета одобрена — добро пожаловать в команду!",
    )

# ── Мост: ответы админов в топике → автору ──

@dp.message(F.chat.id == ADMIN_CHAT_ID, F.message_thread_id)
async def relay_to_user(m: Message) -> None:
    if not m.from_user or m.from_user.is_bot:
        return
    if m.text and m.text.startswith(("!", "/")):
        return  # внутренний комментарий / команда
    row = db_topic(m.message_thread_id)
    if not row or row["status"] != "open":
        return
    if not is_staff(m.from_user.id):
        return
    try:
        await m.copy_to(chat_id=row["user_id"])
    except TelegramForbiddenError:
        await m.reply("Автор недоступен — он заблокировал бота.")

# ═════════════════════ АДМИН-КОМАНДЫ ═════════════════════

@dp.message(Command("recruit"), F.chat.type == ChatType.PRIVATE)
async def cmd_recruit(m: Message, command: CommandObject) -> None:
    if not is_staff(m.from_user.id):
        await m.answer("Нет прав.")
        return
    args = (command.args or "").strip().lower()
    if args == "on":
        set_setting("recruiting", "1")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            Btn(text="Открыть анкету · Google Docs", url=GOOGLE_DOC_URL)
        ]])
        text = (
            "📣 <b>Объявлен набор в персонал предложки!</b>\n\n"
            "Хочешь помогать новостному каналу? Заполни анкету по кнопке "
            "ниже или пройди мини-анкету прямо здесь: /start → "
            "«Подать анкету в персонал»."
        )
        ok = fail = 0
        for uid in db_all_users():
            try:
                await send_fx(uid, EMOJI_RECRUIT, text, reply_markup=kb)
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
        await m.answer(
            f"Набор открыт 📣\nРассылка: доставлено <b>{ok}</b>, ошибок {fail}.\n"
            "Кнопка анкеты появилась в меню /start. Стоп: /recruit off"
        )
    elif args == "off":
        set_setting("recruiting", "0")
        await m.answer("Набор закрыт, кнопка анкеты скрыта из меню.")
    else:
        await m.answer("Формат: /recruit on — объявить набор, /recruit off — закрыть.")


@dp.message(Command("admins"), F.chat.type == ChatType.PRIVATE)
async def cmd_admins(m: Message) -> None:
    if not is_staff(m.from_user.id):
        await m.answer("Нет прав.")
        return
    lines = [
        f"Владелец: <code>{OWNER_ID}</code>",
        f"Сисадмин: <code>{SYSADMIN_ID}</code>",
    ]
    extra = db_admins()
    if extra:
        lines.append("Назначенные:")
        lines += [f"  • <code>{a}</code>" for a in extra]
    else:
        lines.append("Назначенных админов нет.")
    await m.answer("\n".join(lines))


@dp.message(Command("addadmin"), F.chat.type == ChatType.PRIVATE)
async def cmd_addadmin(m: Message, command: CommandObject) -> None:
    if not can_manage(m.from_user.id):
        await m.answer("Только владелец и сисадмин.")
        return
    if not command.args or not command.args.split()[0].isdigit():
        await m.answer("Формат: /addadmin user_id")
        return
    uid = int(command.args.split()[0])
    if uid in (OWNER_ID, SYSADMIN_ID):
        await m.answer("Это системная роль — уже имеет все права.")
        return
    db_admin_add(uid, m.from_user.id)
    await m.answer(f"✅ <code>{uid}</code> назначен администратором.")
    try:
        await bot.send_message(uid, "Вам выданы права администратора предложки. /help")
    except TelegramForbiddenError:
        pass


@dp.message(Command("deladmin"), F.chat.type == ChatType.PRIVATE)
async def cmd_deladmin(m: Message, command: CommandObject) -> None:
    if not can_manage(m.from_user.id):
        await m.answer("Только владелец и сисадмин.")
        return
    if not command.args or not command.args.split()[0].isdigit():
        await m.answer("Формат: /deladmin user_id")
        return
    uid = int(command.args.split()[0])
    if uid in (OWNER_ID, SYSADMIN_ID):
        await m.answer("Системные роли снимаются только в коде bot.py.")
        return
    db_admin_del(uid)
    await m.answer(f"🗑 <code>{uid}</code> снят с должности.")
    try:
        await bot.send_message(uid, "Ваши права администратора предложки сняты.")
    except TelegramForbiddenError:
        pass


@dp.message(Command("block"), F.chat.type == ChatType.PRIVATE)
async def cmd_block(m: Message, command: CommandObject) -> None:
    if not can_manage(m.from_user.id):
        await m.answer("Только владелец и сисадмин.")
        return
    if not command.args or not command.args.split()[0].isdigit():
        await m.answer("Формат: /block user_id [причина]")
        return
    parts = command.args.split(maxsplit=1)
    uid = int(parts[0])
    reason = parts[1] if len(parts) > 1 else "без причины"
    if can_manage(uid):
        await m.answer("Системные роли блокировать нельзя.")
        return
    db_block(uid, m.from_user.id, reason)
    await m.answer(f"🚫 <code>{uid}</code> заблокирован. Причина: {html.quote(reason)}")
    try:
        await bot.send_message(uid, "Вы заблокированы в предложке.")
    except TelegramForbiddenError:
        pass


@dp.message(Command("unblock"), F.chat.type == ChatType.PRIVATE)
async def cmd_unblock(m: Message, command: CommandObject) -> None:
    if not can_manage(m.from_user.id):
        await m.answer("Только владелец и сисадмин.")
        return
    if not command.args or not command.args.split()[0].isdigit():
        await m.answer("Формат: /unblock user_id")
        return
    uid = int(command.args.split()[0])
    db_unblock(uid)
    await m.answer(f"✅ <code>{uid}</code> разблокирован.")
    try:
        await bot.send_message(uid, "Доступ к предложке восстановлен. /start")
    except TelegramForbiddenError:
        pass


@dp.message(Command("blocked"), F.chat.type == ChatType.PRIVATE)
async def cmd_blocked(m: Message) -> None:
    if not can_manage(m.from_user.id):
        await m.answer("Только владелец и сисадмин.")
        return
    rows = db_blocked()
    if not rows:
        await m.answer("Чёрный список пуст.")
        return
    lines = ["<b>Заблокированные:</b>"] + [
        f"• <code>{r['user_id']}</code> — {html.quote(r['reason'] or '')} "
        f"({r['blocked_at'][:10]})"
        for r in rows[:50]
    ]
    await m.answer("\n".join(lines))

# ── Утилита владельца: ID чатов, топиков, премиум-эмодзи ──

def extract_custom_emoji(m: Message) -> list[str]:
    ents = list(m.entities or []) + list(m.caption_entities or [])
    return [e.custom_emoji_id for e in ents if e.type == "custom_emoji"]


@dp.message(Command("ids"))
async def cmd_ids(m: Message, state: FSMContext) -> None:
    owner_not_set = OWNER_ID == _PLACEHOLDERS["OWNER_ID"]
    if m.from_user.id != OWNER_ID and not owner_not_set:
        return  # молча: команда только для владельца
    lines = [
        f"Ваш ID: <code>{m.from_user.id}</code>",
        f"Чат: <code>{m.chat.id}</code> ({m.chat.type})",
    ]
    if m.message_thread_id:
        lines.append(f"Топик: <code>{m.message_thread_id}</code>")
    if owner_not_set:
        lines.append(
            "⚠️ OWNER_ID ещё заглушка: /ids временно открыта всем — "
            "впишите свой ID из этой строки в константу и перезапустите бота!"
        )
    emojis = extract_custom_emoji(m)
    if emojis:
        lines.append("Премиум-эмодзи:")
        lines += [f"<code>{e}</code>" for e in emojis]
    else:
        lines.append("Пришлите следующим сообщением премиум-эмодзи — верну их ID.")
        await state.set_state(IdsSG.emoji)
    await m.answer("\n".join(lines))


@dp.message(IdsSG.emoji)
async def ids_catch_emoji(m: Message, state: FSMContext) -> None:
    if m.from_user.id != OWNER_ID and OWNER_ID != _PLACEHOLDERS["OWNER_ID"]:
        return
    emojis = extract_custom_emoji(m)
    if not emojis:
        await m.answer("Премиум-эмодзи не найдены. Пришлите ещё раз или /cancel.")
        return
    await state.clear()
    await m.answer("ID эмодзи:\n" + "\n".join(f"<code>{e}</code>" for e in emojis))

# ───────────────────────── ЗАПУСК ─────────────────────────

async def verify_config() -> None:
    """Диагностика при старте: какой файл запущен и что бот реально видит."""
    log.info(
        "Запущен файл: %s · сборка %s · база: %s",
        os.path.abspath(__file__), BOT_BUILD, os.path.abspath(DB_PATH),
    )
    log.info(
        "Конфиг · OWNER_ID=%s · SYSADMIN_ID=%s · ADMIN_CHAT_ID=%s · CHANNEL_ID=%s",
        OWNER_ID, SYSADMIN_ID, ADMIN_CHAT_ID, CHANNEL_ID,
    )
    left = [k for k in _PLACEHOLDERS if globals()[k] == _PLACEHOLDERS[k]]
    if "ВСТАВЬ" in GOOGLE_DOC_URL:
        left.append("GOOGLE_DOC_URL")
    if left:
        log.error(
            "НЕ ЗАПОЛНЕНЫ КОНСТАНТЫ: %s — бот работает на примерах-заглушках! "
            "Отредактируйте именно ЭТОТ файл: %s (блок НАСТРОЙКИ в начале) "
            "или задайте env-переменные с теми же именами. "
            "Точные ID покажет /ids — пока бот не настроен, она открыта всем",
            ", ".join(left), os.path.abspath(__file__),
        )
    if ADMIN_CHAT_ID == CHANNEL_ID:
        log.warning(
            "ADMIN_CHAT_ID и CHANNEL_ID сейчас ОДИНАКОВЫЕ: %s — "
            "в одну из переменных вставлен не тот ID",
            ADMIN_CHAT_ID,
        )
    me = await bot.get_me()
    for name, cid in (("ADMIN_CHAT_ID", ADMIN_CHAT_ID), ("CHANNEL_ID", CHANNEL_ID)):
        try:
            chat = await bot.get_chat(cid)
        except TelegramBadRequest as e:
            log.error(
                "%s=%r → %s. Бот не видит этот чат! Чек-лист: "
                "1) бот — участник и админ этого чата; "
                "2) ID супергруппы/канала начинается с -100; "
                "3) точный ID покажет /ids с аккаунта владельца прямо в этом чате",
                name, cid, e.message,
            )
            continue
        log.info("%s=%s · «%s» · тип: %s", name, cid, chat.title, chat.type)
        if name == "ADMIN_CHAT_ID":
            log.info("   топики (форум) включены: %s", getattr(chat, "is_forum", "?"))
        try:
            member = await bot.get_chat_member(cid, me.id)
            log.info(
                "   статус бота: %s · manage_topics=%s · pin=%s · post=%s",
                member.status,
                getattr(member, "can_manage_topics", "—"),
                getattr(member, "can_pin_messages", "—"),
                getattr(member, "can_post_messages", "—"),
            )
            if name == "ADMIN_CHAT_ID":
                if not getattr(member, "can_manage_topics", False):
                    log.error("   ⛔ НЕТ права «Управление топиками» — топики заявок не создадутся!")
                if not getattr(member, "can_pin_messages", False):
                    log.error("   ⛔ НЕТ права «Закрепление сообщений» — пост и чек не закрепятся!")
            if name == "CHANNEL_ID" and not getattr(member, "can_post_messages", False):
                log.error(
                    "   ⛔ НЕТ права «Публикация сообщений» в канале! "
                    "Кнопка «Опубликовать» будет падать. Включите: "
                    "Канал → Администраторы → бот → Публикация сообщений"
                )
        except TelegramBadRequest as e:
            log.error("   get_chat_member: %s", e.message)


async def main() -> None:
    if "ВСТАВЬ" in BOT_TOKEN:
        log.error("Укажите BOT_TOKEN (env или константа в bot.py)")
        sys.exit(1)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ])
    log.info("Предложка-бот запускается…")
    await verify_config()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")
