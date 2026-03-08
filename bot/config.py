"""
Конфигурация SNI Monitoring Panel.
Все чувствительные данные читаются из переменных окружения.
Поддерживается .env файл (python-dotenv, если установлен).
"""

import os
import sys
from pathlib import Path

# Пробуем загрузить .env (опционально)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── Telegram ──────────────────────────────────────────────────────────────────
_token = os.environ.get("TG_BOT_TOKEN", "").strip()
if not _token:
    print("[FATAL] Переменная TG_BOT_TOKEN не задана.", file=sys.stderr)
    sys.exit(1)
BOT_TOKEN: str = _token

_admins = os.environ.get("TG_ADMIN_IDS", "").strip()
if not _admins:
    print("[FATAL] Переменная TG_ADMIN_IDS не задана.", file=sys.stderr)
    sys.exit(1)
try:
    ADMIN_IDS: set[int] = {int(x) for x in _admins.split(",") if x.strip()}
except ValueError:
    print("[FATAL] TG_ADMIN_IDS должна содержать числа через запятую.", file=sys.stderr)
    sys.exit(1)

# ── Пути ─────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
DB_PATH       = BASE_DIR / "data" / "sni_monitor.db"
SNI_LIST_PATH = BASE_DIR / "data" / "sni.txt"

# Создаём папку data при необходимости
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── SSH ───────────────────────────────────────────────────────────────────────
SSH_USER = os.environ.get("SSH_USER", "root")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))

# ── Воркер ───────────────────────────────────────────────────────────────────
REMOTE_DIR  = os.environ.get("REMOTE_DIR", "/opt/sni_worker")
CONCURRENCY = int(os.environ.get("SNI_CONCURRENCY", "50"))

# ── История ──────────────────────────────────────────────────────────────────
HISTORY_KEEP = int(os.environ.get("HISTORY_KEEP", "20"))

# ── Крон-расписание ──────────────────────────────────────────────────────────
CRON_HOUR   = int(os.environ.get("CRON_HOUR",   "9"))
CRON_MINUTE = int(os.environ.get("CRON_MINUTE", "0"))
