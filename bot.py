import json
import os
import logging
from aiohttp import web

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

ACTORS_FILE = "actors.json"
TOPICS_FILE = "topics.json"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


class Register(StatesGroup):
    entering_nick = State()


user_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎭 Мой ник")],
        [KeyboardButton(text="✏ Сменить ник")]
    ],
    resize_keyboard=True
)


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


actors = load_json(ACTORS_FILE)
topics = load_json(TOPICS_FILE)

tasks = {}
actor_selection = {}

task_status = {}
status_messages = {}
private_messages = {}


def topic_key(chat_id, thread_id):
    return f"{chat_id}:{thread_id}"


def find_actor_by_id(user_id):
    for nick, data in actors.items():
        if data["id"] == user_id:
            return nick
    return None


async def get_topic_name(message):
    if message.reply_to_message:
        if message.reply_to_message.forum_topic_created:
            return message.reply_to_message.forum_topic_created.name

    if message.forum_topic_created:
        return message.forum_topic_created.name

    if message.forum_topic_edited:
        return message.forum_topic_edited.name

    return None


async def ensure_topic_saved(message: types.Message):

    if not message.message_thread_id:
        return

    key = topic_key(message.chat.id, message.message_thread_id)

    name = await get_topic_name(message)

    if name:
        topics[key] = name
        save_json(TOPICS_FILE, topics)

    elif key not in topics:
        topics[key] = f"Тема {message.message_thread_id}"
        save_json(TOPICS_FILE, topics)


def build_status_text(task_id):

    lines = ["📊 Статусы актёров\n"]

    for actor, status in task_status[task_id].items():
        lines.append(f"{actor} — {status}")

    return "\n".join(lines)


@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):

    user_id = message.from_user.id
    nick = find_actor_by_id(user_id)

    if nick:
        await message.answer(
            f"Ты уже зарегистрирован как: {nick}",
            reply_markup=user_menu
        )
        return

    await state.set_state(Register.entering_nick)

    await message.answer("Введи свой ник актёра.")


@dp.message(Register.entering_nick)
async def save_nick(message: types.Message, state: FSMContext):

    new_nick = message.text.strip()
    user_id = message.from_user.id

    old_nick = find_actor_by_id(user_id)

    if old_nick:
        del actors[old_nick]

    actors[new_nick] = {
        "id": user_id,
        "telegram": message.from_user.username
    }

    save_json(ACTORS_FILE, actors)

    await state.clear()

    await message.answer(
        f"Ник сохранён: {new_nick}",
        reply_markup=user_menu
    )


@dp.message(Command("ping"))
async def ping(message: types.Message):
    await message.answer("pong")


def is_subtitles(message: types.Message):

    if not message.document:
        return False

    name = message.document.file_name.lower()

    return name.endswith(".srt") or name.endswith(".ass") or name.endswith(".txt")


@dp.message(F.document)
async def subtitles_detect(message: types.Message):

    await ensure_topic_saved(message)

    if not is_subtitles(message):
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🎙 Назначить актёров",
                callback_data=f"assign:{message.message_id}"
            )]
        ]
    )

    await message.reply("🎬 Панель серии", reply_markup=keyboard)


def build_actor_menu(message_id):

    buttons = []

    for name in actors.keys():

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


@dp.callback_query(F.data.startswith("send:"))
async def send_task(callback: types.CallbackQuery):

    message_id = int(callback.data.split(":")[1])
    selected = actor_selection.get(message_id, [])

    if not selected:
        await callback.answer("Выберите актёров.")
        return

    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id

    key = topic_key(chat_id, thread_id)
    topic = topics.get(key, "Без темы")

    task_id = str(message_id)

    tasks[task_id] = {
        "chat": chat_id,
        "thread": thread_id
    }

    task_status[task_id] = {}

    for actor in selected:
        task_status[task_id][actor] = "⏳"

    status_msg = await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=build_status_text(task_id)
    )

    status_messages[task_id] = status_msg.message_id

    for actor_name in selected:

        user_id = actors[actor_name]["id"]

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="👀 Увидел",
                        callback_data=f"seen:{task_id}:{user_id}"
                    ),
                    InlineKeyboardButton(
                        text="🎤 Записано",
                        callback_data=f"done:{task_id}:{user_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Не участвую",
                        callback_data=f"skip:{task_id}:{user_id}"
                    )
                ]
            ]
        )

        msg = await bot.send_message(
            user_id,
            f"🎙 Вам пришло на озвучку\n\n📂 {topic}\n\nСтатус: ⏳ ожидание",
            reply_markup=keyboard
        )

        private_messages[f"{task_id}_{user_id}"] = msg.message_id

    await callback.answer()
    await callback.message.edit_text("✅ Задание отправлено актёрам.")


async def update_status(callback, status, task_id, user_id):

    actor_name = find_actor_by_id(user_id)

    task_status[task_id][actor_name] = status

    group_msg = status_messages.get(task_id)

    task = tasks.get(task_id)

    if group_msg and task:

        await bot.edit_message_text(
            chat_id=task["chat"],
            message_id=group_msg,
            text=build_status_text(task_id)
        )

    private_msg = private_messages.get(f"{task_id}_{user_id}")

    if private_msg:

        lines = callback.message.text.split("\n")
        new_lines = []

        for line in lines:
            if not line.startswith("Статус:"):
                new_lines.append(line)

        new_lines.append(f"Статус: {status}")

        await bot.edit_message_text(
            chat_id=user_id,
            message_id=private_msg,
            text="\n".join(new_lines),
            reply_markup=callback.message.reply_markup
        )


@dp.callback_query(F.data.startswith("seen:"))
async def seen(callback: types.CallbackQuery):

    _, task_id, user_id = callback.data.split(":")
    user_id = int(user_id)

    await update_status(callback, "👀", task_id, user_id)

    await callback.answer()


@dp.callback_query(F.data.startswith("done:"))
async def done(callback: types.CallbackQuery):

    _, task_id, user_id = callback.data.split(":")
    user_id = int(user_id)

    await update_status(callback, "🎤", task_id, user_id)

    await callback.answer()


@dp.callback_query(F.data.startswith("skip:"))
async def skip(callback: types.CallbackQuery):

    _, task_id, user_id = callback.data.split(":")
    user_id = int(user_id)

    await update_status(callback, "❌", task_id, user_id)

    await callback.answer()


async def webhook_handler(request):

    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logging.exception(f"Webhook error: {e}")

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
