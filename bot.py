import asyncio
import logging
import random
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery, ReplyKeyboardRemove

# Telegram API credentials
API_ID = 25781839
API_HASH = "20a3f2f168739259a180dcdd642e196c"
BOT_TOKEN = "7614305417:AAGyXRK5sPap2V2elxVZQyqwfRpVCW6wOFc"
GROUP_ID = -1002431056179
ADMIN_IDS = [7584086775]

# Setup logging
logging.basicConfig(level=logging.INFO)

# Init bot and dispatcher
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())

# SQLite Setup
conn = sqlite3.connect("reports.db")
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        description TEXT,
        contact TEXT,
        additional TEXT,
        proof_type TEXT,
        proof_id TEXT
    )
""")
conn.commit()

# FSM States
class ReportSteps(StatesGroup):
    captcha = State()
    description = State()
    proof = State()
    contact = State()
    additional = State()

# Captcha Generator
def generate_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    return a, b, str(a + b)

# Start Command
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    a, b, answer = generate_captcha()
    await state.update_data(captcha_answer=answer)
    await message.answer(f"Welcome {message.from_user.full_name}!\n\nTo continue, please solve this CAPTCHA:\n\nWhat is {a} + {b}?")
    await state.set_state(ReportSteps.captcha)

# CAPTCHA Validation
@dp.message(ReportSteps.captcha)
async def captcha_check(message: Message, state: FSMContext):
    user_answer = message.text.strip()
    data = await state.get_data()
    if user_answer == data.get("captcha_answer"):
        await message.answer("✅ CAPTCHA passed.\n\nPlease describe what fraud happened to you:")
        await state.set_state(ReportSteps.description)
    else:
        await message.answer("❌ Incorrect answer. Try again:")

# Step 1: Get Fraud Description
@dp.message(ReportSteps.description)
async def get_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Send Proof", callback_data="send_proof")]
    ])
    await message.answer("Now, please send a proof (photo, document, or description):", reply_markup=kb)
    await state.set_state(ReportSteps.proof)

# Callback: Proof Navigation
@dp.callback_query(F.data == "send_proof")
async def proof_prompt(callback: CallbackQuery):
    await callback.message.answer("Please upload the proof (photo, document, or write it):")
    await callback.answer()

# Step 2: Get Proof
@dp.message(ReportSteps.proof, F.photo | F.document | F.text)
async def get_proof(message: Message, state: FSMContext):
    if message.photo:
        proof_type = "photo"
        proof_id = message.photo[-1].file_id
    elif message.document:
        proof_type = "document"
        proof_id = message.document.file_id
    else:
        proof_type = "text"
        proof_id = message.text
    await state.update_data(proof_type=proof_type, proof_id=proof_id)
    await message.answer("Now, enter the Telegram username or phone number of the fraudster:")
    await state.set_state(ReportSteps.contact)

# Step 3: Contact of Fraudster
@dp.message(ReportSteps.contact)
async def get_contact(message: Message, state: FSMContext):
    await state.update_data(contact=message.text)
    await message.answer("Finally, provide any other important info you'd like to share:")
    await state.set_state(ReportSteps.additional)

# Step 4: Additional Info + Send to Group + Save in DB
@dp.message(ReportSteps.additional)
async def get_additional(message: Message, state: FSMContext):
    await state.update_data(additional=message.text)
    data = await state.get_data()

    report_text = (
        f"🚨 <b>New Fraud Report</b>\n\n"
        f"👤 From: <a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a>\n"
        f"📝 <b>Description:</b> {data['description']}\n"
        f"📞 <b>Fraudster Contact:</b> {data['contact']}\n"
        f"❗ <b>Additional Info:</b> {data['additional']}"
    )

    # Send to group with proof
    if data["proof_type"] == "photo":
        await bot.send_photo(chat_id=GROUP_ID, photo=data["proof_id"], caption=report_text)
    elif data["proof_type"] == "document":
        await bot.send_document(chat_id=GROUP_ID, document=data["proof_id"], caption=report_text)
    else:
        report_text += f"\n\n🖼 <b>Proof:</b> {data['proof_id']}"
        await bot.send_message(chat_id=GROUP_ID, text=report_text)

    # Save to SQLite
    cursor.execute("""
        INSERT INTO reports (user_id, username, description, contact, additional, proof_type, proof_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        message.from_user.id,
        message.from_user.username,
        data["description"],
        data["contact"],
        data["additional"],
        data["proof_type"],
        data["proof_id"]
    ))
    conn.commit()

    await message.answer("✅ Thank you! Your report has been submitted.", reply_markup=ReplyKeyboardRemove())
    await state.clear()

# Admin-only broadcast (optional)
@dp.message(F.text.startswith("/broadcast"))
async def broadcast_handler(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("Access Denied.")
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        return await message.answer("Usage: /broadcast Your message here")
    cursor.execute("SELECT DISTINCT user_id FROM reports")
    users = cursor.fetchall()
    count = 0
    for user_id, in users:
        try:
            await bot.send_message(chat_id=user_id, text=f"📢 Admin Message:\n\n{text}")
            count += 1
        except:
            continue
    await message.answer(f"✅ Broadcast sent to {count} users.")

# Run bot
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
