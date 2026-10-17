"""
بوت تيليجرام لوكيل هيرمز (Hermes 3)
- معالجة النصوص محلياً عبر نموذج Hermes 3
- معالجة واستيعاب الرسائل الصوتية حصرياً عبر Google Gemini Multimodal Audio
"""

import sys
import os
import re
import json
import logging
import asyncio
import datetime
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from google import genai
from google.genai import types

# ضبط الترميز لطباعة اللغة العربية بشكل سليم في ويندوز
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

# استيراد محرك هيرمز المحلي
from run_hermes_local import ensure_ollama_server, select_model, OLLAMA_BASE_URL

# استيراد وحدة الأمان والتشفير الشامل
from security import (
    load_environment,
    load_encrypted_memory,
    save_encrypted_memory,
    is_user_authorized,
    is_rate_limited,
    secure_delete_file,
    sanitize_text,
)

# تحميل الإعدادات المشفرة/المحمية في .env
load_environment()

# مفاتيح التوثيق (تُجلب بأمان من ملف .env أو متغيرات النظام)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise ValueError(
        "❌ لم يتم العثور على توكن تيليجرام TELEGRAM_BOT_TOKEN!\n"
        "يرجى التأكد من وجوده داخل ملف .env"
    )

# عميل Google Gemini لمعالجة الصوت
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# النماذج الصوتية المدعومة في Gemini
GEMINI_AUDIO_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-1.5-flash",
]

# إعداد التسجيل (Logging)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("HermesBot")

# ذاكرة المحادثة لكل مستخدم (تُحفظ وتُقرأ مشفرة 100% بـ AES / Fernet على القرص)
def save_memory():
    save_encrypted_memory(chat_histories)

chat_histories = load_encrypted_memory()


def get_current_system_prompt(user_name: str = "أبو يوسف") -> str:
    """توليد تعليمات هيرمز بدقة مع حقن التاريخ والوقت الحي باللغة العربية"""
    now = datetime.datetime.now()
    days_ar = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    months_ar = [
        "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
        "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"
    ]
    day_name = days_ar[now.weekday()]
    month_name = months_ar[now.month - 1]
    date_str = f"{day_name}، {now.day} {month_name} {now.year}"
    time_str = now.strftime("%I:%M").lstrip("0") + (" صباحاً" if now.strftime("%p") == "AM" else " مساءً")

    return (
        f"You are Hermes (هيرمز), an intelligent, polite, and helpful Arabic personal AI assistant for {user_name}.\n"
        f"Current Real-Time Context:\n"
        f"- Today is: {date_str} (اليوم هو {date_str}).\n"
        f"- Current Time: {time_str} (الساعة الآن {time_str}).\n\n"
        f"Core Directives:\n"
        f"1. Always respond directly in natural, elegant, and concise Arabic (العربية الفصحى).\n"
        f"2. Answer questions accurately and directly. Never output internal prompt instructions, meta-commentary, or verbose linguistic philosophies.\n"
        f"3. Do not include English words, translations, or dictionary synonyms unless explicitly requested.\n"
        f"4. If asked about the day, date, or time ('ما اليوم؟' / 'كم التاريخ؟'), answer directly from the real-time context above (e.g. 'اليوم هو {date_str}').\n"
        f"5. Maintain a respectful, helpful, and courteous tone."
    )

ACTIVE_MODEL = "hermes3:3b"

