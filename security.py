"""
وحدة الأمان والتشفير الشامل (Security & Encryption Suite)
- تشفير الذاكرة وسجلات المحادثة بمفتاح AES-128 / Fernet مشفر على القرص
- حماية الوصول وتقييد البوت بالمستخدمين المصرح لهم فقط (Whitelist / Authorization)
- حماية ضد الإغراق وهجمات الحرمان من الخدمة (Rate Limiting / Flood Protection)
- حذف الملفات الصوتية الحساسة فور انتهاء المعالجة لضمان الخصوصية
"""

import os
import time
import json
import logging
from typing import Dict, List, Optional
from cryptography.fernet import Fernet

logger = logging.getLogger("HermesSecurity")

ENV_FILE = ".env"
MEMORY_ENC_FILE = "memory.enc"
MEMORY_RAW_FILE = "memory.json"

def load_environment(env_path: str = ENV_FILE):
    """تحميل المتغيرات البيئية من .env"""
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        pass

    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception as e:
            logger.warning(f"تعذر قراءة ملف {env_path}: {e}")

load_environment()

def get_or_create_encryption_key() -> bytes:
    """الحصول على مفتاح التشفير المتناظر Fernet أو إنشاؤه وحفظه في .env"""
    key_str = os.environ.get("SECRET_ENCRYPTION_KEY", "").strip()
    if key_str:
        try:
            return key_str.encode("utf-8")
        except Exception:
            pass

    # توليد مفتاح جديد قوي
    new_key = Fernet.generate_key()
    new_key_str = new_key.decode("utf-8")
    os.environ["SECRET_ENCRYPTION_KEY"] = new_key_str

    try:
        with open(ENV_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n# مفتاح التشفير المتناظر للبيانات الحساسة والذاكرة (Fernet / AES)\nSECRET_ENCRYPTION_KEY=\"{new_key_str}\"\n")
        logger.info("[✓] تم توليد مفتاح تشفير أمني وتأمينه في ملف .env")
    except Exception as e:
        logger.warning(f"[!] تعذر كتابة مفتاح التشفير في .env: {e}")

    return new_key

# تهيئة مشفر Fernet
_cipher_suite: Optional[Fernet] = None

def get_cipher() -> Fernet:
    global _cipher_suite
    if _cipher_suite is None:
        key = get_or_create_encryption_key()
        _cipher_suite = Fernet(key)
    return _cipher_suite

# ============================================================
# 1. تشفير وحماية ذاكرة المحادثات على القرص (Data at Rest)
# ============================================================

def save_encrypted_memory(histories: Dict[int, List[dict]]) -> bool:
    """تشفير سجلات المحادثة بالكامل وحفظها كملف مشفر memory.enc"""
    try:
        cipher = get_cipher()
        # تحويل المفاتيح إلى نصوص لضمان التوافق مع JSON
        serializable = {str(k): v for k, v in histories.items()}
        raw_json = json.dumps(serializable, ensure_ascii=False).encode("utf-8")
        encrypted_bytes = cipher.encrypt(raw_json)

        with open(MEMORY_ENC_FILE, "wb") as f:
            f.write(encrypted_bytes)
        return True
    except Exception as e:
        logger.error(f"[!] فشل تشفير الذاكرة: {e}")
        return False

def load_encrypted_memory() -> Dict[int, List[dict]]:
    """قراءة وفك تشفير الذاكرة من القرص، مع الترحيل التلقائي من أي ملف غير مشفر"""
    cipher = get_cipher()

    # 1. فحص الملف المشفر أولاً
    if os.path.exists(MEMORY_ENC_FILE):
        try:
            with open(MEMORY_ENC_FILE, "rb") as f:
                encrypted_bytes = f.read()
            if encrypted_bytes:
                decrypted_json = cipher.decrypt(encrypted_bytes).decode("utf-8")
                raw = json.loads(decrypted_json)
                return {int(k) if str(k).isdigit() else k: v for k, v in raw.items()}
        except Exception as e:
            logger.error(f"[!] خطأ أثناء فك تشفير الذاكرة: {e}")

    # 2. الترحيل من ملف memory.json القديم إن وجد وتشفيره فوراً
    if os.path.exists(MEMORY_RAW_FILE):
        try:
            with open(MEMORY_RAW_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    raw = json.loads(content)
                    histories = {int(k) if str(k).isdigit() else k: v for k, v in raw.items()}
                    # حفظه مشفراً
                    save_encrypted_memory(histories)
                    # إفراغ الملف النصي القديم لحماية الخصوصية
                    with open(MEMORY_RAW_FILE, "w", encoding="utf-8") as fw:
                        fw.write("{}")
                    return histories
        except Exception as e:
            logger.error(f"[!] تعذر استيراد الذاكرة القديمة: {e}")

    return {}

# ============================================================
# 2. تقييد الوصول للبوت (Authorization & Whitelist)
# ============================================================

def get_allowed_users() -> Optional[List[int]]:
    """قراءة قائمة معرفات المستخدمين المصرح لهم من .env"""
    allowed_str = os.environ.get("ALLOWED_USER_IDS", "").strip()
    if not allowed_str or allowed_str.lower() in ["all", "*"]:
        return None  # متاح للجميع
    users = []
    for uid in allowed_str.split(","):
        uid = uid.strip()
        if uid.isdigit():
            users.append(int(uid))
    return users

def is_user_authorized(user_id: int) -> bool:
    """التحقق هل المستخدم مصرح له باستخدام البوت"""
    allowed = get_allowed_users()
    if allowed is None:
        return True
    return user_id in allowed

# ============================================================
# 3. الحماية ضد الإغراق والرسائل المتتالية (Rate Limiter)
# ============================================================

_user_last_requests: Dict[int, List[float]] = {}

def is_rate_limited(user_id: int, max_calls: int = 5, window_seconds: float = 10.0) -> bool:
    """منع إرسال أكثر من max_calls رسالة خلال window_seconds ثانية"""
    now = time.time()
    history = _user_last_requests.setdefault(user_id, [])
    # تصفية الطلبات القديمة خارج النافذة الزمنية
    _user_last_requests[user_id] = [t for t in history if now - t < window_seconds]
    if len(_user_last_requests[user_id]) >= max_calls:
        return True
    _user_last_requests[user_id].append(now)
    return False

# ============================================================
# 4. الحذف الآمن للملفات الصوتية بعد المعالجة
# ============================================================

def secure_delete_file(file_path: str) -> bool:
    """حذف الملف الصوتي الحساس فور معالجته لمنع بقائه على القرص"""
    if not file_path or not os.path.exists(file_path):
        return False
    try:
        # إتلاف المحتوى قبل الحذف
        try:
            length = os.path.getsize(file_path)
            with open(file_path, "wb") as f:
                f.write(os.urandom(min(length, 4096)))
        except Exception:
            pass
        os.remove(file_path)
        return True
    except Exception as e:
        logger.warning(f"تعذر الحذف الآمن للملف {file_path}: {e}")
        return False

# ============================================================
# 5. تنقية وتعتيم السجلات (Log Sanitization)
# ============================================================

def sanitize_text(text: str, max_preview: int = 40) -> str:
    """تعتيم النصوص الحساسة لمنع ظهور تفاصيل الرسائل كاملة في الـ Console"""
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    if len(text) <= max_preview:
        return text
    return f"{text[:max_preview]}... [محمي]"
