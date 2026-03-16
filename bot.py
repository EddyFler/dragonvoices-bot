import asyncio
import logging
import json
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("BOT_TOKEN") or "PUT_YOUR_TOKEN_HERE"
USERS_FILE = "users.json"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------- база пользователей ----------

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
    """Сохраняем пользователя, если у него есть username"""
    if user and user.username:
        users[user.username] = user.id
        save_users()

# ---------- авто-регистрация в любом сообщении ----------

@dp.message()
async def auto_register(message: types.Message):
    register_user(message.from_user)

# ---------- регистрация через /start ----------

@dp.message(Command("start"))
async def start(message: types.Message):
    register_user(message.from_user)
    await message.answer("Ты подключен к системе озвучки.")

# ---------- напоминание ----------

async def reminder(user_id, text, keyboard, delay):
    await asyncio.sleep(delay)
    try:
        await bot.send_message(user_id, text, reply_markup=keyboard)
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

    # ---------- ссылка на сообщение ----------
    chat_id = str(message.chat.id)

    if chat_id.startswith("-100"):
        chat_link_id = chat_id[4:]
    else:
        chat_link_id = chat_id

    message_link = f"https://t.me/c/{chat_link_id}/{original.message_id}"

    # ---------- получение названия темы ----------
    topic = "Без темы"

    if message.message_thread_id:
        try:
            topic_info = await bot.get_forum_topic(
                chat_id=message.chat.id,
                message_thread_id=message.message_thread_id
            )
            topic = topic_info.name
        except:
            topic = f"Тема #{message.message_thread_id}"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📂 Открыть субтитры", url=message_link)],
            [InlineKeyboardButton(text="✅ Озвучено", callback_data="done")]
        ]
    )

    sent = 0

    for username in usernames:

        if username not in users:
            continue

        user_id = users[username]

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
                    10800
                )
            )

            asyncio.create_task(
                reminder(
                    user_id,
                    "⏰ Последнее напоминание: проверьте субтитры",
                    keyboard,
                    18000
                )
            )

            sent += 1

            await asyncio.sleep(0.4)

        except Exception as e:
            print("Ошибка отправки:", e)

    await message.reply(f"Задание отправлено актёрам ({sent}).")

# ---------- кнопка DONE ----------

@dp.callback_query(F.data == "done")
async def done(callback: types.CallbackQuery):

    await callback.message.edit_text(
        callback.message.text + "\n\n✅ Озвучено"
    )

    await callback.answer("Отмечено")

# ---------- запуск ----------

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
