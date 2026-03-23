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

BASE_URL = "https://dragonvoices-bot.onrender.com"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = BASE_URL + WEBHOOK_PATH

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Московское время UTC+3
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
se_sheet = spreadsheet.worksheet("sound_engineers")  # ТЗ1 п.7: новый лист


# ---------- STORAGE ----------

tasks = {}
actor_selection = {}
task_status = {}
status_messages = {}
task_meta = {}
actor_messages = {}

# Новые хранилища
deadlines = {}           # (task_id, user_id) -> deadline_str
recordings = {}          # (task_id, user_id) -> {"type": "file"/"link", "content": ..., "from_chat": ..., "msg_id": ...}
sound_engineers = {}     # task_id -> user_id (int)
se_selection = {}        # task_id -> nick или None
se_status_messages = {}  # task_id -> {"user_id": ..., "msg_id": ...}
reminder_store = {}      # (task_id, user_id, label) -> asyncio.Task
subtitles_store = {}     # task_id -> {"chat_id": ..., "message_id": ...}  ТЗ1 п.5 / ТЗ2 п.3


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


# ---------- ACTORS ----------

def find_actor_by_id(user_id):
    rows = actors_sheet.get_all_records()
    for row in rows:
        if int(row["user_id"]) == user_id:
            return row["nick"]
    return None


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


# ---------- SOUND ENGINEERS (ТЗ1 п.7) ----------

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


# ---------- STATUS TEXT (ТЗ1 п.10, п.11: кликабельные ники) ----------

def build_status(task_id):
    lines = ["📊 Статусы актёров", ""]
    for user_id, status in task_status[task_id].items():
        nick = find_actor_by_id(user_id) or f"id:{user_id}"
        nick_link = f'<a href="tg://user?id={user_id}">{nick}</a>'
        # Если статус содержит http-ссылку — заменяем на кликабельное слово «Ссылка»
        if "http" in status:
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
    """Парсит строку дедлайна в datetime (московское время).
    Поддерживаемые форматы: ЧЧ:ММ / ДД.ММ ЧЧ:ММ / ДД.ММ.ГГГГ ЧЧ:ММ"""
    now = datetime.now(MOSCOW_TZ)
    formats = [
        "%d.%m.%Y %H:%M",
        "%d.%m %H:%M",
        "%H:%M",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(deadline_str.strip(), fmt)
            if fmt == "%H:%M":
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
            elif fmt == "%d.%m %H:%M":
                dt = dt.replace(year=now.year)
            dt = dt.replace(tzinfo=MOSCOW_TZ)
            # Если время уже прошло сегодня — переносим на следующий день
            if dt <= now and fmt == "%H:%M":
                dt += timedelta(days=1)
            return dt
        except ValueError:
            continue
    return None


# ---------- REMINDERS (ТЗ2: улучшенные напоминания) ----------

async def _reminder_coroutine(
    delay: float, user_id: int, task_id: str,
    topic_name: str, deadline_str: str, hours_before: int
):
    await asyncio.sleep(delay)

    # Отправляем только если задание ещё не завершено
    status = task_status.get(task_id, {}).get(user_id, "")
    if status and not status.startswith("✅") and not status.startswith("❌"):
        task_link = tasks.get(task_id, {}).get("link", "")

        # ТЗ2 п.1, п.2: кнопка + HTML-ссылка
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📂 Открыть сообщение", url=task_link)],
                [
                    InlineKeyboardButton(text="👀 Увидел", callback_data=f"seen:{task_id}:{user_id}"),
                    InlineKeyboardButton(text="🎤 Записано", callback_data=f"done:{task_id}:{user_id}")
                ]
            ]
        )

        # ТЗ2 п.4: структура напоминания
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

        # ТЗ2 п.3: прикреплять субтитры если есть
        subtitles = subtitles_store.get(task_id)
        if subtitles:
            await bot.send_message(user_id, "📄 Субтитры:")
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=subtitles["chat_id"],
                    message_id=subtitles["message_id"]
                )
            except Exception as e:
                logging.warning(f"Не удалось переслать субтитры актёру {user_id}: {e}")


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
            # Отменяем старое напоминание если есть
            if key in reminder_store:
                reminder_store[key].cancel()
            t = asyncio.create_task(
                _reminder_coroutine(delay, user_id, task_id, topic_name, deadline_str, hours)
            )
            reminder_store[key] = t


