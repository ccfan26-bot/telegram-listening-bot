import os
import base64
import datetime
import io
import psycopg2
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from gtts import gTTS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN or not POE_API_KEY:
    raise ValueError("Missing BOT_TOKEN or POE_API_KEY")
if not DATABASE_URL:
    raise ValueError("Missing DATABASE_URL")

# ==============================
# Poe 客户端
# ==============================
client = openai.OpenAI(
    api_key=POE_API_KEY,
    base_url="https://api.poe.com/v1",
)

# ==============================
# 数据库初始化 + 迁移
# ==============================
conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS materials (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    transcript TEXT NOT NULL,
    audio_file_id TEXT,
    difficulty INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT NOW()
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    last_checkin TEXT,
    streak INTEGER DEFAULT 0,
    difficulty INTEGER DEFAULT 1,
    current_material_id INTEGER,
    reminder_enabled BOOLEAN DEFAULT TRUE
)
""")

# 兼容旧表：如果列不存在就自动添加
migrations = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS difficulty INTEGER DEFAULT 1",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS current_material_id INTEGER",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS reminder_enabled BOOLEAN DEFAULT TRUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS streak INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_checkin TEXT",
]
for sql in migrations:
    cursor.execute(sql)

# ==============================
# 预置材料（9 篇，初/中/高各 3 篇）
# ==============================
SEED_MATERIALS = [
    # 🟢 初级
    (
        "My Morning Routine",
        "I wake up at seven o'clock every morning. First, I brush my teeth and wash my face. "
        "Then I make coffee and eat breakfast. I usually have toast with eggs. "
        "After breakfast, I check my phone for messages. I leave for work at eight thirty.",
        1,
    ),
    (
        "Shopping at the Market",
        "I go to the market every Saturday morning. I buy fresh vegetables and fruit. "
        "Tomatoes, carrots, and apples are my favorites. The market is always busy on weekends. "
        "I also buy bread from the bakery next door. It's a great way to start the weekend.",
        1,
    ),
    (
        "Talking About the Weather",
        "The weather today is really nice. The sun is shining and there are no clouds. "
        "It's warm but not too hot. A light breeze makes it feel perfect. "
        "Days like this are great for walking in the park. I hope it stays like this all week.",
        1,
    ),
    # 🟡 中级
    (
        "The Benefits of Exercise",
        "Regular exercise has many benefits for both your body and mind. "
        "When you exercise consistently, you improve your cardiovascular health and strengthen your muscles. "
        "Physical activity also releases endorphins, which help reduce stress and boost your mood. "
        "Even thirty minutes of moderate exercise a day can make a significant difference in your overall well-being.",
        2,
    ),
    (
        "Technology and Daily Life",
        "Technology has transformed the way we live and communicate. "
        "Smartphones keep us connected to friends and family around the world. "
        "Online platforms allow us to access information instantly and learn new skills at our own pace. "
        "However, spending too much time on screens can lead to problems like eye strain and reduced attention spans. "
        "Finding the right balance is essential in today's digital world.",
        2,
    ),
    (
        "Traveling on a Budget",
        "Traveling doesn't have to be expensive if you plan carefully. "
        "Booking flights in advance and being flexible with your dates can save you hundreds of dollars. "
        "Instead of staying in hotels, consider hostels or home-sharing options for a more affordable experience. "
        "Eating local street food is not only cheaper but often more delicious than tourist restaurants. "
        "With the right approach, you can explore the world without breaking the bank.",
        2,
    ),
    # 🔴 高级
    (
        "The Future of Artificial Intelligence",
        "Artificial intelligence is rapidly reshaping the landscape of nearly every industry, "
        "from healthcare to finance to creative arts. "
        "While proponents argue that AI will unlock unprecedented levels of productivity, "
        "critics warn of potential disruptions to employment and the ethical implications of autonomous decision-making. "
        "The challenge for policymakers and technologists alike is to harness AI's transformative potential "
        "while establishing robust frameworks that ensure accountability and fairness.",
        3,
    ),
    (
        "Climate Change and Global Policy",
        "The scientific consensus on climate change is unequivocal: human activities, "
        "particularly the burning of fossil fuels, are driving unprecedented changes to the Earth's climate system. "
        "Rising global temperatures are already manifesting in more frequent extreme weather events, "
        "rising sea levels, and disruptions to ecosystems worldwide. "
        "Addressing this crisis requires not only technological innovation but also coordinated international policy action — "
        "a challenge that demands cooperation across geopolitical, economic, and cultural divides.",
        3,
    ),
    (
        "Globalization and Cultural Identity",
        "Globalization has accelerated the flow of goods, ideas, and people across borders, "
        "creating a more interconnected world but also generating complex tensions around cultural identity. "
        "As global media and consumer culture permeate societies worldwide, "
        "many communities grapple with how to preserve their unique traditions and languages. "
        "At the same time, cultural exchange has fostered remarkable creativity, "
        "giving rise to hybrid forms of art, music, and cuisine that enrich human experience in profound ways.",
        3,
    ),
]


def seed_materials():
    cursor.execute("SELECT COUNT(*) FROM materials")
    count = cursor.fetchone()[0]
    if count == 0:
        cursor.executemany(
            "INSERT INTO materials (title, transcript, difficulty) VALUES (%s, %s, %s)",
            SEED_MATERIALS,
        )
        print(f"✅ Seeded {len(SEED_MATERIALS)} materials into database.")


seed_materials()

# ==============================
# 常量 & 状态
# ==============================
DIFFICULTY_LABELS = {1: "🟢 初级", 2: "🟡 中级", 3: "🔴 高级"}
pending_material = {}  # 管理员添加素材时的临时状态

# ==============================
# 数据库辅助函数
# ==============================
def ensure_user(user_id):
    cursor.execute(
        "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )


def get_material(material_id):
    cursor.execute(
        "SELECT id, title, transcript, audio_file_id, difficulty FROM materials WHERE id=%s",
        (material_id,),
    )
    return cursor.fetchone()


def get_next_material(difficulty, current_id=None):
    if current_id:
        cursor.execute(
            "SELECT id FROM materials WHERE difficulty=%s AND id > %s ORDER BY id ASC LIMIT 1",
            (difficulty, current_id),
        )
    else:
        cursor.execute(
            "SELECT id FROM materials WHERE difficulty=%s ORDER BY id ASC LIMIT 1",
            (difficulty,),
        )
    row = cursor.fetchone()
    return row[0] if row else None


def is_admin(user_id):
    return ADMIN_ID == 0 or user_id == ADMIN_ID


# ==============================
# /start
# ==============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    ensure_user(user_id)

    keyboard = [[
        InlineKeyboardButton("🟢 初级", callback_data="level_1"),
        InlineKeyboardButton("🟡 中级", callback_data="level_2"),
        InlineKeyboardButton("🔴 高级", callback_data="level_3"),
    ]]

    await update.message.reply_text(
        "🎧 *欢迎来到英语精听训练系统*\n\n"
        "本系统采用 Shadowing（影子跟读）方法：\n"
        "▸ 反复听同一段材料，先不看原文\n"
        "▸ 模仿语音语调跟读\n"
        "▸ 能流利复述后再进入下一篇\n\n"
        "初/中/高级各有 3 篇精选材料，音频自动生成。\n\n"
        "请先选择你的难度等级：",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ==============================
# /setlevel
# ==============================
async def setlevel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton("🟢 初级", callback_data="level_1"),
        InlineKeyboardButton("🟡 中级", callback_data="level_2"),
        InlineKeyboardButton("🔴 高级", callback_data="level_3"),
    ]]
    await update.message.reply_text(
        "请选择新的难度等级：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def level_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    difficulty = int(query.data.split("_")[1])

    ensure_user(user_id)
    material_id = get_next_material(difficulty)

    cursor.execute(
        "UPDATE users SET difficulty=%s, current_material_id=%s WHERE user_id=%s",
        (difficulty, material_id, user_id),
    )

    label = DIFFICULTY_LABELS[difficulty]
    if material_id:
        mat = get_material(material_id)
        await query.edit_message_text(
            f"✅ 难度已设置：{label}\n\n"
            f"📖 第一篇：{mat[1]}\n\n"
            f"发送 /material 获取音频和原文 🎯"
        )
    else:
        await query.edit_message_text(f"✅ 难度已设置：{label}\n\n⚠️ 该难度暂无材料。")


# ==============================
# /material
# ==============================
async def material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    ensure_user(user_id)

    cursor.execute(
        "SELECT current_material_id, difficulty FROM users WHERE user_id=%s", (user_id,)
    )
    row = cursor.fetchone()

    if not row or not row[0]:
        await update.message.reply_text(
            "⚠️ 还没有分配到材料。\n请先用 /setlevel 设置难度。"
        )
        return

    current_material_id, _ = row
    mat = get_material(current_material_id)
    if not mat:
        await update.message.reply_text("⚠️ 材料不存在，请重新 /setlevel。")
        return

    mat_id, title, transcript, audio_file_id, diff = mat

    await update.message.reply_text(
        f"📖 *{title}*\n"
        f"难度：{DIFFICULTY_LABELS[diff]}\n\n"
        f"📝 *原文：*\n{transcript}\n\n"
        f"🎯 *练习步骤：*\n"
        f"1️⃣ 先只听音频，不看原文\n"
        f"2️⃣ 对照原文，找出没听清的地方\n"
        f"3️⃣ 跟读模仿，注意语音语调\n"
        f"4️⃣ 发送语音获取 AI 评分\n"
        f"5️⃣ 流利复述后，发 /done 进入下一篇",
        parse_mode="Markdown",
    )

    if audio_file_id:
        # 直接使用缓存的 file_id
        await update.message.reply_audio(audio=audio_file_id, title=title)
    else:
        # 用 gTTS 生成（初级稍慢）
        thinking = await update.message.reply_text("🔊 正在生成音频，请稍候...")
        try:
            tts = gTTS(text=transcript, lang="en", slow=(diff == 1))
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            buf.seek(0)

            sent = await update.message.reply_audio(
                audio=buf, title=title, filename=f"{title}.mp3"
            )

            # 缓存 file_id，下次直接复用
            if sent.audio:
                cursor.execute(
                    "UPDATE materials SET audio_file_id=%s WHERE id=%s",
                    (sent.audio.file_id, mat_id),
                )
        except Exception as e:
            await update.message.reply_text(f"⚠️ 音频生成失败：{e}")
        finally:
            await thinking.delete()


# ==============================
# /done
# ==============================
async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    ensure_user(user_id)

    cursor.execute(
        "SELECT current_material_id, difficulty FROM users WHERE user_id=%s", (user_id,)
    )
    row = cursor.fetchone()

    if not row or not row[0]:
        await update.message.reply_text("⚠️ 还没有分配到材料。")
        return

    current_id, difficulty = row
    next_id = get_next_material(difficulty, current_id=current_id)

    if next_id:
        cursor.execute(
            "UPDATE users SET current_material_id=%s WHERE user_id=%s",
            (next_id, user_id),
        )
        mat = get_material(next_id)
        await update.message.reply_text(
            f"🎉 恭喜掌握当前材料！\n\n"
            f"📖 下一篇：*{mat[1]}*\n"
            f"发送 /material 开始学习 💪",
            parse_mode="Markdown",
        )
    else:
        cursor.execute(
            "UPDATE users SET current_material_id=NULL WHERE user_id=%s", (user_id,)
        )
        await update.message.reply_text(
            "🏆 你已完成该难度全部 3 篇材料！\n"
            "用 /setlevel 挑战更高难度 🚀"
        )


# ==============================
# /status
# ==============================
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    ensure_user(user_id)

    cursor.execute(
        "SELECT streak, difficulty, current_material_id FROM users WHERE user_id=%s",
        (user_id,),
    )
    streak, difficulty, material_id = cursor.fetchone()

    mat_title = "未分配"
    if material_id:
        mat = get_material(material_id)
        if mat:
            mat_title = mat[1]

    await update.message.reply_text(
        f"📊 *学习状态*\n\n"
        f"🔥 连续打卡：{streak} 天\n"
        f"📚 当前难度：{DIFFICULTY_LABELS.get(difficulty, '未设置')}\n"
        f"📖 当前材料：{mat_title}",
        parse_mode="Markdown",
    )


# ==============================
# /addmaterial（管理员追加自定义材料）
# ==============================
async def addmaterial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 只有管理员可以添加材料。")
        return

    pending_material[user_id] = True
    await update.message.reply_text(
        "📤 请发送音频文件\n\n"
        "在文件的 *caption* 中按以下格式填写：\n"
        "`标题|难度|原文`\n\n"
        "难度：1=初级，2=中级，3=高级\n\n"
        "示例：\n"
        "`BBC News Intro|2|The Prime Minister announced today that...`",
        parse_mode="Markdown",
    )


async def handle_audio_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_admin(user_id) or user_id not in pending_material:
        return

    audio = update.message.audio
    if not audio:
        return

    caption = update.message.caption or ""
    parts = caption.split("|", 2)

    if len(parts) < 3:
        await update.message.reply_text(
            "❌ Caption 格式错误，请按：`标题|难度|原文`", parse_mode="Markdown"
        )
        return

    title = parts[0].strip()
    try:
        difficulty = int(parts[1].strip())
        assert difficulty in [1, 2, 3]
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ 难度必须是 1、2 或 3")
        return

    transcript = parts[2].strip()
    cursor.execute(
        "INSERT INTO materials (title, transcript, audio_file_id, difficulty) VALUES (%s, %s, %s, %s) RETURNING id",
        (title, transcript, audio.file_id, difficulty),
    )
    new_id = cursor.fetchone()[0]
    pending_material.pop(user_id, None)

    await update.message.reply_text(
        f"✅ 材料添加成功！\n\n"
        f"🆔 ID：{new_id}\n"
        f"📖 标题：{title}\n"
        f"📊 难度：{DIFFICULTY_LABELS[difficulty]}"
    )


# ==============================
# /listmaterials（管理员）
# ==============================
async def listmaterials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 只有管理员可以查看材料列表。")
        return

    cursor.execute("SELECT id, title, difficulty FROM materials ORDER BY difficulty, id")
    rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("📭 材料库为空。")
        return

    text = "📚 *材料库*\n\n"
    for mat_id, title, diff in rows:
        text += f"`#{mat_id}` {DIFFICULTY_LABELS[diff]} {title}\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# ==============================
# 语音评分
# ==============================
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    ensure_user(user_id)

    cursor.execute(
        "SELECT current_material_id FROM users WHERE user_id=%s", (user_id,)
    )
    row = cursor.fetchone()
    transcript = ""
    if row and row[0]:
        mat = get_material(row[0])
        if mat:
            transcript = mat[2]

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    await file.download_to_drive("/tmp/voice.ogg")

    with open("/tmp/voice.ogg", "rb") as f:
        audio_base64 = base64.b64encode(f.read()).decode("utf-8")

    if transcript:
        prompt_text = (
            f"你是英语口语教练，用户正在练习 shadowing（影子跟读）。\n"
            f"原文是：{transcript}\n\n"
            f"请：\n"
            f"1. 转写用户说的内容\n"
            f"2. 与原文对比，指出错误或遗漏\n"
            f"3. 对发音和语调给出评价\n"
            f"4. 评分 0-100（shadowing 准确度）\n"
            f"5. 一句鼓励\n"
            f"控制在200字以内。"
        )
    else:
        prompt_text = (
            "你是英语口语教练。\n"
            "1. 转写音频\n"
            "2. 纠错并给出正确版本\n"
            "3. 评分 0-100\n"
            "4. 一句鼓励\n"
            "控制在150字以内。"
        )

    chat = client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "input_audio", "input_audio": {"data": audio_base64, "format": "ogg"}},
            ],
        }],
    )
    feedback = chat.choices[0].message.content

    # 打卡更新
    today_date = str(datetime.date.today())
    yesterday = str(datetime.date.today() - datetime.timedelta(days=1))

    cursor.execute(
        "SELECT last_checkin, streak FROM users WHERE user_id=%s", (user_id,)
    )
    last_date, streak = cursor.fetchone()

    if last_date == today_date:
        pass
    elif last_date == yesterday:
        streak += 1
    else:
        streak = 1

    cursor.execute(
        "UPDATE users SET last_checkin=%s, streak=%s WHERE user_id=%s",
        (today_date, streak, user_id),
    )

    await update.message.reply_text(feedback)
    await update.message.reply_text(
        f"🔥 连续打卡：{streak} 天\n\n觉得掌握了？发 /done 进入下一篇 ✨"
    )


# ==============================
# 每日提醒（09:00 上海时间）
# ==============================
async def send_daily_reminders(bot):
    cursor.execute("""
        SELECT u.user_id, m.title
        FROM users u
        JOIN materials m ON u.current_material_id = m.id
        WHERE u.reminder_enabled = TRUE
    """)
    for user_id, title in cursor.fetchall():
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"⏰ *每日练习提醒*\n\n"
                    f"📖 当前材料：{title}\n\n"
                    f"今天练了吗？发 /material 开始 💪"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"Reminder failed for {user_id}: {e}")


async def post_init(application):
    scheduler = AsyncIOScheduler(timezone=pytz.timezone("Asia/Shanghai"))
    scheduler.add_job(
        send_daily_reminders,
        trigger="cron",
        hour=9,
        minute=0,
        args=[application.bot],
    )
    scheduler.start()
    print("Scheduler started — daily reminders at 09:00 CST")


# ==============================
# 应用构建
# ==============================
app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .post_init(post_init)
    .build()
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("setlevel", setlevel))
app.add_handler(CommandHandler("material", material))
app.add_handler(CommandHandler("done", done))
app.add_handler(CommandHandler("status", status))
app.add_handler(CommandHandler("addmaterial", addmaterial))
app.add_handler(CommandHandler("listmaterials", listmaterials))
app.add_handler(CallbackQueryHandler(level_callback, pattern="^level_"))
app.add_handler(MessageHandler(filters.AUDIO, handle_audio_upload))
app.add_handler(MessageHandler(filters.VOICE, handle_voice))

# ==============================
# Webhook 启动
# ==============================
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 10000))

    if not RENDER_EXTERNAL_URL:
        raise ValueError("RENDER_EXTERNAL_URL not set")

    webhook_url = f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}"
    print("Starting webhook on port", PORT)
    print("Webhook URL:", webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
        url_path=BOT_TOKEN,
    )
