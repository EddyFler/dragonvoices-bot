import asyncio
import logging
import os
import json
from datetime import datetime, timedelta, timezone
from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Update
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage


TOKEN = "8618936533:AAGPKLwykJl4RWzTukDB4mXUd12bGaPZPFk"

SE_CHAT_ID = -5281880851       # Чат звукорежиссёра
LOG_CHAT_ID = -5238708793      # Чат для логирования ошибок

BASE_URL = "https://dragonvoices-bot.onrender.com"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = BASE_URL + WEBHOOK_PATH

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

MOSCOW_TZ = timezone(timedelta(hours=3))


# ---------- GOOGLE SHEETS ----------

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

CREDS_PATH = "/etc/secrets/credentials.json"

with open(CREDS_PATH) as f:
    creds_dict = json.load(f)

creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
client = gspread.authorize(creds)
spreadsheet = client.open_by_key("1yZgjuvatvSur-pxpOq3lA9Lzc3GRovcJnMK1qHFP-i0")

actors_sheet = spreadsheet.worksheet("actors")
topics_sheet = spreadsheet.worksheet("topics")
se_sheet = spreadsheet.worksheet("sound_engineers")
active_tasks_sheet = spreadsheet.worksheet("active_tasks")
history_sheet = spreadsheet.worksheet("history")
allowed_users_sheet = spreadsheet.worksheet("allowed_users")


# ---------- STORAGE ----------

tasks = {}
actor_selection = {}
task_status = {}
status_messages = {}
task_meta = {}
actor_messages = {}

deadlines = {}
recordings = {}
sound_engineers = {}
se_selection = {}
se_status_messages = {}
reminder_store = {}
subtitles_store = {}
se_file_messages = {}
se_ready_messages = {}
se_assigned_messages = {}
se_nicks = {}


# ---------- FSM ----------

class Register(StatesGroup):
    entering_nick = State()

class ChangeNick(StatesGroup):
    entering_new_nick = State()

class SeenState(StatesGroup):
    entering_deadline = State()

class DoneState(StatesGroup):
    entering_recording = State()


user_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="▶ Старт")],
        [KeyboardButton(text="🎭 Мой ник")],
        [KeyboardButton(text="✏ Сменить ник")]
    ],
    resize_keyboard=True
)


# ---------- ERROR LOGGING ----------

async def log_error(text: str):
    try:
        await bot.send_message(LOG_CHAT_ID, f"🚨 Ошибка:\n{text}")
    except Exception:
        pass


# ---------- GOOGLE SHEETS: ACTIVE TASKS ----------

def save_active_task_row(task_id, chat_id, thread_id, topic, link,
                          original_msg_id, status_msg_id,
                          user_id, user_status, deadline, actor_msg_id):
    active_tasks_sheet.append_row([
        str(task_id), str(chat_id), str(thread_id), topic, link,
        str(original_msg_id), str(status_msg_id),
        str(user_id), user_status, deadline, str(actor_msg_id)
    ])


def update_active_task_status(task_id, user_id, new_status):
    rows = active_tasks_sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        if str(row["task_id"]) == str(task_id) and str(row["user_id"]) == str(user_id):
            active_tasks_sheet.update_cell(i, 9, new_status)
            return


def update_active_task_deadline(task_id, user_id, deadline):
    rows = active_tasks_sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        if str(row["task_id"]) == str(task_id) and str(row["user_id"]) == str(user_id):
            active_tasks_sheet.update_cell(i, 10, deadline)
            return


def delete_active_task(task_id):
    rows = active_tasks_sheet.get_all_records()
    to_delete = [i + 2 for i, row in enumerate(rows) if str(row["task_id"]) == str(task_id)]
    for i in reversed(to_delete):
        active_tasks_sheet.delete_rows(i)


# ---------- GOOGLE SHEETS: HISTORY ----------

def save_history(topic, actors, sound_engineer, task_link):
    now = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    history_sheet.append_row([now, topic, actors, sound_engineer, task_link])


# ---------- RESTORE STATE ON STARTUP ----------

