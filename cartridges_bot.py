import asyncio
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
import os

# === Конфігурація ===
TOKEN = os.getenv("TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "111111111"))
DB_PATH = "cartridges.db"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# === Функції дати ===
def current_date():
    return datetime.now().strftime("%d.%m.%Y")

# === Ініціалізація бази даних ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cartridges (
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
    conn.commit()
    conn.close()

init_db()

# === Кнопки головного меню ===
def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Додати картридж", callback_data="add")
    kb.button(text="🔄 Змінити статус", callback_data="menu_status")
    kb.button(text="📦 Перегляд партій", callback_data="view_batches")
    kb.button(text="📤 Експорт у Excel", callback_data="export_excel")
    kb.adjust(2)
    return kb.as_markup()

# === Команди ===
@dp.message(Command("start"))
async def start(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас немає доступу.")
        return
    await message.answer("🧾 *Облік картриджів*\nВибери дію:", parse_mode="Markdown", reply_markup=main_menu())

# === Обробка кнопок головного меню ===
@dp.callback_query(F.data == "menu_home")
async def menu_home(callback: types.CallbackQuery):
    await callback.message.edit_text("🏠 Головне меню:", reply_markup=main_menu())

# === Додавання картриджа ===
@dp.callback_query(F.data == "add")
async def add_cartridge(callback: types.CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT MAX(batch_id) FROM cartridges")
    batch_id = cur.fetchone()[0] or 1
    cur.execute("INSERT INTO cartridges (date_received, department, status, batch_id) VALUES (?, ?, ?, ?)",
                (current_date(), "Невказано", "⛔ Вилучено у працівника", batch_id))
    conn.commit()
    conn.close()

    await callback.message.answer("✅ Картридж додано.", reply_markup=main_menu())

# === Вибір партії для зміни статусу ===
@dp.callback_query(F.data == "menu_status")
async def choose_batch(callback: types.CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT batch_id FROM cartridges ORDER BY batch_id DESC")
    batches = cur.fetchall()
    conn.close()

    kb = InlineKeyboardBuilder()
    for b in batches:
        kb.button(text=f"Партія #{b[0]}", callback_data=f"status_batch_{b[0]}")
    kb.button(text="🏠 Головне меню", callback_data="menu_home")
    kb.adjust(2)
    await callback.message.edit_text("🔄 Вибери партію для зміни статусу:", reply_markup=kb.as_markup())

# === Вибір картриджа в партії ===
@dp.callback_query(F.data.startswith("status_batch_"))
async def select_cartridge(callback: types.CallbackQuery):
    batch_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, department, status FROM cartridges WHERE batch_id=?", (batch_id,))
    cartridges = cur.fetchall()
    conn.close()

    kb = InlineKeyboardBuilder()
    for c in cartridges:
        kb.button(text=f"#{c[0]} | {c[1]} ({c[2]})", callback_data=f"choose_status_{c[0]}")
    kb.button(text="🏠 Головне меню", callback_data="menu_home")
    kb.adjust(1)
    await callback.message.edit_text(f"📦 Партія #{batch_id}\nВибери картридж:", reply_markup=kb.as_markup())

# === Зміна статусу ===
@dp.callback_query(F.data.startswith("choose_status_"))
async def change_status(callback: types.CallbackQuery):
    cid = int(callback.data.split("_")[2])
    kb = InlineKeyboardBuilder()
    kb.button(text="⛔ Вилучено у працівника", callback_data=f"set_{cid}_s1")
    kb.button(text="🔄 Відправлено на фірму", callback_data=f"set_{cid}_s2")
    kb.button(text="✅ Прибуло з фірми", callback_data=f"set_{cid}_s3")
    kb.button(text="📦 Видано працівнику", callback_data=f"set_{cid}_s4")
    kb.button(text="🏠 Головне меню", callback_data="menu_home")
    kb.adjust(1)
    await callback.message.edit_text(f"🔧 Вибери новий статус для #{cid}:", reply_markup=kb.as_markup())

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
    if field:
        cur.execute(f"UPDATE cartridges SET status=?, {field}=? WHERE id=?", (new_status, today, cid))
    else:
        cur.execute("UPDATE cartridges SET status=? WHERE id=?", (new_status, cid))
    conn.commit()
    cur.execute("SELECT department FROM cartridges WHERE id=?", (cid,))
    dept = cur.fetchone()[0]
    conn.close()

    text = (
        f"✅ *Картридж #{cid}* | *{dept}*\n"
        f"🔁 Новий статус: {new_status}\n"
        f"📅 Дата: {today}\n"
        f"───────────────"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Головне меню", callback_data="menu_home")
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb.as_markup())

# === Веб-ендпоінт для Render ===
async def handle(request):
    return web.Response(text="✅ Bot is alive!", content_type="text/plain")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()
    print("🌐 Web endpoint started on port 10000")

# === Запуск ===
async def main():
    await asyncio.gather(
        run_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
