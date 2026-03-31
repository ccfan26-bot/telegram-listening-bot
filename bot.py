import os
import base64
import datetime
import io
import json
import re
import psycopg2
import pytz
import requests as http_requests
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
BOT_TOKEN           = os.getenv("BOT_TOKEN")
POE_API_KEY         = os.getenv("POE_API_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
DATABASE_URL        = os.getenv("DATABASE_URL")
ADMIN_ID            = int(os.getenv("ADMIN_ID", "0"))
YOUTUBE_COOKIES     = os.getenv("YOUTUBE_COOKIES")   # cookies.txt 完整文本内容

if not BOT_TOKEN or not POE_API_KEY:
    raise ValueError("Missing BOT_TOKEN or POE_API_KEY")
if not DATABASE_URL:
    raise ValueError("Missing DATABASE_URL")

# ── 若设置了 YOUTUBE_COOKIES，启动时写入临时文件 ──
_COOKIES_FILE = "/tmp/yt_cookies.txt"
if YOUTUBE_COOKIES:
    with open(_COOKIES_FILE, "w", encoding="utf-8") as _cf:
        _cf.write(YOUTUBE_COOKIES)
    print("✅ YouTube cookies 已写入 /tmp/yt_cookies.txt")
else:
    _COOKIES_FILE = None
    print("⚠️  未配置 YOUTUBE_COOKIES，yt-dlp 将以匿名方式访问 YouTube")

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
MAX_AUDIO_BYTES   = 10 * 1024 * 1024   # 10 MB
pending_add: dict = {}                  # user_id -> {"step": ..., "data": {...}}

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
# AI 辅助函数
# ==============================
async def ai_analyze_material(text: str) -> dict:
    """调用 AI 自动判断标题和难度，返回 dict"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": (
                "分析以下英文文本，给出合适的标题和难度等级。\n\n"
                f"文本：\n{text[:2000]}\n\n"
                "只输出纯 JSON，格式如下（不加任何 markdown 标记或代码块）：\n"
                '{"title": "英文标题", "difficulty": 2, "reason": "难度判断依据（中文一句话）"}\n\n'
                "难度标准：\n"
                "1=初级：日常生活话题，简单词汇和句型\n"
                "2=中级：较丰富词汇，复合句，适合有一定基础者\n"
                "3=高级：专业词汇，复杂论述，学术或时事议题"
            ),
        }],
    )
    content = response.choices[0].message.content.strip()
    content = re.sub(r'```(?:json)?\s*|\s*```', '', content).strip()
    return json.loads(content)


async def transcribe_audio_bytes(audio_bytes: bytes, fmt: str = "webm") -> str:
    """使用 GPT-4o 转写音频字节数据，返回文本"""
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError("音频过大（超过 10 MB），请上传较短的片段")

    audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "请准确转写这段英文音频的全部内容，只输出原文文字，不要任何解释。",
                },
                {
                    "type": "input_audio",
                    "input_audio": {"data": audio_base64, "format": fmt},
                },
            ],
        }],
    )
    transcript = response.choices[0].message.content.strip()
    if not transcript:
        raise ValueError("未能转写出任何文字，请检查音频内容是否为英文语音")
    return transcript


async def transcribe_audio(file_path: str, fmt: str = "ogg") -> str:
    """读取本地音频文件后调用 transcribe_audio_bytes"""
    with open(file_path, "rb") as f:
        audio_bytes = f.read()
    return await transcribe_audio_bytes(audio_bytes, fmt)


def fetch_audio_from_url(audio_url: str, headers: dict) -> tuple[bytes, str]:
    """
    用 yt-dlp 提取的直链 + headers，流式拉取音频到内存。
    返回 (audio_bytes, ext)，超过 10 MB 则抛出异常。
    """
    resp = http_requests.get(
        audio_url,
        headers=headers,
        timeout=60,
        stream=True,
    )
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if "mp4" in content_type or "m4a" in content_type:
        ext = "m4a"
    elif "ogg" in content_type or "opus" in content_type:
        ext = "ogg"
    elif "mp3" in content_type or "mpeg" in content_type:
        ext = "mp3"
    else:
        ext = "webm"   # YouTube 默认

    chunks = []
    total  = 0
    for chunk in resp.iter_content(chunk_size=8192):
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_AUDIO_BYTES:
            raise ValueError("音频流超过 10 MB 限制，请提供较短的视频（建议 5 分钟以内）")

    return b"".join(chunks), ext


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
        "初/中/高级各有若干精选材料，音频自动生成。\n\n"
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

    user_id    = query.from_user.id
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
        await update.message.reply_audio(audio=audio_file_id, title=title)
    else:
        thinking = await update.message.reply_text("🔊 正在生成音频，请稍候...")
        try:
            tts = gTTS(text=transcript, lang="en", slow=(diff == 1))
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            buf.seek(0)

            sent = await update.message.reply_audio(
                audio=buf, title=title, filename=f"{title}.mp3"
            )

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
            "🏆 你已完成该难度全部材料！\n"
            "用 /setlevel 挑战更高难度，或 /add 添加新材料 🚀"
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
# /add — 添加材料（文本 / 音频文件 / 视频链接）
# ==============================
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    ensure_user(user_id)
    pending_add[user_id] = {"step": "waiting_input"}

    await update.message.reply_text(
        "📥 *添加新材料*\n\n"
        "请发送以下任意一种内容：\n\n"
        "📝 *英文文本* — 直接粘贴英文原文\n"
        "🎵 *音频文件* — 上传 mp3 / m4a / wav 等格式\n"
        "🔗 *视频链接* — YouTube 等平台的视频链接\n\n"
        "AI 会自动识别标题和难度，无需手动填写。\n\n"
        "发 /cancel 取消",
        parse_mode="Markdown",
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id in pending_add:
        pending_add.pop(user_id)
        await update.message.reply_text("❌ 已取消。")
    else:
        await update.message.reply_text("没有进行中的操作。")


async def _show_add_confirmation(update: Update, user_id: int):
    """展示材料预览并请求确认"""
    data       = pending_add[user_id]["data"]
    difficulty = data["difficulty"]
    preview    = data["transcript"][:200] + ("..." if len(data["transcript"]) > 200 else "")
    has_real_audio = data.get("audio_file_id") is not None

    audio_note = (
        "🎙 原声音频已上传（见上方消息）"
        if has_real_audio
        else "🔊 播放时将自动生成 TTS 合成音频"
    )

    keyboard = [[
        InlineKeyboardButton("✅ 确认添加", callback_data="add_confirm"),
        InlineKeyboardButton("❌ 取消",     callback_data="add_cancel"),
    ]]

    await update.message.reply_text(
        f"📋 *材料预览*\n\n"
        f"📖 *标题：* {data['title']}\n"
        f"📊 *难度：* {DIFFICULTY_LABELS.get(difficulty, str(difficulty))}\n"
        f"💡 *AI 判断：* {data['reason']}\n"
        f"🔈 *音频：* {audio_note}\n\n"
        f"📝 *原文预览：*\n{preview}\n\n"
        f"确认添加到材料库吗？",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def _build_ydl_opts() -> dict:
    """
    构建 yt-dlp 选项字典。
    若已配置 YOUTUBE_COOKIES 环境变量，自动加入 cookiefile，
    避免 YouTube 的「Sign in to confirm you're not a bot」错误。
    """
    opts = {
        "format":      "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
        "quiet":       True,
        "no_warnings": True,
    }
    if _COOKIES_FILE:
        opts["cookiefile"] = _COOKIES_FILE
    return opts


async def handle_add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户在 /add 流程中发送的文本或视频链接"""
    user_id = update.message.from_user.id

    if user_id not in pending_add or pending_add[user_id].get("step") != "waiting_input":
        return

    text        = update.message.text.strip()
    url_pattern = re.compile(r'^https?://\S+$')

    # ---- 视频链接 ----
    if url_pattern.match(text):
        thinking = await update.message.reply_text("🔗 正在解析视频链接，请稍候...")
        try:
            try:
                import yt_dlp
            except ImportError:
                await thinking.edit_text(
                    "❌ yt-dlp 未安装，无法处理视频链接。\n"
                    "请直接粘贴英文文本或上传音频文件。"
                )
                pending_add.pop(user_id, None)
                return

            # ① 提取元数据 + 音频流直链，不下载任何文件
            ydl_opts = _build_ydl_opts()
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(text, download=False)

            video_title  = info.get("title", "")
            audio_url    = info.get("url", "")
            http_headers = info.get("http_headers", {})

            if not audio_url:
                raise ValueError("无法从该链接提取音频流，请换一个视频试试")

            # ② 流式拉取音频到内存（不落磁盘）
            await thinking.edit_text("⬇️ 正在获取音频流，请稍候...")
            audio_bytes, ext = fetch_audio_from_url(audio_url, http_headers)

            # ③ 转写
            await thinking.edit_text("🎙 正在转写音频内容，请稍候...")
            transcript = await transcribe_audio_bytes(audio_bytes, ext)

            # ④ AI 分析难度
            await thinking.edit_text("🤖 正在分析难度，请稍候...")
            analysis = await ai_analyze_material(transcript)

            # 视频标题兜底
            if video_title and len(analysis.get("title", "")) < 3:
                analysis["title"] = video_title

            # ⑤ 上传原声音频到 Telegram，取得永久 file_id
            await thinking.edit_text("📤 正在上传原声音频，请稍候...")
            audio_buf  = io.BytesIO(audio_bytes)
            sent_audio = await update.message.reply_audio(
                audio    = audio_buf,
                title    = analysis["title"],
                filename = f"audio.{ext}",
                caption  = "🎧 原声音频预览 — 请确认是否添加到材料库",
            )
            file_id = sent_audio.audio.file_id if sent_audio.audio else None

            pending_add[user_id] = {
                "step": "confirming",
                "data": {
                    "title":         analysis["title"],
                    "difficulty":    analysis["difficulty"],
                    "transcript":    transcript,
                    "reason":        analysis["reason"],
                    "audio_file_id": file_id,   # 原声，永久有效
                },
            }
            await thinking.delete()
            await _show_add_confirmation(update, user_id)

        except Exception as e:
            err_str = str(e)

            # ── 友好提示：YouTube 机器人验证错误 ──
            if "Sign in to confirm" in err_str or "bot" in err_str.lower():
                tip = (
                    "❌ YouTube 拒绝访问：需要登录验证（反爬虫机制）。\n\n"
                    "🔧 *解决方法：配置 YouTube Cookies*\n\n"
                    "1️⃣ 在电脑浏览器中登录 YouTube\n"
                    "2️⃣ 安装扩展 *Get cookies.txt LOCALLY*\n"
                    "   （Chrome / Edge 应用商店搜索即可）\n"
                    "3️⃣ 访问 youtube.com，点击扩展导出 `cookies.txt`\n"
                    "4️⃣ 打开导出的文件，复制全部文本\n"
                    "5️⃣ 在 Render 控制台 → Environment 添加：\n"
                    "   `YOUTUBE_COOKIES` = *(粘贴 cookies.txt 全文)*\n"
                    "6️⃣ 重新部署服务后即可正常使用\n\n"
                    "📖 详细说明：https://github.com/yt-dlp/yt-dlp/wiki/FAQ"
                    "#how-do-i-pass-cookies-to-yt-dlp"
                )
                await thinking.edit_text(tip, parse_mode="Markdown")
            else:
                await thinking.edit_text(
                    f"❌ 处理失败：{err_str}\n\n"
                    "请确认视频可公开访问，或换用直接粘贴英文文本的方式。"
                )

            pending_add.pop(user_id, None)

    # ---- 英文文本 ----
    else:
        if len(text) < 30:
            await update.message.reply_text(
                "⚠️ 文本太短，请发送完整的英文材料（至少 30 个字符）。"
            )
            return

        thinking = await update.message.reply_text("🤖 正在分析材料难度，请稍候...")
        try:
            analysis = await ai_analyze_material(text)

            pending_add[user_id] = {
                "step": "confirming",
                "data": {
                    "title":         analysis["title"],
                    "difficulty":    analysis["difficulty"],
                    "transcript":    text,
                    "reason":        analysis["reason"],
                    "audio_file_id": None,   # 无原声，播放时 gTTS 生成
                },
            }
            await thinking.delete()
            await _show_add_confirmation(update, user_id)

        except Exception as e:
            await thinking.edit_text(f"❌ 分析失败：{e}")
            pending_add.pop(user_id, None)


async def handle_add_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户在 /add 流程中上传的音频文件"""
    user_id = update.message.from_user.id

    if user_id not in pending_add or pending_add[user_id].get("step") != "waiting_input":
        return

    audio = update.message.audio
    if not audio:
        return

    thinking = await update.message.reply_text("🎵 正在处理音频文件，请稍候...")
    try:
        file      = await context.bot.get_file(audio.file_id)
        file_path = "/tmp/upload_audio"
        await file.download_to_drive(file_path)

        mime = audio.mime_type or "audio/ogg"
        fmt  = mime.split("/")[-1]
        if fmt == "mpeg":
            fmt = "mp3"

        await thinking.edit_text("🎙 正在转写音频内容，请稍候...")
        transcript = await transcribe_audio(file_path, fmt)

        await thinking.edit_text("🤖 正在分析难度，请稍候...")
        analysis = await ai_analyze_material(transcript)

        pending_add[user_id] = {
            "step": "confirming",
            "data": {
                "title":         analysis["title"],
                "difficulty":    analysis["difficulty"],
                "transcript":    transcript,
                "reason":        analysis["reason"],
                "audio_file_id": audio.file_id,   # 原声，永久有效
            },
        }
        await thinking.delete()
        await _show_add_confirmation(update, user_id)

    except Exception as e:
        await thinking.edit_text(f"❌ 处理失败：{e}")
        pending_add.pop(user_id, None)


async def add_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理添加材料的确认 / 取消按钮"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "add_cancel":
        pending_add.pop(user_id, None)
        await query.edit_message_text("❌ 已取消添加。")
        return

    state = pending_add.get(user_id)
    if not state or state.get("step") != "confirming":
        await query.edit_message_text("⚠️ 操作已过期，请重新发送 /add。")
        return

    data = state["data"]
    cursor.execute(
        "INSERT INTO materials (title, transcript, audio_file_id, difficulty) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (data["title"], data["transcript"], data.get("audio_file_id"), data["difficulty"]),
    )
    new_id = cursor.fetchone()[0]
    pending_add.pop(user_id, None)

    await query.edit_message_text(
        f"✅ *材料已成功添加！*\n\n"
        f"🆔 ID：{new_id}\n"
        f"📖 标题：{data['title']}\n"
        f"📊 难度：{DIFFICULTY_LABELS.get(data['difficulty'], str(data['difficulty']))}\n\n"
        f"其他用户选择对应难度时将会学习到此材料 🎉",
        parse_mode="Markdown",
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
    row        = cursor.fetchone()
    transcript = ""
    if row and row[0]:
        mat = get_material(row[0])
        if mat:
            transcript = mat[2]

    voice = update.message.voice
    file  = await context.bot.get_file(voice.file_id)
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
                {"type": "text",        "text": prompt_text},
                {"type": "input_audio", "input_audio": {"data": audio_base64, "format": "ogg"}},
            ],
        }],
    )
    feedback = chat.choices[0].message.content

    today_date = str(datetime.date.today())
    yesterday  = str(datetime.date.today() - datetime.timedelta(days=1))

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
                chat_id    = user_id,
                text       = (
                    f"⏰ *每日练习提醒*\n\n"
                    f"📖 当前材料：{title}\n\n"
                    f"今天练了吗？发 /material 开始 💪"
                ),
                parse_mode = "Markdown",
            )
        except Exception as e:
            print(f"Reminder failed for {user_id}: {e}")


async def post_init(application):
    scheduler = AsyncIOScheduler(timezone=pytz.timezone("Asia/Shanghai"))
    scheduler.add_job(
        send_daily_reminders,
        trigger = "cron",
        hour    = 9,
        minute  = 0,
        args    = [application.bot],
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

app.add_handler(CommandHandler("start",          start))
app.add_handler(CommandHandler("setlevel",       setlevel))
app.add_handler(CommandHandler("material",       material))
app.add_handler(CommandHandler("done",           done))
app.add_handler(CommandHandler("status",         status))
app.add_handler(CommandHandler("add",            add_command))
app.add_handler(CommandHandler("cancel",         cancel_command))
app.add_handler(CommandHandler("listmaterials",  listmaterials))
app.add_handler(CallbackQueryHandler(level_callback,       pattern="^level_"))
app.add_handler(CallbackQueryHandler(add_confirm_callback, pattern="^add_"))
app.add_handler(MessageHandler(filters.AUDIO,                           handle_add_audio))
app.add_handler(MessageHandler(filters.VOICE,                           handle_voice))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,         handle_add_text))

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
        listen      = "0.0.0.0",
        port        = PORT,
        webhook_url = webhook_url,
        url_path    = BOT_TOKEN,
    )