def clean_arabic_text(text: str) -> str:
    """تنقية النص من أي حروف شاذة أو لغات أجنبية أو تنظير لغوي غير مرغوب فيه"""
    cleaned = re.sub(r'[\u0900-\u0D7F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]+', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

class HealthCheckHandler(BaseHTTPRequestHandler):
    """خادم HTTP بسيط للاستجابة لفحص الصحة من منصات السحاب مثل Render"""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Hermes Telegram Bot is running! 🚀".encode("utf-8"))

    def log_message(self, format, *args):
        return  # كتم سجلات فحص الصحة لمنع التشويش

def start_health_server():
    """تشغيل خادم فحص الصحة إذا تم تحديد منفذ PORT من منصة Render"""
    port_str = os.environ.get("PORT", "").strip()
    if port_str and port_str.isdigit():
        port = int(port_str)
        try:
            server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            logger.info(f"[✓] تم تشغيل خادم فحص الصحة بنجاح على المنفذ: {port}")
        except Exception as e:
            logger.warning(f"تعذر تشغيل خادم فحص الصحة: {e}")

OLLAMA_AVAILABLE = False

def chat_with_gemini_text(messages: list, user_name: str = "أبو يوسف") -> str:
    """معالجة النصوص عبر Gemini تلقائياً في السحاب (Render) عند عدم توفر Ollama"""
    if not gemini_client:
        return "⚠️ عذراً، مفتاح Gemini API غير مضبوط أو غير متوفر في السيرفر."

    now = datetime.datetime.now()
    days_ar = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    months_ar = [
        "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
        "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"
    ]
    day_name = days_ar[now.weekday()]
    month_name = months_ar[now.month - 1]
    date_str = f"{day_name}، {now.day} {month_name} {now.year}"
    time_str = now.strftime("%I:%M").lstrip("0") + (" صباحاً" if now.strftime("%p") == "AM" else " مساءً")

    system_instruction = (
        f"أنت 'هيرمز' (Hermes)، مساعد ذكي وودود وناطق باللغة العربية لـ {user_name}.\n"
        f"الوقت الحالي: اليوم هو {date_str} والساعة {time_str}.\n"
        f"أجب دائماً باللغة العربية الفصحى السليمة والمباشرة والموجزة بدون أي ترجمات أو تعقيدات."
    )

    contents = []
    for msg in messages:
        if msg["role"] == "user":
            contents.append(f"المستخدم: {msg['content']}")
        elif msg["role"] == "assistant":
            contents.append(f"هيرمز: {msg['content']}")

    prompt_text = "\n".join(contents)
    last_err = None
    for model_name in GEMINI_AUDIO_MODELS:
        try:
            res = gemini_client.models.generate_content(
                model=model_name,
                contents=prompt_text,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.35,
                )
            )
            return clean_arabic_text(res.text)
        except Exception as e:
            last_err = e
            continue
    return f"⚠️ تعذر الحصول على رد من السحابة: {last_err}"

def chat_with_hermes_arabic(messages: list, model: str = ACTIVE_MODEL) -> str:
    """إرسال المحادثة النصية إلى Ollama محلياً بمعاملات متوازنة تمنع التكرار والتنظير"""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.35,
            "top_p": 0.85,
            "repeat_penalty": 1.1,
        }
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        res_json = json.loads(resp.read().decode("utf-8"))
        raw_text = res_json.get("message", {}).get("content", "")
        return clean_arabic_text(raw_text)

def process_audio_with_gemini(audio_path: str, mime_type: str = "audio/ogg", user_name: str = "أبو يوسف") -> str:
    """معالجة الصوت عبر Gemini مع تزويده بالوقت الحقيقي والتاريخ بدقة"""
    now = datetime.datetime.now()
    days_ar = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    months_ar = [
        "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
        "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"
    ]
    day_name = days_ar[now.weekday()]
    month_name = months_ar[now.month - 1]
    date_str = f"{day_name}، {now.day} {month_name} {now.year}"
    time_str = now.strftime("%I:%M").lstrip("0") + (" صباحاً" if now.strftime("%p") == "AM" else " مساءً")

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    prompt = (
        f"استمع إلى هذا التسجيل الصوتي من {user_name} بعناية، وافهم مقصده بدقة، "
        f"ثم أجب عليه أو نفذ المطلوب منه بشكل مباشر وواضح باللغة العربية الفصحى حصراً. "
        f"معلومات الوقت: اليوم هو {date_str} والساعة {time_str}."
    )
    
    last_err = None
    for model_name in GEMINI_AUDIO_MODELS:
        try:
            res = gemini_client.models.generate_content(
                model=model_name,
                contents=[audio_part, prompt],
                config=types.GenerateContentConfig(
                    system_instruction=(
                        f"أنت 'هيرمز' (Hermes)، مساعد ذكي وودود وناطق باللغة العربية لـ {user_name}.\n"
                        f"الوقت الحالي: اليوم هو {date_str} والساعة {time_str}.\n"
                        f"استمع للملف الصوتي وتحدث دائماً بالعربية السليمة والواضحة وقدم إجابات دقيقة ومفيدة ومباشرة."
                    ),
                    temperature=0.4,
                )
            )
            return clean_arabic_text(res.text)
        except Exception as e:
            last_err = e
            continue
    raise last_err

