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
import urllib.request
import urllib.error
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


SYSTEM_PROMPT = (
    "أنت 'هيرمز' (Hermes)، مساعد ذكي وودود وناطق باللغة العربية.\n"
    "تعليمات صارمة وإلزامية لا تقبل الاستثناء:\n"
    "1. تحدث دائماً باللغة العربية الفصحى الواضحة والجميلة أو بلهجة عربية بسيطة ومفهومة فقط.\n"
    "2. يُمنع منعاً باتاً استخدام أي لغة أخرى مثل الهندية أو الأردية أو أي لغات آسيوية أو أجنبية.\n"
    "3. لا تكتب أي حروف أو رموز من أبجديات أخرى غير الحروف العربية (أ-ي) وعلامات الترقيم والأرقام.\n"
    "4. يجب أن تكون جميع ردودك باللغة العربية الصافية السليمة والمفيدة والموجزة."
)

ACTIVE_MODEL = "hermes3:3b"

def clean_arabic_text(text: str) -> str:
    """تنقية النص من أي حروف شاذة أو لغات أجنبية (كالهندية أو الآسيوية)"""
    cleaned = re.sub(r'[\u0900-\u0D7F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]+', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def chat_with_hermes_arabic(messages: list, model: str = ACTIVE_MODEL) -> str:
    """إرسال المحادثة النصية إلى Ollama محلياً"""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.3,
            "top_p": 0.85,
            "repeat_penalty": 1.2,
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

def process_audio_with_gemini(audio_path: str, mime_type: str = "audio/ogg") -> str:
    """قراءة الملف الصوتي وتمريره لنموذج Gemini لمعالجته واستخراج الرد العربي"""
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    prompt = (
        "استمع إلى هذا التسجيل الصوتي جيداً، وافهم ما يقوله المتحدث بدقة، "
        "ثم أجب عليه أو نفذ المطلوب منه بشكل مباشر وواضح باللغة العربية الفصحى أو لهجة عربية مفهومة حصراً. "
        "إذا كان الصوت سؤالاً فأجب عليه، وإذا كان استفساراً فوضحه، ولا تستخدم أي لغة غير العربية إطلاقاً."
    )
    
    last_err = None
    for model_name in GEMINI_AUDIO_MODELS:
        try:
            res = gemini_client.models.generate_content(
                model=model_name,
                contents=[audio_part, prompt],
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "أنت 'هيرمز' (Hermes)، مساعد ذكي وودود وناطق باللغة العربية.\n"
                        "استمع للملف الصوتي وتحدث دائماً باللغة العربية السليمة والواضحة وقدم إجابات دقيقة ومفيدة ومباشرة."
                    ),
                    temperature=0.4,
                )
            )
            return clean_arabic_text(res.text)
        except Exception as e:
            last_err = e
            continue
    raise last_err

def get_user_history(user_id: int) -> list:
    if user_id not in chat_histories:
        chat_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    return chat_histories[user_id]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "صديقي"

    if not is_user_authorized(user_id):
        logger.warning(f"[Security] محاولة وصول غير مصرح بها من {user_name} ({user_id})")
        await update.message.reply_text("⛔ عذراً، هذا البوت خاص ومخصص لمستخدمين مصرح لهم فقط.")
        return

    chat_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
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
    if not is_user_authorized(user_id):
        await update.message.reply_text("⛔ عذراً، هذا البوت خاص ومخصص لمستخدمين مصرح لهم فقط.")
        return

    chat_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    save_memory()
    await update.message.reply_text("🔄 تم مسح وتصفير سجل المحادثة وتجديد الجلسة بنجاح.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message.text
    if not user_msg:
        return
    
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "المستخدم"

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

    history = get_user_history(user_id)
    history.append({"role": "user", "content": user_msg})
    
    if len(history) > 13:
        history = [history[0]] + history[-12:]
        chat_histories[user_id] = history

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    
    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(None, chat_with_hermes_arabic, history, ACTIVE_MODEL)
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
    user_name = update.effective_user.first_name or "المستخدم"

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
        reply = await loop.run_in_executor(None, process_audio_with_gemini, local_path, mime_type)

        if not reply:
            reply = "تم الاستماع للتسجيل الصوتي، ولكن لم أتمكن من استخراج رد مناسب. يرجى إعادة المحاولة."

        # حفظ الرد في سجل محادثة المستخدم مشفراً
        history = get_user_history(user_id)
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
    global ACTIVE_MODEL
    print("=" * 60)
    print("      تشغيل بوت تيليجرام لوكيل هيرمز (Hermes 3)")
    print("      يدعم النصوص محلياً + الرسائل الصوتية عبر Gemini")
    print("      نظام الأمان المتقدم والتشفير الكامل: مفعّل 🔒")
    print("=" * 60)

    if not ensure_ollama_server():
        print("[!] خطأ: تعذر تشغيل محرك Ollama المحلي.")
        return

    ACTIVE_MODEL = select_model()
    print(f"[✓] المحرك المحلي متصل بنجاح (النموذج: {ACTIVE_MODEL})")
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
