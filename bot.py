import asyncio
import logging
import json
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = "8618936533:AAGPKLwykJl4RWzTukDB4mXUd12bGaPZPFk"
USERS_FILE = "users.json"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------- базы ----------

users = {}
tasks = {}

# ---------- загрузка пользователей ----------

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users():
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f)

users = load_users()

def register_user(user: types.User):
    if user and user.username:
        users[user.username] = user.id
        save_users()

# ---------- авто регистрация (но НЕ для команд) ----------

@dp.message(~F.text.startswith("/"))
async def auto_register(message: types.Message):
    register_user(message.from_user)

# ---------- /start ----------

@dp.message(Command("start"))
async def start(message: types.Message):
    register_user(message.from_user)
    await message.answer("Ты подключен к системе озвучки.")

# ---------- /ping ----------

@dp.message(Command("ping"))
async def ping(message: types.Message):
    await message.answer("pong")

# ---------- напоминание ----------

async def reminder(user_id, text, keyboard, delay, task_id):

    await asyncio.sleep(delay)

    if task_id not in tasks:
        return

    try:
        await bot.send_message(
            user_id,
            text,
            reply_markup=keyboard
        )
    except:
        pass

# ---------- notify ----------

@dp.message(Command("notify"))
async def notify(message: types.Message):

    register_user(message.from_user)

    if not message.reply_to_message:
        await message.reply("Ответь /notify на сообщение с субтитрами.")
        return

    args = message.text.split()
    usernames = []

    for arg in args[1:]:
        if arg.startswith("@"):
            usernames.append(arg.replace("@", ""))

    original = message.reply_to_message

    chat_id = str(message.chat.id)

    if chat_id.startswith("-100"):
        chat_link_id = chat_id[4:]
    else:
        chat_link_id = chat_id

    message_link = f"https://t.me/c/{chat_link_id}/{original.message_id}"

    topic = "Без темы"

    if message.message_thread_id:
        topic = f"Тема #{message.message_thread_id}"

    sent = 0

    for username in usernames:

        if username not in users:
            continue

        user_id = users[username]

        task_id = f"{original.message_id}_{user_id}"
        tasks[task_id] = True

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📂 Открыть субтитры", url=message_link)],
                [
                    InlineKeyboardButton(text="👀 Увидел", callback_data=f"seen:{task_id}"),
                    InlineKeyboardButton(text="🎤 Записано", callback_data=f"done:{task_id}")
                ],
                [
                    InlineKeyboardButton(text="❌ Не участвую", callback_data=f"skip:{task_id}")
                ]
            ]
        )

        try:

            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=original.chat.id,
                message_id=original.message_id
            )

            await bot.send_message(
                user_id,
                f"🎙 Вам пришло на озвучку\n\n📂 Тема: {topic}",
                reply_markup=keyboard
            )

            asyncio.create_task(
                reminder(
                    user_id,
                    "⏰ Напоминание: у вас есть субтитры на озвучку",
                    keyboard,
                    10800,
                    task_id
                )
            )

            asyncio.create_task(
                reminder(
                    user_id,
                    "⏰ Последнее напоминание: проверьте субтитры",
                    keyboard,
                    18000,
                    task_id
                )
            )

            sent += 1

            await asyncio.sleep(0.4)

        except Exception as e:
            print("Ошибка отправки:", e)

    await message.reply(f"Задание отправлено актёрам ({sent}).")

# ---------- кнопки ----------

@dp.callback_query(F.data.startswith("seen:"))
async def seen(callback: types.CallbackQuery):

    await callback.message.edit_text(
        callback.message.text + "\n\n👀 Увидел"
    )

    await callback.answer("Отмечено")


@dp.callback_query(F.data.startswith("done:"))
async def done(callback: types.CallbackQuery):

    task_id = callback.data.split(":")[1]

    if task_id in tasks:
        del tasks[task_id]

    user = callback.from_user.username or callback.from_user.full_name

    await callback.message.edit_text(
        callback.message.text + f"\n\n🎤 Записано: @{user}"
    )

    await callback.answer("Отмечено")


@dp.callback_query(F.data.startswith("skip:"))
async def skip(callback: types.CallbackQuery):

    task_id = callback.data.split(":")[1]

    if task_id in tasks:
        del tasks[task_id]

    user = callback.from_user.username or callback.from_user.full_name

    await callback.message.edit_text(
        callback.message.text + f"\n\n❌ Не участвует: @{user}"
    )

    await callback.answer("Отмечено")

# ---------- запуск ----------

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
