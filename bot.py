
import logging
import os
import json
from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Update
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage


TOKEN = "8618936533:AAGPKLwykJl4RWzTukDB4mXUd12bGaPZPFk"

BASE_URL = "https://dragonvoices-bot.onrender.com"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = BASE_URL + WEBHOOK_PATH

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ---------- GOOGLE SHEETS ----------

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

CREDS_PATH = "/etc/secrets/credentials.json"

with open(CREDS_PATH) as f:
    creds_dict = json.load(f)

creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

creds = Credentials.from_service_account_info(
    creds_dict,
    scopes=scope
)

client = gspread.authorize(creds)
spreadsheet = client.open_by_key("1yZgjuvatvSur-pxpOq3lA9Lzc3GRovcJnMK1qHFP-i0")

actors_sheet = spreadsheet.worksheet("actors")


# ---------- STORAGE ----------

tasks = {}
actor_selection = {}
task_status = {}
status_messages = {}
task_meta = {}
actor_messages = {}


# ---------- FSM ----------

class Register(StatesGroup):
    entering_nick = State()

class ChangeNick(StatesGroup):
    entering_new_nick = State()


user_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="▶ Старт")],
        [KeyboardButton(text="🎭 Мой ник")],
        [KeyboardButton(text="✏ Сменить ник")]
    ],
    resize_keyboard=True
)


# ---------- ACTORS ----------

def find_actor_by_id(user_id):

    rows = actors_sheet.get_all_records()

    for row in rows:
        if int(row["user_id"]) == user_id:
            return row["nick"]

    return None


def save_actor(user_id, nick, telegram):

    actors_sheet.append_row([
        user_id,
        nick,
        telegram
    ])


def update_actor(user_id, nick):

    rows = actors_sheet.get_all_records()

    for i, row in enumerate(rows, start=2):
        if int(row["user_id"]) == user_id:
            actors_sheet.update_cell(i, 2, nick)


def get_all_actors():

    rows = actors_sheet.get_all_records()
    return [r["nick"] for r in rows]


def get_actor_id_by_nick(nick):

    rows = actors_sheet.get_all_records()

    for r in rows:
        if r["nick"] == nick:
            return int(r["user_id"])

    return None


# ---------- STATUS TEXT ----------

def build_status(task_id):

    lines = ["📊 Статусы актёров", ""]

    for user_id, status in task_status[task_id].items():

        nick = find_actor_by_id(user_id)

        if not nick:
            nick = f"id:{user_id}"

        lines.append(f"{nick} — {status}")

    return "\n".join(lines)


# ---------- SUBTITLES ----------

def is_subtitles(message: types.Message):

    if not message.document:
        return False

    name = message.document.file_name.lower()

    return name.endswith(".srt") or name.endswith(".ass") or name.endswith(".txt")


@dp.message(F.document)
async def subtitles_detect(message: types.Message):

    if not is_subtitles(message):
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎙 Назначить актёров",
                    callback_data=f"assign:{message.message_id}"
                )
            ]
        ]
    )

    await message.reply(
        "🎬 Панель серии",
        reply_markup=keyboard
    )


# ---------- ACTOR MENU ----------

