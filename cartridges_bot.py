import asyncio
import sqlite3
import os
import json
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openpyxl import Workbook
import gspread
from oauth2client.service_account import ServiceAccountCredentials


# === 🔧 Налаштування ===
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GSHEET_ID = os.getenv("GSHEET_ID")
DB_PATH = os.path.join(os.path.dirname(__file__), "cartridges.db")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# === 🗓️ Форматування дат ===
def normalize_date(date_str):
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return date_str.strip()


def current_date():
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
    cur.execute("SELECT id FROM batches WHERE status='active'")
    if not cur.fetchone():
        cur.execute("INSERT INTO batches (created_at, status) VALUES (?, 'active')", (current_date(),))
    conn.commit()
    conn.close()


# === 🔗 Google Sheets ===
def init_gsheets():
    try:
        key_data = os.getenv("GOOGLE_SERVICE_KEY")
        if not key_data:
            print("⚠️ GOOGLE_SERVICE_KEY не знайдено.")
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
        print("✅ Підключено до Google Sheets")
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
    print("🎨 Таблицю оформлено")


def sync_to_sheets():
    sheet = init_gsheets()
    if sheet is None:
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, date_received, department, status,
               date_sent, date_returned, date_given, batch_id
        FROM cartridges ORDER BY id ASC
    """)
    rows = cur.fetchall()
    conn.close()

    try:
        try:
            ws = sheet.worksheet("Cartridges")
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(title="Cartridges", rows="1000", cols="8")
            setup_gsheet_format(ws)

        ws.clear()
        headers = [
            "ID", "Дата вилучення", "Відділ", "Статус",
            "Дата відправлення", "Дата повернення", "Дата видачі", "№ партії"
        ]
        ws.append_row(headers)
        if rows:
            ws.append_rows(rows)
        ws.format("A1:H1", {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER"})
        ws.freeze(rows=1)
        print("✅ Дані синхронізовано з Google Sheets")
    except Exception as e:
        print("⚠️ Помилка синхронізації:", e)


# === Службові ===
def is_admin(uid):
    return uid == ADMIN_ID


# === 🏠 Головне меню ===
async def show_main_menu(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Додати", callback_data="menu_add")
    kb.button(text="👁️ Переглянути партії", callback_data="menu_view")
    kb.button(text="🔧 Змінити статус", callback_data="menu_status")
    kb.button(text="🆕 Нова партія", callback_data="menu_newbatch")
    kb.button(text="📤 Експорт у Excel", callback_data="menu_export")
    kb.adjust(2)
    await message.answer("🛠️ *Адмін-меню:*\nВибери дію 👇",
                         parse_mode="Markdown", reply_markup=kb.as_markup())


# === /start ===
@dp.message(Command("start"))
async def start(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ У вас немає доступу.")
    await show_main_menu(message)


# === Меню кнопок ===
@dp.callback_query(F.data.startswith("menu_"))
async def menu_actions(callback: types.CallbackQuery):
    action = callback.data.split("_")[1]
    if action == "add":
        await add_cartridge(callback.message)
    elif action == "view":
        await view_all(callback.message)
    elif action == "status":
        await show_status_menu(callback.message)
    elif action == "newbatch":
        await new_batch(callback.message)
    elif action == "export":
        await export_excel(callback.message)
    elif action == "home":
        await show_main_menu(callback.message)


# === ➕ Додати картридж ===
async def add_cartridge(message: types.Message):
    await message.answer("Введи дані у форматі:\n`Дата вилучення, Відділення`\n"
                         "Наприклад: `20.10.2025, Бухгалтерія`", parse_mode="Markdown")

    @dp.message(F.text)
    async def save_info(msg: types.Message):
        try:
            date_received, dept = map(str.strip, msg.text.split(","))
        except:
            return await msg.reply("❌ Формат: 20.10.2025, Відділення")

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT id FROM batches WHERE status='active'")
        batch = cur.fetchone()
        if not batch:
            cur.execute("INSERT INTO batches (created_at, status) VALUES (?, 'active')", (current_date(),))
            conn.commit()
            cur.execute("SELECT id FROM batches WHERE status='active'")
            batch = cur.fetchone()
        batch_id = batch[0]

        cur.execute("""
            INSERT INTO cartridges (date_received, department, status, batch_id)
            VALUES (?, ?, ?, ?)
        """, (normalize_date(date_received), dept, "⛔ Вилучено у працівника", batch_id))
        conn.commit()
        conn.close()

        sync_to_sheets()
        await msg.answer("✅ Картридж додано!")
        await show_main_menu(msg)


# === 👁️ Перегляд партій ===
async def view_all(message: types.Message):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT b.id, b.created_at, COUNT(c.id)
        FROM batches b
        LEFT JOIN cartridges c ON b.id = c.batch_id
        GROUP BY b.id ORDER BY b.id
    """)
    batches = cur.fetchall()
    conn.close()

    if not batches:
        await message.answer("📦 Партій ще немає.")
        return await show_main_menu(message)

    text = "📦 *Список партій:*\n\n"
    for b in batches:
        text += f"🗂️ *Партія {b[0]}* | 📅 {b[1]} | 🖨️ {b[2]} картр.\n───────────────\n"

    kb = InlineKeyboardBuilder()
    for b in batches:
        kb.button(text=f"📋 Партія {b[0]}", callback_data=f"batch_{b[0]}")
    kb.button(text="🏠 Головне меню", callback_data="menu_home")
    kb.adjust(1)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb.as_markup())