async def restore_state():
    try:
        rows = active_tasks_sheet.get_all_records()
        if not rows:
            return

        tasks_data = {}
        for row in rows:
            tid = str(row["task_id"])
            if tid not in tasks_data:
                tasks_data[tid] = {
                    "chat_id": int(row["chat_id"]),
                    "thread_id": int(row["thread_id"]) if row["thread_id"] else None,
                    "topic": row["topic"],
                    "link": row["link"],
                    "original": int(row["original_msg_id"]),
                    "status_msg_id": int(row["status_msg_id"]),
                    "users": []
                }
            tasks_data[tid]["users"].append({
                "user_id": int(row["user_id"]),
                "status": row["user_status"],
                "deadline": row["deadline"],
                "actor_msg_id": int(row["actor_msg_id"]) if row["actor_msg_id"] else None
            })

        for tid, data in tasks_data.items():
            tasks[tid] = {
                "chat": data["chat_id"],
                "thread": data["thread_id"],
                "link": data["link"],
                "original": data["original"],
                "topic": data["topic"]
            }
            task_meta[tid] = (data["chat_id"], data["thread_id"])
            status_messages[tid] = data["status_msg_id"]
            task_status[tid] = {}

            for u in data["users"]:
                uid = u["user_id"]
                task_status[tid][uid] = u["status"]
                if u["actor_msg_id"]:
                    actor_messages[(tid, uid)] = u["actor_msg_id"]
                if u["deadline"]:
                    deadlines[(tid, uid)] = u["deadline"]
                    if not u["status"].startswith("✅") and not u["status"].startswith("❌"):
                        schedule_reminders(tid, uid, u["deadline"])

        logging.info(f"Восстановлено {len(tasks_data)} активных задач из Google Sheets")
    except Exception as e:
        logging.error(f"Ошибка восстановления состояния: {e}")
        await log_error(f"Ошибка восстановления состояния при старте: {e}")


# ---------- ACTORS ----------

def find_actor_by_id(user_id):
    rows = actors_sheet.get_all_records()
    for row in rows:
        if int(row["user_id"]) == user_id:
            return row["nick"]
    return None


def is_allowed(user_id):
    rows = allowed_users_sheet.get_all_records()
    return any(int(r["user_id"]) == user_id for r in rows)


def save_actor(user_id, nick, telegram):
    actors_sheet.append_row([user_id, nick, telegram])


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


# ---------- SOUND ENGINEERS ----------

def get_all_sound_engineers():
    rows = se_sheet.get_all_records()
    return [r["nick"] for r in rows]


def get_se_id_by_nick(nick):
    rows = se_sheet.get_all_records()
    for r in rows:
        if r["nick"] == nick:
            return int(r["user_id"])
    return None


# ---------- TOPICS ----------

def save_topic(chat_id, thread_id, name):
    topics_sheet.append_row([chat_id, thread_id, name])


def get_topic(chat_id, thread_id):
    rows = topics_sheet.get_all_records()
    for r in rows:
        if int(r["chat_id"]) == chat_id and int(r["thread_id"]) == thread_id:
            return r["name"]
    return None


async def detect_topic(message: types.Message):
    if message.forum_topic_created:
        return message.forum_topic_created.name
    if message.reply_to_message:
        if message.reply_to_message.forum_topic_created:
            return message.reply_to_message.forum_topic_created.name
    return None


async def ensure_topic_saved(message: types.Message):
    if not message.message_thread_id:
        return
    name = await detect_topic(message)
    if name:
        if not get_topic(message.chat.id, message.message_thread_id):
            save_topic(message.chat.id, message.message_thread_id, name)


# ---------- STATUS TEXT ----------

def build_status(task_id):
    lines = ["📊 Статусы актёров", ""]
    for user_id, status in task_status[task_id].items():
        nick = find_actor_by_id(user_id) or f"id:{user_id}"
        nick_link = f'<a href="tg://user?id={user_id}">{nick}</a>'
        if "http" in status and "<a href" not in status:
            parts = status.split(" ", 1)
            emoji = parts[0]
            url = parts[1].strip() if len(parts) > 1 else ""
            status_display = f'{emoji} <a href="{url}">Ссылка</a>'
        else:
            status_display = status
        lines.append(f"{nick_link} — {status_display}")
    return "\n".join(lines)


# ---------- DEADLINE PARSING ----------