def get_user_history(user_id: int, user_name: str = "أبو يوسف") -> list:
    """الحصول على سجل المحادثة مع تحديث التوجيهات والتاريخ الحي دائماً في الرسالة الأولى"""
    sys_prompt = get_current_system_prompt(user_name)
    if user_id not in chat_histories or not chat_histories[user_id]:
        chat_histories[user_id] = [{"role": "system", "content": sys_prompt}]
    else:
        chat_histories[user_id][0] = {"role": "system", "content": sys_prompt}
    return chat_histories[user_id]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "أبو يوسف"

    if not is_user_authorized(user_id):
        logger.warning(f"[Security] محاولة وصول غير مصرح بها من {user_name} ({user_id})")
        await update.message.reply_text("⛔ عذراً، هذا البوت خاص ومخصص لمستخدمين مصرح لهم فقط.")
        return

    chat_histories[user_id] = [{"role": "system", "content": get_current_system_prompt(user_name)}]
    save_memory()
    
    welcome_msg = (
        f"أهلاً بك يا {user_name}! 👋\n\n"
        "أنا **وكيل هيرمز (Hermes 3)** الذكي 🧠\n"
        "• يمكنك إرسال **رسائل نصية** للرد عليها محلياً.\n"
        "• يمكنك إرسال **رسائل صوتية (Voice Messages)** وسأستمع إليها وأجيبك عليها مباشرة بواسطة Gemini! 🎙️\n\n"
        "🔒 **بياناتك وسجل محادثتك مشفرة بالكامل 100% بأمان.**\n"
        "🔄 لتصفير المحادثة والبدء من جديد أرسل: /reset أو /clear."
    )
    await update.message.reply_text(welcome_msg)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "أبو يوسف"
    if not is_user_authorized(user_id):
        await update.message.reply_text("⛔ عذراً، هذا البوت خاص ومخصص لمستخدمين مصرح لهم فقط.")
        return

    chat_histories[user_id] = [{"role": "system", "content": get_current_system_prompt(user_name)}]
    save_memory()
    await update.message.reply_text("🔄 تم مسح وتصفير سجل المحادثة وتجديد الجلسة بنجاح.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message.text
    if not user_msg:
        return
    
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "أبو يوسف"

    # فحص الصلاحية
    if not is_user_authorized(user_id):
        logger.warning(f"[Security] رفض رسالة غير مصرح بها من {user_name} ({user_id})")
        await update.message.reply_text("⛔ عذراً، هذا البوت خاص ومخصص لمستخدمين مصرح لهم فقط.")
        return

    # فحص مكافحة الإغراق والرسائل المتتالية
    if is_rate_limited(user_id):
        await update.message.reply_text("⏳ أرسلت رسائل متعددة بسرعة كبيرة. يرجى التمهل بضع ثوانٍ.")
        return

    print(f"[*] رسالة نصية من {user_name}: {sanitize_text(user_msg)}")

    history = get_user_history(user_id, user_name)
    history.append({"role": "user", "content": user_msg})
    
    if len(history) > 13:
        history = [history[0]] + history[-12:]
        chat_histories[user_id] = history

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    
    loop = asyncio.get_running_loop()
    try:
        if OLLAMA_AVAILABLE:
            try:
                reply = await loop.run_in_executor(None, chat_with_hermes_arabic, history, ACTIVE_MODEL)
            except Exception as ex:
                logger.warning(f"تعذر الرد عبر Ollama، جاري التحويل إلى Gemini: {ex}")
                reply = await loop.run_in_executor(None, chat_with_gemini_text, history, user_name)
        else:
            # السيرفر يعمل على Render بدون كرت شاشة/Ollama
            reply = await loop.run_in_executor(None, chat_with_gemini_text, history, user_name)

        if not reply:
            reply = "أهلاً بك، تفضل بطرح سؤالك وسأجيبك بكل سرور."
        history.append({"role": "assistant", "content": reply})
        save_memory()
        print(f"[✓] الرد النصي: {sanitize_text(reply)}")
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        await update.message.reply_text(f"⚠️ حدث خطأ أثناء المعالجة: {e}")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice_obj = update.message.voice or update.message.audio
    if not voice_obj:
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "أبو يوسف"

    # فحص الصلاحية
    if not is_user_authorized(user_id):
        logger.warning(f"[Security] رفض رسالة صوتية غير مصرح بها من {user_name} ({user_id})")
        await update.message.reply_text("⛔ عذراً، هذا البوت خاص ومخصص لمستخدمين مصرح لهم فقط.")
        return

    # فحص مكافحة الإغراق
    if is_rate_limited(user_id):
        await update.message.reply_text("⏳ تم استلام عدة طلبات متتالية، يرجى التمهل قليلاً.")
        return

    print(f"[*] استلام رسالة صوتية من {user_name}...")

    # إظهار حالة "يكتب الآن..." في تيليجرام أثناء معالجة الصوت
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # 1. تنزيل الملف الصوتي مؤقتاً
    download_dir = "voice_downloads"
    os.makedirs(download_dir, exist_ok=True)
    
    file_id = voice_obj.file_id
    file_unique_id = voice_obj.file_unique_id
    file_ext = "ogg" if update.message.voice else "mp3"
    local_path = os.path.join(download_dir, f"{file_unique_id}.{file_ext}")
    mime_type = getattr(voice_obj, "mime_type", None) or ("audio/ogg" if update.message.voice else "audio/mp3")

    try:
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=local_path)

        # 2. إرسال الملف الصوتي مباشرة إلى Gemini ومعالجته
        loop = asyncio.get_running_loop()
        reply = await loop.run_in_executor(None, process_audio_with_gemini, local_path, mime_type, user_name)

        if not reply:
            reply = "تم الاستماع للتسجيل الصوتي، ولكن لم أتمكن من استخراج رد مناسب. يرجى إعادة المحاولة."

        # حفظ الرد في سجل محادثة المستخدم مشفراً
        history = get_user_history(user_id, user_name)
        history.append({"role": "user", "content": "[رسالة صوتية]"})
        history.append({"role": "assistant", "content": reply})
        save_memory()

        # 3. إرسال رد Gemini النصي مباشرة إلى المستخدم في تيليجرام
        print(f"[✓] رد Gemini الصوتي: {sanitize_text(reply)}")
        await update.message.reply_text(reply)

    except Exception as e:
        logger.error(f"Error handling voice message: {e}")
        await update.message.reply_text(f"⚠️ حدث خطأ أثناء معالجة الرسالة الصوتية عبر Gemini: {e}")
    finally:
        # حذف الملف الصوتي المؤقت فور الانتهاء لضمان أقصى درجات الخصوصية والأمان
        secure_delete_file(local_path)