def build_actor_menu(message_id):

    buttons = []

    actors = get_all_actors()

    for name in actors:

        selected = False

        if message_id in actor_selection:
            selected = name in actor_selection[message_id]

        mark = "☑" if selected else "☐"

        buttons.append([
            InlineKeyboardButton(
                text=f"{mark} {name}",
                callback_data=f"toggle:{message_id}:{name}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="🚀 Отправить задание",
            callback_data=f"send:{message_id}"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.startswith("assign:"))
async def open_actor_menu(callback: types.CallbackQuery):

    message_id = int(callback.data.split(":")[1])

    actor_selection[message_id] = set()

    keyboard = build_actor_menu(message_id)

    await callback.message.edit_text(
        "Выберите актёров:",
        reply_markup=keyboard
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_actor(callback: types.CallbackQuery):

    _, message_id, name = callback.data.split(":")
    message_id = int(message_id)

    if message_id not in actor_selection:
        actor_selection[message_id] = set()

    if name in actor_selection[message_id]:
        actor_selection[message_id].remove(name)
    else:
        actor_selection[message_id].add(name)

    keyboard = build_actor_menu(message_id)

    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


# ---------- SEND TASK ----------

@dp.callback_query(F.data.startswith("send:"))
async def send_task(callback: types.CallbackQuery):

    message_id = int(callback.data.split(":")[1])
    selected = actor_selection.get(message_id, [])

    if not selected:
        await callback.answer("Выберите актёров.")
        return

    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id

    chat_str = str(chat_id)
    chat_link_id = chat_str[4:]

    message_link = f"https://t.me/c/{chat_link_id}/{message_id}"

    task_id = str(message_id)

    tasks[task_id] = {
        "chat": chat_id,
        "thread": thread_id,
        "link": message_link,
        "original": message_id
    }

    task_status[task_id] = {}

    for actor_name in selected:
        user_id = get_actor_id_by_nick(actor_name)
        task_status[task_id][user_id] = "⏳"

    status_msg = await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        reply_to_message_id=message_id,
        text=build_status(task_id)
    )

    status_messages[task_id] = status_msg.message_id
    task_meta[task_id] = (chat_id, thread_id)

    for actor_name in selected:

        user_id = get_actor_id_by_nick(actor_name)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📂 Открыть сообщение", url=message_link)],
                [
                    InlineKeyboardButton(text="👀 Увидел", callback_data=f"seen:{task_id}:{user_id}"),
                    InlineKeyboardButton(text="🎤 Записано", callback_data=f"done:{task_id}:{user_id}")
                ],
                [
                    InlineKeyboardButton(text="❌ Не участвую", callback_data=f"skip:{task_id}:{user_id}")
                ]
            ]
        )

        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=chat_id,
            message_id=message_id
        )

        msg = await bot.send_message(
            user_id,
            "🎙 Вам пришло на озвучку\n\nСтатус: ⏳",
            reply_markup=keyboard
        )

        actor_messages[(task_id, user_id)] = msg.message_id

    await callback.message.edit_text("✅ Задание отправлено актёрам.")
    await callback.answer()


# ---------- UPDATE STATUS ----------

async def update_status(task_id, user_id, status):

    task_status[task_id][user_id] = status

    msg_id = status_messages.get(task_id)
    chat_id, thread_id = task_meta[task_id]

    if msg_id:

        await bot.edit_message_text(
            text=build_status(task_id),
            chat_id=chat_id,
            message_id=msg_id
        )

    actor_msg = actor_messages.get((task_id, user_id))

    if actor_msg:

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📂 Открыть сообщение", url=tasks[task_id]["link"])],
                [
                    InlineKeyboardButton(text="👀 Увидел", callback_data=f"seen:{task_id}:{user_id}"),
                    InlineKeyboardButton(text="🎤 Записано", callback_data=f"done:{task_id}:{user_id}")
                ],
                [
                    InlineKeyboardButton(text="❌ Не участвую", callback_data=f"skip:{task_id}:{user_id}")
                ]
            ]
        )

        await bot.edit_message_text(
            chat_id=user_id,
            message_id=actor_msg,
            text=f"🎙 Вам пришло на озвучку\n\nСтатус: {status}",
            reply_markup=keyboard
        )


@dp.callback_query(F.data.startswith("seen:"))
async def seen(callback: types.CallbackQuery):

    _, task_id, user_id = callback.data.split(":")
    await update_status(task_id, int(user_id), "👀")
    await callback.answer()


@dp.callback_query(F.data.startswith("done:"))
async def done(callback: types.CallbackQuery):

    _, task_id, user_id = callback.data.split(":")
    await update_status(task_id, int(user_id), "✅")
    await callback.answer()


@dp.callback_query(F.data.startswith("skip:"))
async def skip(callback: types.CallbackQuery):

    _, task_id, user_id = callback.data.split(":")
    await update_status(task_id, int(user_id), "❌")
    await callback.answer()


# ---------- WEBHOOK ----------

async def webhook_handler(request):

    try:

        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)

    except Exception:

        logging.exception("Webhook error")

    return web.Response(text="ok")


async def on_startup(app):

    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)


async def on_shutdown(app):

    await bot.delete_webhook()
    await bot.session.close()


def create_app():

    app = web.Application()

    app.router.add_post(WEBHOOK_PATH, webhook_handler)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    logging.info(f"Starting server on port {port}")

    web.run_app(create_app(), host="0.0.0.0", port=port)