def parse_deadline(deadline_str: str):
    now = datetime.now(MOSCOW_TZ)
    formats = ["%d.%m.%Y %H:%M", "%d.%m %H:%M", "%H:%M"]
    for fmt in formats:
        try:
            dt = datetime.strptime(deadline_str.strip(), fmt)
            if fmt == "%H:%M":
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
            elif fmt == "%d.%m %H:%M":
                dt = dt.replace(year=now.year)
            dt = dt.replace(tzinfo=MOSCOW_TZ)
            if dt <= now and fmt == "%H:%M":
                dt += timedelta(days=1)
            return dt
        except ValueError:
            continue
    return None


# ---------- REMINDERS ----------

async def _reminder_coroutine(
    delay: float, user_id: int, task_id: str,
    topic_name: str, deadline_str: str, hours_before: int
):
    await asyncio.sleep(delay)
    status = task_status.get(task_id, {}).get(user_id, "")
    if status and not status.startswith("✅") and not status.startswith("❌"):
        task_link = tasks.get(task_id, {}).get("link", "")
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📂 Открыть сообщение", url=task_link)],
                [
                    InlineKeyboardButton(text="👀 Увидел", callback_data=f"s:{task_id}:{user_id}"),
                    InlineKeyboardButton(text="🎤 Записано", callback_data=f"d:{task_id}:{user_id}")
                ]
            ]
        )
        try:
            await bot.send_message(
                user_id,
                f"⏰ <b>Напоминание!</b>\n\n"
                f"До сдачи записи для серии «{topic_name}» осталось <b>{hours_before} ч.</b>\n"
                f"Дедлайн: <b>{deadline_str}</b>\n\n"
                f'<a href="{task_link}">📂 Открыть задание</a>',
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=keyboard
            )
            subtitles = subtitles_store.get(task_id)
            if subtitles:
                try:
                    await bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=subtitles["chat_id"],
                        message_id=subtitles["message_id"]
                    )
                except Exception as e:
                    logging.warning(f"Не удалось переслать субтитры актёру {user_id}: {e}")
        except Exception as e:
            await log_error(f"Ошибка отправки напоминания актёру {user_id}: {e}")


def schedule_reminders(task_id: str, user_id: int, deadline_str: str):
    dt = parse_deadline(deadline_str)
    if not dt:
        return
    now = datetime.now(MOSCOW_TZ)
    topic_name = tasks.get(task_id, {}).get("topic", "")
    for hours in [6, 2]:
        reminder_time = dt - timedelta(hours=hours)
        delay = (reminder_time - now).total_seconds()
        if delay > 0:
            key = (task_id, user_id, f"{hours}h")
            if key in reminder_store:
                reminder_store[key].cancel()
            t = asyncio.create_task(
                _reminder_coroutine(delay, user_id, task_id, topic_name, deadline_str, hours)
            )
            reminder_store[key] = t


# ---------- ALL DONE CHECK ----------

