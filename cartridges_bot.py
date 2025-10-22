import asyncio
import sqlite3
import os
import json
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

import gspread
from oauth2client.service_account import ServiceAccountCredentials


# === 🔧 Налаштування ===
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GSHEET_ID = os.getenv("GSHEET_ID")
DB_PATH = os.path.join(os.path.dirname(__file__), "cartridges.db")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# === 🗓️ Форматування дат ===
def normalize_date(date_str: str) -> str:
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return date_str.strip()


def current_date() -> str:
    return datetime.now().strftime("%d.%m.%Y")


# === 📁 Ініціалізація бази ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cartridges(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_received TEXT,
            department TEXT,
            status TEXT,
            date_sent TEXT,
            date_returned TEXT,
            date_given TEXT,
            batch_id INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS batches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            status TEXT
        )
    """)
    # гарантуємо хоч одну активну партію
    cur.execute("SELECT id FROM batches WHERE status='active'")
    if not cur.fetchone():
        cur.execute("INSERT INTO batches (created_at, status) VALUES (?, 'active')", (current_date(),))
    conn.commit()
    conn.close()


# === 🔗 Google Sheets ===
def init_gsheets():
    try:
        key_data = os.getenv("GOOGLE_SERVICE_KEY")
        if not key_data or not GSHEET_ID:
            print("⚠️ GOOGLE_SERVICE_KEY або GSHEET_ID не задані.")
            return None

        creds_dict = json.loads(key_data)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GSHEET_ID)
        return sheet
    except Exception as e:
        print("⚠️ Помилка підключення до Google Sheets:", e)
        return None


def setup_gsheet_format(ws):
    headers = [
        "ID", "Дата вилучення", "Відділ", "Статус",
        "Дата відправлення", "Дата повернення", "Дата видачі", "№ партії"
    ]
    ws.clear()
    ws.append_row(headers)
    ws.format("A1:H1", {
        "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
        "textFormat": {"bold": True},
        "horizontalAlignment": "CENTER"
    })
    ws.freeze(rows=1)


def sync_to_sheets():
    sheet = init_gsheets()
    if sheet is None:
        return

    # читаємо БД
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, date_received, department, status,
               date_sent, date_returned, date_given, batch_id
        FROM cartridges
        ORDER BY batch_id ASC, id ASC
    """)
    rows = cur.fetchall()
    conn.close()

    try:
        try:
            ws = sheet.worksheet("Cartridges")
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(title="Cartridges", rows="2000", cols="8")
            setup_gsheet_format(ws)

        ws.clear()
        headers = [
            "ID", "Дата вилучення", "Відділ", "Статус",
            "Дата відправлення", "Дата повернення", "Дата видачі", "№ партії"
        ]
        ws.append_row(headers)
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
        ws.format("A1:H1", {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER"})
        ws.freeze(rows=1)
        print("✅ Дані синхронізовано з Google Sheets")
    except Exception as e:
        print("⚠️ Помилка синхронізації:", e)


# === Службові ===
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


# === 🏠 Головне меню ===
def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Додати", callback_data="menu_add")
    kb.button(text="👁️ Переглянути партії", callback_data="menu_view")
    kb.button(text="🔧 Змінити статус", callback_data="menu_status")
    kb.button(text="🆕 Нова партія", callback_data="menu_newbatch")
    kb.adjust(2)
    return kb.as_markup()


async def show_main_menu(message: types.Message):
    await message.answer("🛠️ *Адмін-меню:*\nВибери дію 👇",
                         parse_mode="Markdown", reply_markup=main_menu_kb())


# === /start ===
@dp.message(Command("start"))
async def start(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ У вас немає доступу.")
    await show_main_menu(message)


# === Меню кнопок (роутер) ===
@dp.callback_query(F.data.startswith("menu_"))
async def menu_actions(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("⛔ Немає доступу", show_alert=True)

    action = callback.data.split("_")[1]
    if action == "add":
        await start_add_flow(callback, state)
    elif action == "view":
        await view_batches(callback)
    elif action == "status":
        await show_status_menu(callback)
    elif action == "newbatch":
        await new_batch(callback)


# === ➕ Додати (спочатку вибір партії) ===
class AddFlow(StatesGroup):
    choosing_batch = State()
    entering_data = State()
    chosen_batch_id = State()


async def start_add_flow(callback: types.CallbackQuery, state: FSMContext):
    # список партій
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, created_at, status FROM batches ORDER BY id DESC")
    batches = cur.fetchall()
    conn.close()

    kb = InlineKeyboardBuilder()
    for bid, created, status in batches:
        emoji = "🟢" if status == "active" else "⚪"
        kb.button(text=f"{emoji} Партія {bid} ({created})", callback_data=f"select_batch_{bid}")
    kb.button(text="🆕 Створити нову партію", callback_data="create_batch_for_add")
    kb.button(text="🏠 Головне меню", callback_data="go_home")
    kb.adjust(1)

    await state.set_state(AddFlow.choosing_batch)
    await callback.message.edit_text("🔹 Вибери партію, до якої додати картридж:", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "go_home", AddFlow.choosing_batch)
async def go_home_from_add(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🏠 Головне меню", reply_markup=main_menu_kb())


@dp.callback_query(F.data.startswith("select_batch_"), AddFlow.choosing_batch)
async def choose_batch_for_add(callback: types.CallbackQuery, state: FSMContext):
    batch_id = int(callback.data.split("_")[2])
    await state.update_data(chosen_batch_id=batch_id)
    await state.set_state(AddFlow.entering_data)
    await callback.message.edit_text(
        f"🗂️ Обрано партію #{batch_id}\nВведи дані у форматі:\n`ДД.ММ.РРРР, Відділення`",
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "create_batch_for_add", AddFlow.choosing_batch)
async def create_new_batch_for_add(callback: types.CallbackQuery, state: FSMContext):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO batches (created_at, status) VALUES (?, 'active')", (current_date(),))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    await state.update_data(chosen_batch_id=new_id)
    await state.set_state(AddFlow.entering_data)
    await callback.message.edit_text(
        f"🆕 Створено та обрано партію #{new_id}\nВведи дані у форматі:\n`ДД.ММ.РРРР, Відділення`",
        parse_mode="Markdown"
    )


@dp.message(AddFlow.entering_data, F.text)
async def add_save_info(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Немає доступу.")

    txt = msg.text.strip()
    if "," not in txt:
        return await msg.reply("❌ Формат: `20.10.2025, Бухгалтерія`", parse_mode="Markdown")

    date_str, dept = map(str.strip, txt.split(",", 1))
    date_received = normalize_date(date_str)

    data = await state.get_data()
    batch_id = data.get("chosen_batch_id")
    if not batch_id:
        await state.clear()
        return await msg.answer("⚠️ Партію не вибрано. Спробуйте ще раз.")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO cartridges (date_received, department, status, batch_id)
        VALUES (?, ?, ?, ?)
    """, (date_received, dept, "⛔ Вилучено у працівника", batch_id))
    conn.commit()
    conn.close()

    sync_to_sheets()
    await state.clear()
    await msg.answer(f"✅ Додано картридж до партії #{batch_id}")
    await show_main_menu(msg)


# === 👁️ Перегляд партій ===
async def view_batches(callback: types.CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT b.id, b.created_at, b.status, COUNT(c.id)
        FROM batches b
        LEFT JOIN cartridges c ON b.id = c.batch_id
        GROUP BY b.id
        ORDER BY b.id DESC
    """)
    batches = cur.fetchall()
    conn.close()

    if not batches:
        return await callback.message.edit_text("📦 Партій ще немає.", reply_markup=main_menu_kb())

    text = "📦 *Список партій:*\n\n"
    kb = InlineKeyboardBuilder()
    for bid, created, status, cnt in batches:
        emoji = "🟢" if status == "active" else "⚪"
        text += f"{emoji} Партія {bid} | 📅 {created} | 🖨️ {cnt} шт.\n"
        kb.button(text=f"📋 Відкрити {bid}", callback_data=f"open_batch_{bid}")
        kb.button(text=f"🗑️ Видалити {bid}", callback_data=f"ask_del_batch_{bid}")
    kb.button(text="🏠 Головне меню", callback_data="go_home_plain")
    kb.adjust(1)
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "go_home_plain")
async def go_home_plain(callback: types.CallbackQuery):
    await callback.message.edit_text("🏠 Головне меню", reply_markup=main_menu_kb())


@dp.callback_query(F.data.startswith("open_batch_"))
async def open_batch(callback: types.CallbackQuery):
    batch_id = int(callback.data.split("_")[2])

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, created_at, status FROM batches WHERE id=?", (batch_id,))
    b = cur.fetchone()
    cur.execute("""
        SELECT id, date_received, department, status, date_sent, date_returned, date_given
        FROM cartridges
        WHERE batch_id=?
        ORDER BY id ASC
    """, (batch_id,))
    carts = cur.fetchall()
    conn.close()

    if not b:
        return await callback.answer("Партію не знайдено", show_alert=True)

    header = f"📦 *Партія #{b[0]}* • 📅 {b[1]} • Статус: {b[2]}"
    if not carts:
        body = "\n\n(Записів немає)"
    else:
        def row_text(r):
            cid, d_recv, dept, status, d_sent, d_ret, d_giv = r
            return f"#{cid} • {dept} • {status}\n🗓 {d_recv or '—'} | → {d_sent or '—'} | ⤴ {d_ret or '—'} | ✔ {d_giv or '—'}"
        body = "\n\n" + "\n".join(row_text(r) for r in carts)

    kb = InlineKeyboardBuilder()
    for r in carts:
        cid = r[0]
        kb.button(text=f"🔧 Статус #{cid}", callback_data=f"edit_cart_{cid}")
        kb.button(text=f"❌ Видалити #{cid}", callback_data=f"ask_del_cart_{cid}")
    kb.button(text="⬅️ Назад до списку партій", callback_data="menu_view")
    kb.button(text="🏠 Головне меню", callback_data="go_home_plain")
    kb.adjust(2)

    await callback.message.edit_text(header + body, parse_mode="Markdown", reply_markup=kb.as_markup())


# === 🗑️ Видалення партії (з підтвердженням) ===
@dp.callback_query(F.data.startswith("ask_del_batch_"))
async def ask_del_batch(callback: types.CallbackQuery):
    batch_id = int(callback.data.split("_")[-1])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Так, видалити", callback_data=f"del_batch_{batch_id}_yes")
    kb.button(text="❌ Ні", callback_data="menu_view")
    kb.adjust(2)
    await callback.message.edit_text(
        f"⚠️ Ви впевнені, що хочете видалити *партію #{batch_id}* разом із усіма картриджами?",
        parse_mode="Markdown",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data.startswith("del_batch_"))
async def del_batch(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    batch_id = int(parts[2])
    confirm = parts[3]
    if confirm != "yes":
        return await view_batches(callback)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM cartridges WHERE batch_id=?", (batch_id,))
    cur.execute("DELETE FROM batches WHERE id=?", (batch_id,))
    conn.commit()
    conn.close()
    sync_to_sheets()
    await callback.message.edit_text(f"🗑️ Партію #{batch_id} видалено.")
    await view_batches(callback)


# === ❌ Видалення картриджа (з підтвердженням) ===
@dp.callback_query(F.data.startswith("ask_del_cart_"))
async def ask_del_cart(callback: types.CallbackQuery):
    cid = int(callback.data.split("_")[-1])

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT batch_id FROM cartridges WHERE id=?", (cid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return await callback.answer("Запис не знайдено", show_alert=True)
    batch_id = row[0]

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Так, видалити", callback_data=f"del_cart_{cid}_{batch_id}_yes")
    kb.button(text="❌ Ні", callback_data=f"open_batch_{batch_id}")
    kb.adjust(2)
    await callback.message.edit_text(
        f"⚠️ Видалити *картридж #{cid}* з партії #{batch_id}?",
        parse_mode="Markdown",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data.startswith("del_cart_"))
async def del_cart(callback: types.CallbackQuery):
    _, _, cid, batch_id, confirm = callback.data.split("_")
    cid = int(cid); batch_id = int(batch_id)
    if confirm != "yes":
        return await open_batch(callback)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM cartridges WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    sync_to_sheets()
    await callback.message.edit_text(f"✅ Картридж #{cid} видалено.")
    await open_batch(callback)


# === 🔧 Зміна статусу (через перегляд партії) ===
def status_kb_for_cart(cid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="⛔ Вилучено", callback_data=f"set_{cid}_s1")
    kb.button(text="🔄 Відправлено", callback_data=f"set_{cid}_s2")
    kb.button(text="✅ Прибуло", callback_data=f"set_{cid}_s3")
    kb.button(text="📦 Видано", callback_data=f"set_{cid}_s4")
    kb.button(text="⬅️ Назад", callback_data=f"back_cart_{cid}")
    kb.adjust(2)
    return kb.as_markup()


@dp.callback_query(F.data.startswith("edit_cart_"))
async def edit_cart(callback: types.CallbackQuery):
    cid = int(callback.data.split("_")[2])
    await callback.message.edit_reply_markup(reply_markup=status_kb_for_cart(cid))


@dp.callback_query(F.data.startswith("back_cart_"))
async def back_cart(callback: types.CallbackQuery):
    cid = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT batch_id FROM cartridges WHERE id=?", (cid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return await callback.answer("Запис не знайдено", show_alert=True)
    bid = row[0]
    await open_batch(callback)


@dp.callback_query(F.data.startswith("set_"))
async def set_status(callback: types.CallbackQuery):
    cid, code = int(callback.data.split("_")[1]), callback.data.split("_")[2]
    status_map = {
        "s1": ("⛔ Вилучено у працівника", "date_received"),
        "s2": ("🔄 Відправлено на фірму", "date_sent"),
        "s3": ("✅ Прибуло з фірми", "date_returned"),
        "s4": ("📦 Видано працівнику", "date_given"),
    }
    new_status, field = status_map[code]
    today = current_date()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"UPDATE cartridges SET status=?, {field}=? WHERE id=?", (new_status, today, cid))
    cur.execute("SELECT batch_id FROM cartridges WHERE id=?", (cid,))
    row = cur.fetchone()
    conn.commit()
    conn.close()

    sync_to_sheets()
    if row:
        await open_batch(callback)
    else:
        await callback.message.edit_text("✅ Статус змінено.")


# === 🔧 Змінити статус (окремий пункт меню) ===
async def show_status_menu(callback: types.CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT b.id, b.created_at, COUNT(c.id)
        FROM batches b
        LEFT JOIN cartridges c ON b.id = c.batch_id
        GROUP BY b.id ORDER BY b.id DESC
    """)
    batches = cur.fetchall()
    conn.close()

    if not batches:
        return await callback.message.edit_text("📦 Партій ще немає.", reply_markup=main_menu_kb())

    kb = InlineKeyboardBuilder()
    for b in batches:
        kb.button(text=f"🗂️ Партія {b[0]} ({b[2]} шт.)", callback_data=f"status_batch_{b[0]}")
    kb.button(text="🏠 Головне меню", callback_data="go_home_plain")
    kb.adjust(1)
    await callback.message.edit_text("🔧 Вибери партію:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("status_batch_"))
async def status_batch(callback: types.CallbackQuery):
    batch_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, department, status FROM cartridges WHERE batch_id=?", (batch_id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return await callback.message.edit_text(f"📭 У партії {batch_id} немає картриджів.", reply_markup=main_menu_kb())

    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"#{r[0]} | {r[1]} ({r[2]})", callback_data=f"edit_cart_{r[0]}")
    kb.button(text="⬅️ Назад", callback_data="menu_status")
    kb.button(text="🏠 Головне меню", callback_data="go_home_plain")
    kb.adjust(1)
    await callback.message.edit_text(f"🔧 Партія {batch_id} — вибери картридж:", reply_markup=kb.as_markup())


# === 🆕 Нова партія (окремий пункт меню) ===
async def new_batch(callback: types.CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # закриваємо активні та створюємо нову як активну
    cur.execute("UPDATE batches SET status='closed' WHERE status='active'")
    cur.execute("INSERT INTO batches (created_at, status) VALUES (?, 'active')", (current_date(),))
    conn.commit()
    conn.close()

    sync_to_sheets()
    await callback.message.edit_text("📦 Створено нову партію!", reply_markup=main_menu_kb())


# === 🚀 Запуск ===
async def main():
    init_db()
    print("🤖 Бот запущено…")
    sync_to_sheets()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
