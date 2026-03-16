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

users = {}
tasks = {}

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users():
    with open(USERS_FILE,"w",encoding="utf-8") as f:
        json.dump(users,f)

users = load_users()

def register_user(user):
    if user.username:
        users[user.username] = user.id
        save_users()

# авто регистрация
@dp.message(~F.text.startswith("/"))
async def auto_register(message: types.Message):
    register_user(message.from_user)

@dp.message(Command("start"))
async def start(message: types.Message):
    register_user(message.from_user)
    await message.answer("Ты подключен к системе озвучки.")

@dp.message(Command("ping"))
async def ping(message: types.Message):
    await message.answer("pong")

# получение названия темы
async def get_topic_name(message: types.Message):

    if message.reply_to_message and message.reply_to_message.forum_topic_created:
        return message.reply_to_message.forum_topic_created.name

    if message.message_thread_id:
        try:
            topic = await bot.get_forum_topic(
                chat_id=message.chat.id,
                message_thread_id=message.message_thread_id
            )
            return topic.name
        except:
            pass

    return "Без темы"

async def reminder(user_id,text,keyboard,delay,task_id):

    await asyncio.sleep(delay)

    if task_id not in tasks:
        return

    try:
        await bot.send_message(user_id,text,reply_markup=keyboard)
    except:
        pass

@dp.message(Command("notify"))
async def notify(message: types.Message):

    register_user(message.from_user)

    if not message.reply_to_message:
        await message.reply("Ответь /notify на сообщение с субтитрами.")
        return

    topic = await get_topic_name(message)

    original = message.reply_to_message

    chat_id = str(message.chat.id)

    if chat_id.startswith("-100"):
        chat_link_id = chat_id[4:]
    else:
        chat_link_id = chat_id

    message_link = f"https://t.me/c/{chat_link_id}/{original.message_id}"

    args = message.text.split()

    usernames = []

    for arg in args[1:]:
        if arg.startswith("@"):
            usernames.append(arg.replace("@",""))

    sent = 0

    for username in usernames:

        if username not in users:
            continue

        user_id = users[username]

        task_id = f"{original.message_id}_{user_id}"

        tasks[task_id] = {
            "chat":message.chat.id,
            "topic":topic,
            "link":message_link
        }

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📂 Открыть сообщение",url=message_link)],
                [
                    InlineKeyboardButton(text="👀 Увидел",callback_data=f"seen:{task_id}"),
                    InlineKeyboardButton(text="🎤 Записано",callback_data=f"done:{task_id}")
                ],
                [
                    InlineKeyboardButton(text="❌ Не участвую",callback_data=f"skip:{task_id}")
                ]
            ]
        )

        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=original.chat.id,
            message_id=original.message_id
        )

        await bot.send_message(
            user_id,
            f"🎙 Вам пришло на озвучку\n\n📂 {topic}\n\nСтатус: ⏳ Ожидание",
            reply_markup=keyboard
        )

        asyncio.create_task(
            reminder(
                user_id,
                "⏰ Напоминание: у вас есть субтитры",
                keyboard,
                10800,
                task_id
            )
        )

        asyncio.create_task(
            reminder(
                user_id,
                "⏰ Последнее напоминание",
                keyboard,
                18000,
                task_id
            )
        )

        sent += 1

        await asyncio.sleep(0.3)

    await message.reply(f"Задание отправлено актёрам ({sent})")

async def update_status(callback,status,task_id,stop_timer=False):

    keyboard = callback.message.reply_markup

    lines = callback.message.text.split("\n")

    new_lines = []

    for line in lines:
        if not line.startswith("Статус:"):
            new_lines.append(line)

    new_lines.append(f"Статус: {status}")

    new_text = "\n".join(new_lines)

    await callback.message.edit_text(
        new_text,
        reply_markup=keyboard
    )

    task = tasks.get(task_id)

    if task:

        user = callback.from_user.username

        await bot.send_message(
            task["chat"],
            f"{status} @{user}\n📂 {task['topic']}\n{task['link']}"
        )

        if stop_timer:
            del tasks[task_id]

@dp.callback_query(F.data.startswith("seen:"))
async def seen(callback: types.CallbackQuery):

    task_id = callback.data.split(":")[1]

    await update_status(callback,"👀 Увидел",task_id,False)

    await callback.answer()

@dp.callback_query(F.data.startswith("done:"))
async def done(callback: types.CallbackQuery):

    task_id = callback.data.split(":")[1]

    await update_status(callback,"🎤 Записано",task_id,True)

    await callback.answer()

@dp.callback_query(F.data.startswith("skip:"))
async def skip(callback: types.CallbackQuery):

    task_id = callback.data.split(":")[1]

    await update_status(callback,"❌ Не участвует",task_id,True)

    await callback.answer()

async def main():

    logging.basicConfig(level=logging.INFO)

    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