async def check_all_done(task_id: str):
    statuses = task_status.get(task_id, {})
    if not statuses:
        return
    logging.info(f"check_all_done: task_id={task_id}, statuses={statuses}")
    all_finished = all(
        s.startswith("✅") or s.startswith("❌")
        for s in statuses.values()
    )
    logging.info(f"check_all_done: all_finished={all_finished}, se_user_id={sound_engineers.get(task_id)}")
    if not all_finished:
        return

    se_user_id = sound_engineers.get(task_id)
    if not se_user_id:
        return

    if not SE_CHAT_ID:
        return

    topic_name = tasks.get(task_id, {}).get("topic", "Без темы")
    task_link = tasks.get(task_id, {}).get("link", "")

    se_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📂 Открыть задание", url=task_link)],
            [
                InlineKeyboardButton(text="✅ Серия сдана", callback_data=f"se_done:{task_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"se_reject:{task_id}")
            ]
        ]
    )

    subtitles = subtitles_store.get(task_id)
    logging.info(f"check_all_done: subtitles={subtitles}")
    if subtitles:
        try:
            await bot.copy_message(
                chat_id=SE_CHAT_ID,
                from_chat_id=subtitles["chat_id"],
                message_id=subtitles["message_id"]
            )
        except Exception as e:
            logging.warning(f"Не удалось переслать субтитры звукарю: {e}")
            await log_error(f"Не удалось переслать субтитры звукарю: {e}")

    assigned_msg_id = se_assigned_messages.get(task_id)
    if assigned_msg_id:
        try:
            await bot.delete_message(chat_id=SE_CHAT_ID, message_id=assigned_msg_id)
        except Exception as e:
            logging.warning(f"Не удалось удалить сообщение назначения: {e}")

    try:
        await bot.send_message(
            SE_CHAT_ID,
            f"🎚 Серия «{topic_name}» готова к сведению!\n\n"
            f"Все актёры завершили работу:\n\n{build_status(task_id)}",
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=se_keyboard
        )
    except Exception as e:
        await log_error(f"Ошибка отправки уведомления звукарю: {e}")

    file_msg_ids = []
    for (tid, uid), rec in recordings.items():
        if tid != task_id:
            continue
        nick = find_actor_by_id(uid) or f"id:{uid}"
        if rec["type"] == "file":
            try:
                label_msg = await bot.send_message(SE_CHAT_ID, f"📁 Файл от {nick}:")
                file_msg_ids.append(label_msg.message_id)
                file_msg = await bot.copy_message(
                    chat_id=SE_CHAT_ID,
                    from_chat_id=rec["from_chat"],
                    message_id=rec["msg_id"]
                )
                file_msg_ids.append(file_msg.message_id)
            except Exception as e:
                logging.warning(f"Не удалось переслать файл от {uid}: {e}")
                await log_error(f"Не удалось переслать файл от {uid}: {e}")
    se_file_messages[task_id] = file_msg_ids


# ---------- REFRESH HELPERS ----------

async def refresh_group_status(task_id: str):
    msg_id = status_messages.get(task_id)
    if msg_id:
        chat_id, _ = task_meta[task_id]
        try:
            await bot.edit_message_text(
                text=build_status(task_id),
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
        except Exception:
            pass

    se_info = se_status_messages.get(task_id)
    if se_info:
        topic_name = tasks.get(task_id, {}).get("topic", "Без темы")
        try:
            await bot.edit_message_text(
                chat_id=se_info["user_id"],
                message_id=se_info["msg_id"],
                text=f"🎚 Вы назначены звукорежиссёром\n\n"
                     f"📂 Тема: {topic_name}\n\n"
                     f"Статус: {build_status(task_id)}",
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
        except Exception:
            pass


async def refresh_actor_message(task_id: str, user_id: int, status: str):
    actor_msg = actor_messages.get((task_id, user_id))
    if not actor_msg:
        return
    task_link = tasks[task_id]["link"]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📂 Открыть сообщение", url=task_link)],
            [
                InlineKeyboardButton(text="👀 Увидел", callback_data=f"s:{task_id}:{user_id}"),
                InlineKeyboardButton(text="🎤 Записано", callback_data=f"d:{task_id}:{user_id}")
            ],
            [InlineKeyboardButton(text="❌ Не участвую", callback_data=f"sk:{task_id}:{user_id}")]
        ]
    )
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=actor_msg,
            text=(
                f"🎙 Вам пришло на озвучку\n\n"
                f"📂 Тема: {tasks[task_id]['topic']}\n\n"
                f"Статус: {status}"
            ),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=keyboard
        )
    except Exception:
        pass


# ---------- COMMANDS ----------

@dp.message(Command("ping"))
async def ping(message: types.Message):
    await message.answer("pong")


# ---------- START ----------

@dp.message(Command("start"))
@dp.message(F.text == "▶ Старт")
async def start(message: types.Message, state: FSMContext):
    nick = find_actor_by_id(message.from_user.id)
    if nick:
        await message.answer(f"Ты уже зарегистрирован как: {nick}", reply_markup=user_menu)
        return
    if not is_allowed(message.from_user.id):
        await message.answer("❌ У тебя нет доступа к боту.")
        return
    await state.set_state(Register.entering_nick)
    await message.answer("Введи свой ник актёра.")


@dp.message(Register.entering_nick)
async def save_nick(message: types.Message, state: FSMContext):
    nick = message.text.strip()
    save_actor(message.from_user.id, nick, message.from_user.username)
    await state.clear()
    await message.answer(f"Ник сохранён: {nick}", reply_markup=user_menu)