# === 🔧 Зміна статусів ===
async def show_status_menu(message: types.Message):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT b.id, b.created_at, COUNT(c.id)
        FROM batches b
        LEFT JOIN cartridges c ON b.id = c.batch_id
        GROUP BY b.id ORDER BY b.id
    """)
    batches = cur.fetchall()
    conn.close()

    if not batches:
        await message.answer("📦 Партій ще немає.")
        return await show_main_menu(message)

    kb = InlineKeyboardBuilder()
    for b in batches:
        kb.button(text=f"🗂️ Партія {b[0]} ({b[2]} шт.)", callback_data=f"editbatch_{b[0]}")
    kb.button(text="🏠 Головне меню", callback_data="menu_home")
    kb.adjust(1)
    await message.answer("🔧 Вибери партію для редагування:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("editbatch_"))
async def edit_batch(callback: types.CallbackQuery):
    batch_id = int(callback.data.split("_")[1])
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, department, status FROM cartridges WHERE batch_id=?", (batch_id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await callback.message.edit_text(f"📭 У партії {batch_id} немає картриджів.")
        return

    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"#{r[0]} | {r[1]} ({r[2]})", callback_data=f"choose_{r[0]}")
    kb.button(text="⬅️ Назад", callback_data="menu_status")
    kb.button(text="🏠 Головне меню", callback_data="menu_home")
    kb.adjust(1)
    await callback.message.edit_text(f"🔧 Партія {batch_id} — вибери картридж:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("choose_"))
async def choose_cartridge(callback: types.CallbackQuery):
    cid = int(callback.data.split("_")[1])
    kb = InlineKeyboardBuilder()
    statuses = [
        ("⛔ Вилучено у працівника", "s1"),
        ("🔄 Відправлено на фірму", "s2"),
        ("✅ Прибуло з фірми", "s3"),
        ("📦 Видано працівнику", "s4")
    ]
    for text, code in statuses:
        kb.button(text=text, callback_data=f"set_{cid}_{code}")
    kb.button(text="🏠 Головне меню", callback_data="menu_home")
    kb.adjust(2)
    await callback.message.edit_text(f"🖋️ Вибери новий статус для #{cid}:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("set_"))
async def set_status(callback: types.CallbackQuery):
    cid, code = int(callback.data.split("_")[1]), callback.data.split("_")[2]
    status_map = {
        "s1": "⛔ Вилучено у працівника",
        "s2": "🔄 Відправлено на фірму",
        "s3": "✅ Прибуло з фірми",
        "s4": "📦 Видано працівнику"
    }
    field_map = {
        "s1": "date_received",
        "s2": "date_sent",
        "s3": "date_returned",
        "s4": "date_given"
    }
    new_status = status_map.get(code)
    field = field_map.get(code)
    today = current_date()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"UPDATE cartridges SET status=?, {field}=? WHERE id=?", (new_status, today, cid))
    conn.commit()
    cur.execute("SELECT department FROM cartridges WHERE id=?", (cid,))
    dept = cur.fetchone()[0]
    conn.close()

    sync_to_sheets()

    await callback.message.edit_text(
        f"✅ *Картридж #{cid} ({dept})*\n🔁 Новий статус: {new_status}\n📅 {today}",
        parse_mode="Markdown"
    )


# === 🆕 Нова партія ===
async def new_batch(message: types.Message):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE batches SET status='closed' WHERE status='active'")
    cur.execute("INSERT INTO batches (created_at, status) VALUES (?, 'active')", (current_date(),))
    conn.commit()
    conn.close()

    sync_to_sheets()
    await message.answer("📦 Створено нову партію!")
    await show_main_menu(message)


# === 🚀 Запуск ===
async def main():
    init_db()
    print("🤖 Бот запущено…")
    sync_to_sheets()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