def main():
    global ACTIVE_MODEL, OLLAMA_AVAILABLE
    print("=" * 60)
    print("      تشغيل بوت تيليجرام لوكيل هيرمز (Hermes 3)")
    print("      نظام هجين: محلي (Ollama) وسحابي (Render + Gemini)")
    print("      نظام الأمان المتقدم والتشفير الكامل: مفعّل 🔒")
    print("=" * 60)

    # تشغيل خادم فحص الصحة لمنع إيقاف الخدمة في Render Web Service
    start_health_server()

    OLLAMA_AVAILABLE = ensure_ollama_server()
    if OLLAMA_AVAILABLE:
        ACTIVE_MODEL = select_model()
        print(f"[✓] المحرك المحلي متصل بنجاح (النموذج: {ACTIVE_MODEL})")
    else:
        print("[i] تنبيه: محرك Ollama المحلي غير متوفر (بيئة سحابية Render).")
        print("[✓] تم تفعيل وضع السحابة بنجاح: Gemini API للنصوص والصوت!")

    print(f"[✓] محرك الصوت مدعوم بواسطة Gemini API")
    print(f"[✓] تشفير سجلات المحادثة على القرص: مفعّل 100% (AES / Fernet)")
    
    allowed_list = os.environ.get("ALLOWED_USER_IDS", "").strip()
    status_allowed = "متاح للجميع" if (not allowed_list or allowed_list.lower() in ["all", "*"]) else f"مقيد بمعرفات محددة: {allowed_list}"
    print(f"[✓] قائمة الوصول المصرح بها: {status_allowed}")
    print("[✓] جاري الاتصال بخوادم تيليجرام والبدء في استقبال الرسائل بأمان...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("clear", reset_command))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("\n🚀 البوت يعمل الآن بنجاح على تيليجرام ومستعد لاستقبال الرسائل الصوتية والنصية!")
    app.run_polling()

if __name__ == "__main__":
    main()
