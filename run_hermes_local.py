"""
تشغيل نموذج هيرمز (Hermes 3) محلياً بالكامل عبر Ollama
بدون أي اتصال سحابي أو مفاتيح API أو قيود جغرافية
"""

import sys
import os
import json
import time
import subprocess
import urllib.request
import urllib.error

# ضبط الترميز لطباعة اللغة العربية بشكل سليم في ويندوز
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "hermes3:3b"

# مسارات محتملة لـ ollama.exe
OLLAMA_EXE_PATHS = [
    "ollama",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Ollama.Ollama.Portable_Microsoft.Winget.Source_8wekyb3d8bbwe\ollama.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
]

def check_ollama_running() -> bool:
    try:
        req = urllib.request.Request(f"{OLLAMA_BASE_URL}/", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False

def ensure_ollama_server():
    """التحقق من تشغيل السيرفر وتشغيله تلقائياً في الخلفية إذا لزم الأمر"""
    if check_ollama_running():
        return True

    print("[*] جاري تشغيل محرك Ollama في الخلفية...")
    for exe in OLLAMA_EXE_PATHS:
        try:
            subprocess.Popen(
                [exe, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            break
        except Exception:
            continue

    # انتظار جاهزية السيرفر حتى 10 ثوانٍ
    for _ in range(10):
        time.sleep(1)
        if check_ollama_running():
            print("[✓] تم تشغيل المحرك المحلي بنجاح.")
            return True

    return False

def get_installed_models() -> list:
    try:
        req = urllib.request.Request(f"{OLLAMA_BASE_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []

def select_model() -> str:
    models = get_installed_models()
    if DEFAULT_MODEL in models:
        return DEFAULT_MODEL
    # البحث عن أي موديل يحتوي على hermes
    for m in models:
        if "hermes" in m.lower():
            return m
    if models:
        return models[0]
    return DEFAULT_MODEL

def chat_with_ollama(messages: list, model: str) -> str:
    """إرسال المحادثة واستقبال الرد مباشرة من خادم Ollama المحلي"""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.7,
            "top_p": 0.9,
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
        return res_json.get("message", {}).get("content", "")

def main():
    print("=" * 60)
    print("   وكيل هيرمز (Hermes 3) - يعمل محلياً 100% دون إنترنت")
    print("=" * 60)

    if not ensure_ollama_server():
        print("\n[خطأ]: تعذر الاتصال بمحرك Ollama المحلي.")
        print("يرجى تشغيله يدوياً عبر: ollama serve")
        return

    active_model = select_model()
    print(f"\n[✓] النموذج المحلي النشط: {active_model}")
    print("اكتب رسالتك واضغط Enter (أو اكتب 'خروج' للإنهاء):\n")

    history = [
        {
            "role": "system",
            "content": "أنت 'هيرمز' (Hermes)، مساعد ذكي وقوي ومتعدد المهام ومبني على نموذج Hermes 3 من Nous Research. تجيب بدقة ووضوح باللغة العربية."
        }
    ]

    while True:
        try:
            user_msg = input("أنت: ")
        except (EOFError, KeyboardInterrupt):
            print("\nمع السلامة يا أبو يوسف!")
            break

        if user_msg.strip() in ["خروج", "exit"]:
            print("مع السلامة يا أبو يوسف!")
            break
        if not user_msg.strip():
            continue

        history.append({"role": "user", "content": user_msg})

        try:
            reply = chat_with_ollama(history, model=active_model)
            print(f"\nهيرمز (محلي): {reply}\n")
            history.append({"role": "assistant", "content": reply})
        except Exception as e:
            print(f"\n[خطأ أثناء المعالجة]: {e}\n")

if __name__ == "__main__":
    main()