# ---------- ALL DONE CHECK (ТЗ1 п.3, п.4, п.6) ----------

async def check_all_done(task_id: str):
    """Проверяет, все ли актёры завершили работу. Если да — уведомляет звукорежиссёра."""
    statuses = task_status.get(task_id, {})
    if not statuses:
        return
    all_finished = all(
        s.startswith("✅") or s.startswith("❌")
        for s in statuses.values()
    )
    if not all_finished:
        return

    se_user_id = sound_engineers.get(task_id)
    if not se_user_id:
        return

    topic_name = tasks.get(task_id, {}).get("topic", "Без темы")
    task_link = tasks.get(task_id, {}).get("link", "")

    # ТЗ1 п.4: кнопки управления для звукорежиссёра
    se_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📂 Открыть задание", url=task_link)],
            [
                InlineKeyboardButton(text="✅ Серия сдана", callback_data=f"se_done:{task_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"se_reject:{task_id}")
            ]
        ]
    )

    # ТЗ1 п.9: HTML-ссылка в тексте + parse_mode
    await bot.send_message(
        se_user_id,
        f"🎚 Серия «{topic_name}» готова к сведению!\n\n"
        f"Все актёры завершили работу:\n\n{build_status(task_id)}\n\n"
        f'<a href="{task_link}">📂 Открыть задание</a>',
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=se_keyboard
    )

    # ТЗ1 п.3: пересылаем все файлы/записи звукарю ТОЛЬКО после завершения всех
    for (tid, uid), rec in recordings.items():
        if tid != task_id:
            continue
        nick = find_actor_by_id(uid) or f"id:{uid}"
        if rec["type"] == "file":
            await bot.send_message(se_user_id, f"📁 Файл от {nick}:")
            try:
                await bot.copy_message(
                    chat_id=se_user_id,
                    from_chat_id=rec["from_chat"],
                    message_id=rec["msg_id"]
                )
            except Exception as e:
                logging.warning(f"Не удалось переслать файл от {uid}: {e}")

    # ТЗ1 п.6: пересылаем субтитры звукарю
    subtitles = subtitles_store.get(task_id)
    logging.info(f"check_all_done: task_id={task_id}, subtitles_store keys={list(subtitles_store.keys())}, found={subtitles}")
    if subtitles:
        await bot.send_message(se_user_id, "📄 Субтитры:")
        try:
            await bot.copy_message(
                chat_id=se_user_id,
                from_chat_id=subtitles["chat_id"],
                message_id=subtitles["message_id"]
            )
        except Exception as e:
            logging.warning(f"Не удалось переслать субтитры звукарю: {e}")


# ---------- REFRESH HELPERS ----------