# ---------- MY NICK ----------

@dp.message(F.text == "🎭 Мой ник")
async def my_nick(message: types.Message):
    nick = find_actor_by_id(message.from_user.id)
    if nick:
        await message.answer(f"Твой ник: {nick}")
    else:
        await message.answer("Ты ещё не зарегистрирован.")


# ---------- CHANGE NICK ----------

@dp.message(F.text == "✏ Сменить ник")
async def change_nick(message: types.Message, state: FSMContext):
    await state.set_state(ChangeNick.entering_new_nick)
    await message.answer("Введи новый ник.")


@dp.message(ChangeNick.entering_new_nick)
async def process_change(message: types.Message, state: FSMContext):
    new_nick = message.text.strip()
    update_actor(message.from_user.id, new_nick)
    await state.clear()
    await message.answer(f"Ник изменён на: {new_nick}", reply_markup=user_menu)


# ---------- SEEN → ДЕДЛАЙН ----------

@dp.callback_query(F.data.regexp(r"^s:\d+:\d+$"))
async def seen(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    task_id, user_id = parts[1], parts[2]
    await state.set_state(SeenState.entering_deadline)
    await state.update_data(task_id=task_id, user_id=user_id)
    prompt = await callback.message.answer(
        "📅 Укажи дедлайн сдачи записи.\n\n"
        "Допустимые форматы (МСК):\n"
        "• <code>18:00</code> — сегодня/завтра\n"
        "• <code>25.03 18:00</code> — дата и время\n"
        "• <code>25.03.2025 18:00</code> — с годом",
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)
    await callback.answer()


@dp.message(SeenState.entering_deadline)
async def process_deadline(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    user_id = int(data["user_id"])
    prompt_msg_id = data.get("prompt_msg_id")
    deadline_str = message.text.strip()

    status = f"👀 до {deadline_str}"
    task_status[task_id][user_id] = status
    deadlines[(task_id, user_id)] = deadline_str

    await state.clear()

    if prompt_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=prompt_msg_id)
        except Exception as e:
            logging.warning(f"Не удалось удалить подсказку дедлайна: {e}")

    dt = parse_deadline(deadline_str)
    if dt:
        schedule_reminders(task_id, user_id, deadline_str)
        await message.answer(
            f"✅ Дедлайн сохранён: {deadline_str} (МСК)\n"
            f"🔔 Напоминания придут за 6 и 2 часа до дедлайна."
        )
    else:
        await message.answer(
            f"✅ Дедлайн сохранён: {deadline_str}\n"
            f"⚠️ Не удалось распознать формат — напоминания не настроены."
        )

    try:
        update_active_task_deadline(task_id, user_id, deadline_str)
        update_active_task_status(task_id, user_id, status)
    except Exception as e:
        await log_error(f"Ошибка сохранения дедлайна в Sheets: {e}")

    await refresh_actor_message(task_id, user_id, status)
    await refresh_group_status(task_id)


# ---------- DONE → ЗАПИСЬ ----------

@dp.callback_query(F.data.regexp(r"^d:\d+:\d+$"))
async def done(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    task_id, user_id = parts[1], parts[2]
    await state.set_state(DoneState.entering_recording)
    await state.update_data(task_id=task_id, user_id=user_id)
    await callback.message.answer("🎤 Отправь ссылку на запись или прикрепи файл:")
    await callback.answer()


@dp.message(DoneState.entering_recording)
async def process_recording(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    user_id = int(data["user_id"])
    nick = find_actor_by_id(user_id) or f"id:{user_id}"

    if message.document or message.audio or message.voice or message.video:
        file_obj = message.document or message.audio or message.voice or message.video
        recording_label = "📎 файл"
        recordings[(task_id, user_id)] = {
            "type": "file",
            "content": file_obj.file_id,
            "from_chat": message.chat.id,
            "msg_id": message.message_id
        }
    elif message.text and message.text.strip():
        recording_label = message.text.strip()
        recordings[(task_id, user_id)] = {
            "type": "link",
            "content": recording_label
        }
    else:
        await message.answer("Пожалуйста, отправь ссылку или прикрепи файл.")
        return

    if recordings[(task_id, user_id)]["type"] == "link":
        status = f'✅ <a href="{recording_label}">Ссылка</a>'
    else:
        status = f"✅ {recording_label}"
    task_status[task_id][user_id] = status

    await state.clear()
    await message.answer("✅ Запись принята!")

    chat_id, thread_id = task_meta.get(task_id, (None, None))
    if chat_id:
        try:
            await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=f"🎤 Запись от {nick}:")
            sent = await bot.copy_message(
                chat_id=chat_id, message_thread_id=thread_id,
                from_chat_id=message.chat.id, message_id=message.message_id
            )
            if recordings[(task_id, user_id)]["type"] == "file":
                chat_str = str(chat_id)
                chat_link_id = chat_str[4:]
                group_msg_link = f"https://t.me/c/{chat_link_id}/{sent.message_id}"
                status = f'✅ <a href="{group_msg_link}">Файл</a>'
                task_status[task_id][user_id] = status
        except Exception as e:
            logging.warning(f"Не удалось отправить запись в группу от {user_id}: {e}")
            await log_error(f"Ошибка отправки записи в группу от {user_id}: {e}")

    try:
        update_active_task_status(task_id, user_id, status)
    except Exception as e:
        await log_error(f"Ошибка обновления статуса в Sheets: {e}")

    await refresh_actor_message(task_id, user_id, status)
    await refresh_group_status(task_id)
    await check_all_done(task_id)


# ---------- SUBTITLES ----------

def is_subtitles(message: types.Message):
    if not message.document:
        return False
    name = message.document.file_name.lower()
    return name.endswith(".srt") or name.endswith(".ass") or name.endswith(".txt")


@dp.message(F.document, StateFilter(None))
async def subtitles_detect(message: types.Message):
    if not is_subtitles(message):
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎙 Назначить актёров", callback_data=f"assign:{message.message_id}")]
        ]
    )
    await message.reply("🎬 Панель серии", reply_markup=keyboard, disable_notification=True)
    subtitles_store[str(message.message_id)] = {
        "chat_id": message.chat.id,
        "message_id": message.message_id
    }


