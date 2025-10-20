import os
import asyncio
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# === Налаштування ===
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

DB_PATH = os.path.join(os.path.dirname(__file__), "cartridges.db")


# === ФУНКЦІЯ ПЕРЕВІРКИ/СТВОРЕННЯ БАЗИ ===
def ensure_database():
    """Перевіряє базу: якщо таблиці відсутні або застарілі — відновлює структуру."""
    if not os.path.exists(DB_PATH):
        print("⚙️ База даних не знайдена — створюємо нову...")
        init_db()
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [t[0] for t in cur.fetchall()]

        needs_rebuild = False

        if "cartridges" not in tables or "batches" not in tables:
            needs_rebuild = True
        else:
            cur.execute("PRAGMA table_info(batches)")
            columns = [col[1] for col in cur.fetchall()]
            if "status" not in columns:
                print("⚠️ Таблиця batches без колонки 'status' — потрібно оновити базу.")
                needs_rebuild = True

        conn.close()

        if needs_rebuild:
            os.remove(DB_PATH)
            print("🧱 Відновлення структури бази...")
            init_db()
        else:
            print("✅ База даних у нормі.")
    except Exception as e:
        print("⚠️ Помилка при перевірці бази:", e)
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        init_db()


def init_db():
    """Створює базу даних і таблиці."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # --- Таблиця картриджів ---
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

    # --- Таблиця партій ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            status TEXT
        )
    """)

    conn.commit()
    conn.close()
    print("✅ Створено базу даних і таблиці cartridges, batches.")


# === КОМАНДА /start ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Додати картридж", callback_data="add_cartridge")],
        [InlineKeyboardButton(text="📦 Перегляд партій", callback_data="view_batches")],
        [InlineKeyboardButton(text="♻️ Змінити статус", callback_data="change_status")],
    ])
    await message.answer("🛠️ *Меню керування картриджами:*\nОберіть дію 👇", parse_mode="Markdown", reply_markup=kb)


# === ДОДАВАННЯ КАРТРИДЖА ===
@dp.callback_query(lambda c: c.data == "add_cartridge")
async def add_cartridge(callback: types.CallbackQuery):
    await callback.message.answer("Введіть назву відділення:")
    dp.message.register(save_info)


async def save_info(message: types.Message):
    dept = message.text.strip()
    date_received = datetime.now().strftime("%d.%m.%Y")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Перевірка активної партії
    cur.execute("SELECT id FROM batches WHERE status='active'")
    row = cur.fetchone()

    if row is None:
        cur.execute(
            "INSERT INTO batches (created_at, status) VALUES (?, ?)",
            (date_received, "active")
        )
        conn.commit()
        batch_id = cur.lastrowid
        print("🆕 Створено нову партію:", batch_id)
    else:
        batch_id = row[0]

    # Додаємо картридж
    cur.execute("""
        INSERT INTO cartridges (date_received, department, status, batch_id)
        VALUES (?, ?, ?, ?)
    """, (date_received, dept, "⛔ Вилучено у працівника", batch_id))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Картридж із відділення '{dept}' додано до партії №{batch_id}.")


# === ПЕРЕГЛЯД ПАРТІЙ ===
@dp.callback_query(lambda c: c.data == "view_batches")
async def view_batches(callback: types.CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, created_at, status FROM batches ORDER BY id DESC")
    batches = cur.fetchall()
    conn.close()

    if not batches:
        await callback.message.answer("❌ Партій ще немає.")
        return

    text = "📦 *Список партій:*\n\n"
    for b in batches:
        text += f"🆔 {b[0]} | 📅 {b[1]} | 🟢 {b[2]}\n"
    await callback.message.answer(text, parse_mode="Markdown")


# === ЗМІНА СТАТУСУ ===
@dp.callback_query(lambda c: c.data == "change_status")
async def choose_batch(callback: types.CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, created_at FROM batches ORDER BY id DESC")
    batches = cur.fetchall()
    conn.close()

    if not batches:
        await callback.message.answer("❌ Немає жодної партії для зміни статусів.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Партія {b[0]} ({b[1]})", callback_data=f"batch_{b[0]}")] for b in batches
    ])
    await callback.message.answer("Оберіть партію:", reply_markup=kb)


@dp.callback_query(lambda c: c.data.startswith("batch_"))
async def change_status(callback: types.CallbackQuery):
    batch_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Прийнятий на заправку", callback_data=f"status_{batch_id}_sent")],
        [InlineKeyboardButton(text="✅ Готовий до видачі", callback_data=f"status_{batch_id}_ready")],
        [InlineKeyboardButton(text="📤 Виданий співробітнику", callback_data=f"status_{batch_id}_given")],
    ])
    await callback.message.answer("Оберіть новий статус для партії:", reply_markup=kb)


@dp.callback_query(lambda c: c.data.startswith("status_"))
async def update_status(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    batch_id = int(parts[1])
    new_status = parts[2]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if new_status == "sent":
        status_text = "🚚 Відправлено на заправку"
        cur.execute("UPDATE cartridges SET status=?, date_sent=? WHERE batch_id=?",
                    (status_text, datetime.now().strftime("%d.%m.%Y"), batch_id))
    elif new_status == "ready":
        status_text = "✅ Готовий до видачі"
        cur.execute("UPDATE cartridges SET status=?, date_returned=? WHERE batch_id=?",
                    (status_text, datetime.now().strftime("%d.%m.%Y"), batch_id))
    elif new_status == "given":
        status_text = "📤 Виданий співробітнику"
        cur.execute("UPDATE cartridges SET status=?, date_given=? WHERE batch_id=?",
                    (status_text, datetime.now().strftime("%d.%m.%Y"), batch_id))

    conn.commit()
    conn.close()

    await callback.message.answer(f"✅ Статус партії №{batch_id} змінено на: {status_text}")


# === WEB SERVER (для Render/PythonAnywhere) ===
async def handle(request):
    return web.Response(text="Bot is running ✅")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start()
    print("🌐 Web endpoint started on port", os.getenv("PORT", 10000))


# === ЗАПУСК ===
async def main():
    ensure_database()
    await asyncio.gather(
        run_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
