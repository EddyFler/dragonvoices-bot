import asyncio
import json
import os
import logging

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = "8618936533:AAGPKLwykJl4RWzTukDB4mXUd12bGaPZPFk"

ACTORS_FILE = "actors.json"
TOPICS_FILE = "topics.json"

bot = Bot(token=TOKEN)
dp = Dispatcher()

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
waiting_for_nick = {}
actor_selection = {}

# ---------- регистрация актёров ----------

@dp.message(Command("start"))
async def start(message: types.Message):

    user_id = message.from_user.id

    if user_id in [a["id"] for a in actors.values()]:
        await message.answer("Ты уже зарегистрирован.")
        return

    waiting_for_nick[user_id] = True

    await message.answer("Введи свой ник актёра.")

@dp.message(F.from_user.id.in_(lambda: waiting_for_nick.keys()))
async def save_nick(message: types.Message):

    user_id = message.from_user.id
    nick = message.text.strip()

    actors[nick] = {
        "id": user_id,
        "telegram": message.from_user.username
    }

    save_json(ACTORS_FILE, actors)

    waiting_for_nick.pop(user_id)

    await message.answer(f"Ник сохранён: {nick}")

# ---------- ping ----------

@dp.message(Command("ping"))
async def ping(message: types.Message):
    await message.answer("pong")

# ---------- темы ----------

@dp.message(F.forum_topic_created)
async def topic_created(message: types.Message):

    thread = str(message.message_thread_id)
    name = message.forum_topic_created.name

    topics[thread] = name
    save_json(TOPICS_FILE, topics)

@dp.message(F.forum_topic_edited)
async def topic_edited(message: types.Message):

    thread = str(message.message_thread_id)
    name = message.forum_topic_edited.name

    topics[thread] = name
    save_json(TOPICS_FILE, topics)

# ---------- определение субтитров ----------

def is_subtitles(message: types.Message):

    if not message.document:
        return False

    name = message.document.file_name.lower()

    return name.endswith(".srt") or name.endswith(".ass") or name.endswith(".txt")

# ---------- авто панель ----------

@dp.message(F.document)
async def subtitles_detect(message: types.Message):

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

# ---------- меню актёров ----------

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

    await callback.message.edit_text("Выберите актёров:", reply_markup=keyboard)

# ---------- переключение ----------

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

# ---------- отправка задания ----------

@dp.callback_query(F.data.startswith("send:"))
async def send_task(callback: types.CallbackQuery):

    message_id = int(callback.data.split(":")[1])
    selected = actor_selection.get(message_id, [])

    if not selected:
        await callback.answer("Выберите актёров.")
        return

    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id

    topic = topics.get(str(thread_id), "Без темы")

    chat_str = str(chat_id)
    chat_link_id = chat_str[4:] if chat_str.startswith("-100") else chat_str

    message_link = f"https://t.me/c/{chat_link_id}/{message_id}"

    for actor_name in selected:

        user_id = actors[actor_name]["id"]

        task_id = f"{message_id}_{user_id}"

        tasks[task_id] = {
            "chat": chat_id,
            "thread": thread_id,
            "topic": topic,
            "link": message_link,
            "original": message_id
        }

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📂 Открыть сообщение", url=message_link)],
                [
                    InlineKeyboardButton(text="👀 Увидел", callback_data=f"seen:{task_id}"),
                    InlineKeyboardButton(text="🎤 Записано", callback_data=f"done:{task_id}")
                ],
                [
                    InlineKeyboardButton(text="❌ Не участвую", callback_data=f"skip:{task_id}")
                ]
            ]
        )

        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=chat_id,
            message_id=message_id
        )

        await bot.send_message(
            user_id,
            f"🎙 Вам пришло на озвучку\n\n📂 {topic}\n\nСтатус: ⏳ ожидание",
            reply_markup=keyboard
        )

    await callback.message.edit_text("✅ Задание отправлено актёрам.")

# ---------- статусы ----------

async def update_status(callback, status, task_id, stop_timer=False):

    keyboard = callback.message.reply_markup

    lines = callback.message.text.split("\n")
    new_lines = []

    for line in lines:
        if not line.startswith("Статус:"):
            new_lines.append(line)

    new_lines.append(f"Статус: {status}")

    await callback.message.edit_text("\n".join(new_lines), reply_markup=keyboard)

    task = tasks.get(task_id)

    if task:

        user = callback.from_user.username or callback.from_user.first_name

        await bot.send_message(
            chat_id=task["chat"],
            message_thread_id=task["thread"],
            reply_to_message_id=task["original"],
            text=f"{status} @{user}\n📂 {task['topic']}\n🔗 {task['link']}"
        )

        if stop_timer:
            del tasks[task_id]

@dp.callback_query(F.data.startswith("seen:"))
async def seen(callback: types.CallbackQuery):

    task_id = callback.data.split(":")[1]
    await update_status(callback, "👀 Увидел", task_id, False)
    await callback.answer()

@dp.callback_query(F.data.startswith("done:"))
async def done(callback: types.CallbackQuery):

    task_id = callback.data.split(":")[1]
    await update_status(callback, "🎤 Записано", task_id, True)
    await callback.answer()

@dp.callback_query(F.data.startswith("skip:"))
async def skip(callback: types.CallbackQuery):

    task_id = callback.data.split(":")[1]
    await update_status(callback, "❌ Не участвует", task_id, True)
    await callback.answer()

# ---------- запуск ----------

async def main():

    logging.basicConfig(level=logging.INFO)

    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
