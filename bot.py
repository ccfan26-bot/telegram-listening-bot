import os
import base64
import datetime
import sqlite3
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import openai

# ===== 环境变量 =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
POE_API_KEY = os.getenv("POE_API_KEY")

# ===== Poe 客户端 =====
client = openai.OpenAI(
    api_key=POE_API_KEY,
    base_url="https://api.poe.com/v1",
)

# ===== 数据库 =====
conn = sqlite3.connect("users.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    last_checkin TEXT,
    streak INTEGER DEFAULT 0
)
""")
conn.commit()

# ===== 命令 =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎧 欢迎来到精听训练系统\n\n"
        "输入 /today 开始训练"
    )

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎧 今日任务：\n"
        "1️⃣ 盲听3遍\n"
        "2️⃣ 跟读5遍\n"
        "3️⃣ 发一条完整朗读语音 ✅"
    )

# ===== 语音处理 =====
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    await file.download_to_drive("voice.ogg")

    # 转为 base64
    with open("voice.ogg", "rb") as f:
        audio_bytes = f.read()
        audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")

    prompt_text = """
你是一个英语口语教练。

请完成：
1. 将音频转写成文字
2. 指出语法错误
3. 给出正确版本
4. 简要解释
5. 给0-100评分
6. 一句鼓励

控制在150字以内。
"""

    # 调用 Poe GPT-4o
    chat = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_base64,
                            "format": "ogg"
                        }
                    }
                ],
            }
        ],
    )

    feedback = chat.choices[0].message.content

    # ===== 更新打卡 =====
    user_id = update.message.from_user.id
    today_date = str(datetime.date.today())

    cursor.execute("SELECT last_checkin, streak FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if row:
        last_date, streak = row
        if last_date == str(datetime.date.today() - datetime.timedelta(days=1)):
            streak += 1
        else:
            streak = 1
        cursor.execute("UPDATE users SET last_checkin=?, streak=? WHERE user_id=?",
                       (today_date, streak, user_id))
    else:
        streak = 1
        cursor.execute("INSERT INTO users VALUES (?, ?, ?)",
                       (user_id, today_date, streak))

    conn.commit()

    await update.message.reply_text(feedback)
    await update.message.reply_text(f"🔥 当前连续打卡：{streak}天")

# ===== 每日提醒 =====
async def send_daily(app):
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    for user in users:
        await app.bot.send_message(
            chat_id=user[0],
            text="🎧 晚上好！记得完成今日精听训练 /today"
        )

# ===== 启动 =====
app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("today", today))
app.add_handler(MessageHandler(filters.VOICE, handle_voice))

scheduler = AsyncIOScheduler()
scheduler.add_job(lambda: send_daily(app), "cron", hour=20, minute=30)
scheduler.start()

import os

PORT = int(os.environ.get("PORT", 10000))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

app.run_webhook(
    listen="0.0.0.0",
    port=PORT,
    webhook_url=f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}"
)
