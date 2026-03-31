import os
import base64
import datetime
import sqlite3
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import openai

# ==============================
# 环境变量
# ==============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
POE_API_KEY = os.getenv("POE_API_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

if not BOT_TOKEN or not POE_API_KEY:
    raise ValueError("Missing BOT_TOKEN or POE_API_KEY")

# ==============================
# Poe 客户端
# ==============================
client = openai.OpenAI(
    api_key=POE_API_KEY,
    base_url="https://api.poe.com/v1",
)

# ==============================
# 数据库
# ==============================
conn = sqlite3.connect("/tmp/users.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    last_checkin TEXT,
    streak INTEGER DEFAULT 0
)
""")
conn.commit()

# ==============================
# 命令
# ==============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎧 欢迎来到精听训练系统\n\n发送语音我会帮你评分 ✅"
    )

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 今日任务：\n"
        "1️⃣ 盲听3遍\n"
        "2️⃣ 跟读5遍\n"
        "3️⃣ 发送完整语音 ✅"
    )

# ==============================
# 语音处理
# ==============================
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    await file.download_to_drive("/tmp/voice.ogg")

    with open("/tmp/voice.ogg", "rb") as f:
        audio_base64 = base64.b64encode(f.read()).decode("utf-8")

    prompt_text = """
你是英语口语教练。
1. 转写音频
2. 纠错
3. 给正确版本
4. 评分0-100
5. 一句鼓励
控制在150字以内。
"""

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
                            "format": "ogg",
                        },
                    },
                ],
            }
        ],
    )

    feedback = chat.choices[0].message.content

    # ==============================
    # 更新打卡
    # ==============================
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
        cursor.execute(
            "UPDATE users SET last_checkin=?, streak=? WHERE user_id=?",
            (today_date, streak, user_id),
        )
    else:
        streak = 1
        cursor.execute(
            "INSERT INTO users VALUES (?, ?, ?)",
            (user_id, today_date, streak),
        )

    conn.commit()

    await update.message.reply_text(feedback)
    await update.message.reply_text(f"🔥 连续打卡：{streak}天")

# ==============================
# 创建应用
# ==============================
app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("today", today))
app.add_handler(MessageHandler(filters.VOICE, handle_voice))

# ==============================
# Webhook 启动
# ==============================
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 10000))

    if not RENDER_EXTERNAL_URL:
        raise ValueError("RENDER_EXTERNAL_URL not set")

    webhook_path = f"/{BOT_TOKEN}"
    webhook_url = f"{RENDER_EXTERNAL_URL}{webhook_path}"

    print("Starting webhook...")
    print("Webhook URL:", webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
        url_path=BOT_TOKEN,
    )
