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
from run_hermes_local import ensure_ollama_server, select_model, check_ollama_running, OLLAMA_BASE_URL

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
        f"أنت 'هيرمس' (Hermes)، مساعد ذكي وودود لـ {user_name}، تتحدث حصراً بالعامية الفلسطينية البسيطة والواضحة.\n\n"
        "القواعد الصارمة التي يجب أن تتبعها في كل رد:\n"
        "1. الإيجاز التام: جاوب على قد السؤال مباشرة وبدون أي مقدمات أو فلسفة زائدة أو حشو.\n"
        "2. اللهجة: استخدم العامية الفلسطينية اليومية السلسة والسهلة (مثل: كيفك، شو في، تمام، حاضر، هسا، إلك، شو بدك، من عيوني).\n"
        "3. حجم الرسالة: ممنوع إرسال رسائل طويلة أو فقرات إنشائية إلا إذا طلب المستخدم منك شرحاً تفصيلياً أو كوداً برمجياً.\n"
        "4. النتيجة أولاً: اعطِ الزبدة والمفيد من أول كلمة وركّز على حل المشكلة فوراً.\n"
        "5. ممنوع منعاً باتاً استخدام أي كلمات إنجليزية أو ترجمات أو تنظير لغوي.\n"
        f"6. معلومات الوقت الفعلي الحية: اليوم هو {date_str} والساعة {time_str}. إذا سُئلت عن اليوم أو التاريخ، جاوب مباشرة: اليوم هو {date_str}."
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

def detect_engine_mode() -> str:
    """
    تحديد محرك الذكاء الاصطناعي تلقائياً:
    - 'ollama': تشغيل محلي على حاسوبك عبر Ollama وكرت الشاشة
    - 'gemini': تشغيل سحابي على سيرفر Render عبر Google Gemini API
    """
    # 1. إذا تم تحديد المحرك يدوياً في .env
    forced = os.environ.get("FORCE_ENGINE", "").lower().strip()
    if forced in ["gemini", "cloud"]:
        return "gemini"
    if forced in ["ollama", "local"]:
        return "ollama"

    # 2. فحص هل البيئة سحابية (Render يضع تلقائياً RENDER=true أو PORT)
    is_cloud = bool(os.environ.get("RENDER") or (os.environ.get("PORT") and sys.platform != "win32"))
    if is_cloud:
        return "gemini"

    # 3. فحص هل محرك Ollama متاح ويعمل محلياً
    if check_ollama_running() or ensure_ollama_server():
        return "ollama"

    # 4. في حال عدم توفر Ollama، التحول التلقائي الذكي لـ Gemini
    return "gemini"

ACTIVE_ENGINE = "gemini"