# ---------- ACTOR MENU ----------

def build_actor_menu(message_id):
    buttons = []
    actors = get_all_actors()
    for name in actors:
        selected = message_id in actor_selection and name in actor_selection[message_id]
        mark = "☑" if selected else "☐"
        buttons.append([
            InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"toggle:{message_id}:{name}")
        ])
    buttons.append([InlineKeyboardButton(text="🚀 Отправить задание", callback_data=f"send:{message_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.startswith("assign:"))
async def open_actor_menu(callback: types.CallbackQuery):
    message_id = int(callback.data.split(":")[1])
    actor_selection[message_id] = set()
    keyboard = build_actor_menu(message_id)
    await callback.message.edit_text("Выберите актёров:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_actor(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    _, message_id_str, name = parts
    message_id = int(message_id_str)
    if message_id not in actor_selection:
        actor_selection[message_id] = set()
    if name in actor_selection[message_id]:
        actor_selection[message_id].remove(name)
    else:
        actor_selection[message_id].add(name)
    keyboard = build_actor_menu(message_id)
    await callback.message.edit_reply_markup(reply_markup=keyboard)


# ---------- SOUND ENGINEER MENU ----------

def build_se_menu(task_id):
    buttons = []
    engineers = get_all_sound_engineers()
    selected = se_selection.get(task_id)
    for name in engineers:
        mark = "☑" if selected == name else "☐"
        buttons.append([
            InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"se_toggle:{task_id}:{name}")
        ])
    buttons.append([
        InlineKeyboardButton(text="✅ Назначить звукорежиссёра", callback_data=f"se_confirm:{task_id}")
    ])
    buttons.append([
        InlineKeyboardButton(text="🎚 Назначить Aniharu Myoko", callback_data=f"se_aniharu:{task_id}")
    ])
    buttons.append([
        InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"se_skip:{task_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.startswith("se_toggle:"))
async def se_toggle(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    _, task_id, name = parts
    se_selection[task_id] = name
    keyboard = build_se_menu(task_id)
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("se_confirm:"))
async def se_confirm(callback: types.CallbackQuery):
    task_id = callback.data.split(":", 1)[1]
    selected_nick = se_selection.get(task_id)
    if not selected_nick:
        await callback.answer("Выберите звукорежиссёра.")
        return

    se_user_id = get_se_id_by_nick(selected_nick)
    if not se_user_id:
        await callback.answer("Звукорежиссёр не найден.")
        return

    if not SE_CHAT_ID:
        await callback.answer("SE_CHAT_ID не настроен.")
        return

    sound_engineers[task_id] = se_user_id
    se_nicks[task_id] = selected_nick
    topic_name = tasks.get(task_id, {}).get("topic", "Без темы")
    task_link = tasks.get(task_id, {}).get("link", "")

    se_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📂 Открыть сообщение", url=task_link)],
            [
                InlineKeyboardButton(text="✅ Серия сдана", callback_data=f"se_done:{task_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"se_reject:{task_id}")
            ]
        ]
    )

    chat_id_task, _ = task_meta.get(task_id, (None, None))
    original_msg_id = tasks.get(task_id, {}).get("original")
    if chat_id_task and original_msg_id:
        try:
            await bot.copy_message(chat_id=SE_CHAT_ID, from_chat_id=chat_id_task, message_id=original_msg_id)
        except Exception as e:
            await log_error(f"Ошибка пересылки субтитров звукарю: {e}")

    try:
        msg = await bot.send_message(
            SE_CHAT_ID,
            f"🎚 Вы назначены звукорежиссёром\n\n"
            f"📂 Тема: {topic_name}\n\n"
            f"Статус: {build_status(task_id)}",
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=se_keyboard
        )
        se_status_messages[task_id] = {"user_id": SE_CHAT_ID, "msg_id": msg.message_id}
        se_assigned_messages[task_id] = msg.message_id
    except Exception as e:
        await log_error(f"Ошибка отправки сообщения звукарю: {e}")

    await callback.message.edit_text(f"✅ Звукорежиссёр назначен: {selected_nick}")
    await callback.answer()


@dp.callback_query(F.data.startswith("se_aniharu:"))
async def se_aniharu(callback: types.CallbackQuery):
    task_id = callback.data.split(":", 1)[1]
    se_nicks[task_id] = "Aniharu Myoko"
    await callback.message.edit_text("✅ Звукорежиссёр назначен: Aniharu Myoko")
    await callback.answer()


# ---------- SE SKIP / DONE / REJECT ----------

@dp.callback_query(F.data.startswith("se_skip:"))
async def se_skip(callback: types.CallbackQuery):
    task_id = callback.data.split(":", 1)[1]
    await callback.message.edit_text("⏭ Звукорежиссёр не назначен.")
    await callback.answer()


@dp.callback_query(F.data.startswith("se_done:"))
async def se_done(callback: types.CallbackQuery):
    task_id = callback.data.split(":", 1)[1]
    topic_name = tasks.get(task_id, {}).get("topic", "Без темы")
    task_link = tasks.get(task_id, {}).get("link", "")
    chat_id, thread_id = task_meta.get(task_id, (None, None))

    actors_done = []
    for user_id, status in task_status.get(task_id, {}).items():
        if status.startswith("✅"):
            nick = find_actor_by_id(user_id) or f"id:{user_id}"
            actors_done.append(nick)
    actors_line = ", ".join(actors_done) if actors_done else "—"
    se_nick = se_nicks.get(task_id, "—")

    summary = (
        f"✅ Серия «{topic_name}» сдана!\n\n"
        f"🎭 Актёры: {actors_line}\n"
        f"🎚 Звукорежиссёр: {se_nick}"
    )

    if chat_id:
        await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=summary)

    for msg_id in se_file_messages.get(task_id, []):
        try:
            await bot.delete_message(chat_id=SE_CHAT_ID, message_id=msg_id)
        except Exception as e:
            logging.warning(f"Не удалось удалить файл из чата звукаря: {e}")

    try:
        save_history(topic_name, actors_line, se_nick, task_link)
    except Exception as e:
        await log_error(f"Ошибка сохранения в историю: {e}")

    try:
        delete_active_task(task_id)
    except Exception as e:
        await log_error(f"Ошибка удаления активной задачи: {e}")

    await callback.message.edit_text(summary)
    await callback.answer()


@dp.callback_query(F.data.startswith("se_reject:"))
async def se_reject(callback: types.CallbackQuery):
    task_id = callback.data.split(":", 1)[1]
    topic_name = tasks.get(task_id, {}).get("topic", "Без темы")
    chat_id, thread_id = task_meta.get(task_id, (None, None))

    if chat_id:
        await bot.send_message(
            chat_id=chat_id, message_thread_id=thread_id,
            text=f"❌ Серия «{topic_name}» отклонена звукорежиссёром."
        )

    await callback.message.edit_text(f"❌ Серия «{topic_name}» отклонена.")
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
    topic_name = get_topic(chat_id, thread_id) or "Без темы"

    chat_str = str(chat_id)
    chat_link_id = chat_str[4:]
    message_link = f"https://t.me/c/{chat_link_id}/{message_id}"
    task_id = str(message_id)

    tasks[task_id] = {
        "chat": chat_id,
        "thread": thread_id,
        "link": message_link,
        "original": message_id,
        "topic": topic_name
    }
    task_status[task_id] = {}

    if str(message_id) in subtitles_store:
        subtitles_store[task_id] = subtitles_store.pop(str(message_id))

    for actor_name in selected:
        user_id = get_actor_id_by_nick(actor_name)
        task_status[task_id][user_id] = "⏳"

    status_msg = await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        reply_to_message_id=message_id,
        text=build_status(task_id),
        parse_mode="HTML",
        disable_notification=True
    )
    status_messages[task_id] = status_msg.message_id
    task_meta[task_id] = (chat_id, thread_id)

    for actor_name in selected:
        user_id = get_actor_id_by_nick(actor_name)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📂 Открыть сообщение", url=message_link)],
                [
                    InlineKeyboardButton(text="👀 Увидел", callback_data=f"s:{task_id}:{user_id}"),
                    InlineKeyboardButton(text="🎤 Записано", callback_data=f"d:{task_id}:{user_id}")
                ],
                [InlineKeyboardButton(text="❌ Не участвую", callback_data=f"sk:{task_id}:{user_id}")]
            ]
        )
        await bot.copy_message(chat_id=user_id, from_chat_id=chat_id, message_id=message_id)
        msg = await bot.send_message(
            user_id,
            f"🎙 Вам пришло на озвучку\n\n"
            f"📂 Тема: {topic_name}\n\n"
            f'<a href="{message_link}">📂 Открыть задание</a>\n\n'
            f"Статус: ⏳",
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=keyboard
        )
        actor_messages[(task_id, user_id)] = msg.message_id

        try:
            save_active_task_row(
                task_id, chat_id, thread_id, topic_name, message_link,
                message_id, status_msg.message_id,
                user_id, "⏳", "", msg.message_id
            )
        except Exception as e:
            await log_error(f"Ошибка сохранения задачи в Sheets: {e}")

    await callback.message.edit_text("✅ Задание отправлено актёрам.")

    se_selection[task_id] = None
    se_keyboard = build_se_menu(task_id)
    await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text="🎚 Выберите звукорежиссёра серии:",
        reply_markup=se_keyboard,
        disable_notification=True
    )


# ---------- SKIP ----------

@dp.callback_query(F.data.regexp(r"^sk:\d+:\d+$"))
async def skip(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    task_id, user_id = parts[1], int(parts[2])
    task_status[task_id][user_id] = "❌"

    try:
        update_active_task_status(task_id, user_id, "❌")
    except Exception as e:
        await log_error(f"Ошибка обновления статуса skip в Sheets: {e}")

    await refresh_actor_message(task_id, user_id, "❌")
    await refresh_group_status(task_id)
    await check_all_done(task_id)
    await callback.answer()


# ---------- TOPIC WATCHER ----------

@dp.message()
async def topic_watcher(message: types.Message):
    await ensure_topic_saved(message)


# ---------- WEBHOOK ----------

async def webhook_handler(request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logging.exception("Webhook error")
        await log_error(f"Webhook error: {e}")
    return web.Response(text="ok")


async def on_startup(app):
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    await restore_state()


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