async def refresh_group_status(task_id: str):
    """Обновляет сообщение статуса в группе и у звукорежиссёра."""
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

    # Обновляем статус у звукорежиссёра
    se_info = se_status_messages.get(task_id)
    if se_info:
        topic_name = tasks.get(task_id, {}).get("topic", "Без темы")
        try:
            await bot.edit_message_text(
                chat_id=se_info["user_id"],
                message_id=se_info["msg_id"],
                text=f"📊 Статус серии «{topic_name}»:\n\n{build_status(task_id)}",
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
        except Exception:
            pass


async def refresh_actor_message(task_id: str, user_id: int, status: str):
    """Обновляет личное сообщение актёра с кнопками."""
    actor_msg = actor_messages.get((task_id, user_id))
    if not actor_msg:
        return
    task_link = tasks[task_id]["link"]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📂 Открыть сообщение", url=task_link)],
            [
                InlineKeyboardButton(text="👀 Увидел", callback_data=f"seen:{task_id}:{user_id}"),
                InlineKeyboardButton(text="🎤 Записано", callback_data=f"done:{task_id}:{user_id}")
            ],
            [InlineKeyboardButton(text="❌ Не участвую", callback_data=f"skip:{task_id}:{user_id}")]
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
        await message.answer(
            f"Ты уже зарегистрирован как: {nick}",
            reply_markup=user_menu
        )
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

@dp.callback_query(F.data.startswith("seen:"))
async def seen(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    task_id, user_id = parts[1], parts[2]
    await state.set_state(SeenState.entering_deadline)
    await state.update_data(task_id=task_id, user_id=user_id)
    await callback.message.answer(
        "📅 Укажи дедлайн сдачи записи.\n\n"
        "Допустимые форматы (МСК):\n"
        "• <code>18:00</code> — сегодня/завтра\n"
        "• <code>25.03 18:00</code> — дата и время\n"
        "• <code>25.03.2025 18:00</code> — с годом",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(SeenState.entering_deadline)
async def process_deadline(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    user_id = int(data["user_id"])
    deadline_str = message.text.strip()

    status = f"👀 до {deadline_str}"
    task_status[task_id][user_id] = status
    deadlines[(task_id, user_id)] = deadline_str

    await state.clear()

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

    await refresh_actor_message(task_id, user_id, status)
    await refresh_group_status(task_id)


# ---------- DONE → ЗАПИСЬ (ТЗ1 п.1, п.2) ----------

@dp.callback_query(F.data.startswith("done:"))
async def done(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    task_id, user_id = parts[1], parts[2]
    await state.set_state(DoneState.entering_recording)
    await state.update_data(task_id=task_id, user_id=user_id)
    await callback.message.answer(
        "🎤 Отправь ссылку на запись или прикрепи файл:"
    )
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

    status = f"✅ {recording_label}"
    task_status[task_id][user_id] = status

    await state.clear()
    await message.answer("✅ Запись принята!")

    # ТЗ1 п.2: отправляем запись в рабочую беседу (группу)
    chat_id, thread_id = task_meta.get(task_id, (None, None))
    if chat_id:
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=f"🎤 Запись от {nick}:"
            )
            await bot.copy_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
        except Exception as e:
            logging.warning(f"Не удалось отправить запись в группу от {user_id}: {e}")

    await refresh_actor_message(task_id, user_id, status)
    await refresh_group_status(task_id)
    # ТЗ1 п.1: звукорежиссёр НЕ получает файл сразу — только в check_all_done
    await check_all_done(task_id)


# ---------- SUBTITLES (ТЗ1 п.5) ----------

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
            [
                InlineKeyboardButton(
                    text="🎙 Назначить актёров",
                    callback_data=f"assign:{message.message_id}"
                )
            ]
        ]
    )
    reply = await message.reply("🎬 Панель серии", reply_markup=keyboard)

    # ТЗ1 п.5: сохраняем субтитры — привязываем к message_id панели (task_id будет известен после send_task)
    # Временно сохраняем под ключом исходного message_id субтитров
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


# ---------- SOUND ENGINEER MENU (ТЗ1 п.8) ----------

def build_se_menu(task_id):
    buttons = []
    # ТЗ1 п.8: используем sound_engineers лист
    engineers = get_all_sound_engineers()
    selected = se_selection.get(task_id)
    for name in engineers:
        mark = "☑" if selected == name else "☐"
        buttons.append([
            InlineKeyboardButton(
                text=f"{mark} {name}",
                callback_data=f"se_toggle:{task_id}:{name}"
            )
        ])
    buttons.append([
        InlineKeyboardButton(
            text="✅ Назначить звукорежиссёра",
            callback_data=f"se_confirm:{task_id}"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.startswith("se_toggle:"))
async def se_toggle(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    _, task_id, name = parts
    se_selection[task_id] = name  # одиночный выбор
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
    # ТЗ1 п.8: используем get_se_id_by_nick вместо get_actor_id_by_nick
    se_user_id = get_se_id_by_nick(selected_nick)
    if not se_user_id:
        await callback.answer("Звукорежиссёр не найден.")
        return

    sound_engineers[task_id] = se_user_id
    topic_name = tasks.get(task_id, {}).get("topic", "Без темы")
    task_link = tasks.get(task_id, {}).get("link", "")

    # Кнопки в стиле актёрского сообщения
    se_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📂 Открыть сообщение", url=task_link)],
            [
                InlineKeyboardButton(text="✅ Серия сдана", callback_data=f"se_done:{task_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"se_reject:{task_id}")
            ]
        ]
    )

    # Сначала пересылаем само сообщение с субтитрами (как актёрам copy_message)
    chat_id_task, thread_id_task = task_meta.get(task_id, (None, None))
    original_msg_id = tasks.get(task_id, {}).get("original")
    if chat_id_task and original_msg_id:
        await bot.copy_message(
            chat_id=se_user_id,
            from_chat_id=chat_id_task,
            message_id=original_msg_id
        )

    # Затем статусное сообщение с кнопками — точь-в-точь как у актёров
    msg = await bot.send_message(
        se_user_id,
        f"🎚 Вы назначены звукорежиссёром\n\n"
        f"📂 Тема: {topic_name}\n\n"
        f"Статус: {build_status(task_id)}",
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=se_keyboard
    )
    se_status_messages[task_id] = {"user_id": se_user_id, "msg_id": msg.message_id}

    await callback.message.edit_text(f"✅ Звукорежиссёр назначен: {selected_nick}")
    await callback.answer()


# ---------- SE DONE / SE REJECT (ТЗ1 п.4) ----------

@dp.callback_query(F.data.startswith("se_done:"))
async def se_done(callback: types.CallbackQuery):
    task_id = callback.data.split(":", 1)[1]
    topic_name = tasks.get(task_id, {}).get("topic", "Без темы")
    chat_id, thread_id = task_meta.get(task_id, (None, None))

    if chat_id:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"✅ Серия «{topic_name}» сдана звукорежиссёром!"
        )

    await callback.message.edit_text(f"✅ Серия «{topic_name}» отмечена как сданная.")
    await callback.answer()


@dp.callback_query(F.data.startswith("se_reject:"))
async def se_reject(callback: types.CallbackQuery):
    task_id = callback.data.split(":", 1)[1]
    topic_name = tasks.get(task_id, {}).get("topic", "Без темы")
    chat_id, thread_id = task_meta.get(task_id, (None, None))

    if chat_id:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
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

    # ТЗ1 п.5: переносим субтитры с временного ключа на task_id
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
        parse_mode="HTML"  # ТЗ1 п.10: кликабельные ники
    )
    status_messages[task_id] = status_msg.message_id
    task_meta[task_id] = (chat_id, thread_id)

    for actor_name in selected:
        user_id = get_actor_id_by_nick(actor_name)
        # ТЗ1 п.9: кликабельная ссылка в кнопке (уже была) + HTML parse_mode
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📂 Открыть сообщение", url=message_link)],
                [
                    InlineKeyboardButton(text="👀 Увидел", callback_data=f"seen:{task_id}:{user_id}"),
                    InlineKeyboardButton(text="🎤 Записано", callback_data=f"done:{task_id}:{user_id}")
                ],
                [InlineKeyboardButton(text="❌ Не участвую", callback_data=f"skip:{task_id}:{user_id}")]
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

    await callback.message.edit_text("✅ Задание отправлено актёрам.")

    # Показываем выбор звукорежиссёра
    se_selection[task_id] = None
    se_keyboard = build_se_menu(task_id)
    await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text="🎚 Выберите звукорежиссёра серии:",
        reply_markup=se_keyboard
    )


# ---------- SKIP ----------

@dp.callback_query(F.data.startswith("skip:"))
async def skip(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    task_id, user_id = parts[1], int(parts[2])
    task_status[task_id][user_id] = "❌"
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