def chat_with_gemini_text(messages: list, user_name: str = "أبو يوسف") -> str:
    """معالجة النصوص عبر Gemini تلقائياً في السحاب (Render) عند عدم توفر Ollama"""
    if not gemini_client:
        return "⚠️ عذراً، مفتاح Gemini API غير مضبوط أو غير متوفر في السيرفر."

    sys_prompt = get_current_system_prompt(user_name)

    contents = []
    # تخطي رسالة النظام الأولى لأنها ستمرر في system_instruction
    history_messages = messages[1:] if len(messages) > 1 and messages[0].get("role") == "system" else messages
    for msg in history_messages:
        role_label = user_name if msg["role"] == "user" else "هيرمز"
        contents.append(f"{role_label}: {msg['content']}")

    prompt_text = "\n".join(contents)
    last_err = None
    for model_name in GEMINI_AUDIO_MODELS:
        try:
            res = gemini_client.models.generate_content(
                model=model_name,
                contents=prompt_text,
                config=types.GenerateContentConfig(
                    system_instruction=sys_prompt,
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
        f"استمع إلى هذا التسجيل الصوتي من {user_name} بعناية وافهم المطلوب بدقة. "
        f"أجب عليه حصراً بالعامية الفلسطينية البسيطة والموجزة (الزبدة على قد السؤال بدون مقدمات أو حشو). "
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
                        f"أنت 'هيرمس' (Hermes)، مساعد ذكي وودود لـ {user_name}، تتحدث حصراً بالعامية الفلسطينية البسيطة والواضحة.\n"
                        "القواعد الصارمة:\n"
                        "1. الإيجاز التام: جاوب على قد السؤال مباشرة وبدون أي مقدمات أو فلسفة زائدة أو حشو.\n"
                        "2. اللهجة: استخدم العامية الفلسطينية اليومية السلسة والسهلة (مثل: كيفك، شو في، تمام، حاضر، هسا، إلك).\n"
                        "3. حجم الرسالة: ممنوع إرسال رسائل طويلة أو فقرات إنشائية إلا إذا طلب المستخدم شرحاً تفصيلياً أو كوداً برمجياً.\n"
                        "4. النتيجة أولاً: اعطِ الزبدة والمفيد من أول كلمة وركّز على حل المشكلة فوراً.\n"
                        f"الوقت الحالي: اليوم هو {date_str} والساعة {time_str}."
                    ),
                    temperature=0.35,
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
        f"هلا والله يا {user_name}! 👋\n\n"
        "أنا **هيرمس (Hermes)**، مساعدك الشخصي بالعامية الفلسطينية 🧠\n"
        "• ابعتلي أي **رسالة نصية** وبجاوبك عالسريع وبالمفيد.\n"
        "• ابعتلي **رسائل صوتية (Voice)** وبسمعها وبجاوبك فوراً! 🎙️\n\n"
        "🔒 بياناتك وسجل محادثتك مشفر ومحمي بالكامل 100%.\n"
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
        if ACTIVE_ENGINE == "ollama":
            try:
                # 🖥️ المعالجة محلياً عبر كرت الشاشة ونموذج Hermes 3
                reply = await loop.run_in_executor(None, chat_with_hermes_arabic, history, ACTIVE_MODEL)
            except Exception as ex:
                logger.warning(f"تعذر الرد عبر Ollama المحلي، جاري التحويل التلقائي إلى Gemini: {ex}")
                reply = await loop.run_in_executor(None, chat_with_gemini_text, history, user_name)
        else:
            # ☁️ المعالجة سحابياً عبر سيرفر Render بواسطة Gemini API
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
    global ACTIVE_MODEL, ACTIVE_ENGINE
    print("=" * 60)
    print("      تشغيل بوت تيليجرام لوكيل هيرمز (Hermes 3)")
    print("      نظام التحويل الذكي: Ollama محلياً + Gemini سحابياً (Render)")
    print("      نظام الأمان المتقدم والتشفير الكامل: مفعّل 🔒")
    print("=" * 60)

    # تشغيل خادم فحص الصحة لمنع إيقاف الخدمة في Render Web Service
    start_health_server()

    # تحديد بيئة التشغيل تلقائياً (محلي أو Render)
    ACTIVE_ENGINE = detect_engine_mode()

    if ACTIVE_ENGINE == "ollama":
        ACTIVE_MODEL = select_model()
        print(f"[🖥️ الوضع النشط]: محلي على جهازك (Ollama - النموذج: {ACTIVE_MODEL})")
        print(f"[⚡ تسريع المعالجة]: يعمل عبر كرت الشاشة بدون استهلاك أي حصة خارجية")
    else:
        print("[☁️ الوضع النشط]: سحابي على سيرفر Render")
        print("[⚡ سرعة واستقرار]: يعمل بنظام Gemini 3.8 Flash الذكي للنصوص والصوت 24/7!")

    print(f"[🎙️ معالجة الصوت]: Google Gemini Multimodal Audio")
    print(f"[🔒 تشفير البيانات]: AES / Fernet على القرص (مفعّل 100%)")
    
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
