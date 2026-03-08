#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SNI Monitoring Panel — ежедневная автопроверка (запускается из cron).

Выполняет run_check по всем серверам из БД и отправляет отчёты
через Telegram bot API напрямую (без запущенного бота, только urllib).

Использование:
    python3 cron_check.py                  # все серверы
    python3 cron_check.py --server-id 3   # один сервер
    python3 cron_check.py --dry-run        # показать без запуска
"""

import argparse
import asyncio
import json
import logging
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from config import BOT_TOKEN, ADMIN_IDS, DB_PATH, HISTORY_KEEP
from db import (
    init_db, get_servers, get_server,
    update_server_status, save_result, prune_old_results,
)
from ssh_worker import run_check
from report import (
    ScanStats, parse_results_jsonl,
    format_report, format_summary_report,
)

import aiosqlite

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("cron")


# ══════════════════════════════════════════════════════════════════════════════
#  Telegram (urllib — без aiogram)
# ══════════════════════════════════════════════════════════════════════════════

def _tg_post(method: str, payload: dict) -> bool:
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload, ensure_ascii=False).encode()
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        log.warning("TG %s: %s", method, e)
        return False


def tg_send(text: str) -> None:
    """Отправляет сообщение всем администраторам."""
    # Telegram limit: 4096 символов
    if len(text) > 4096:
        text = text[:4050] + "\n\n<i>…текст обрезан</i>"
    for admin_id in ADMIN_IDS:
        _tg_post("sendMessage", {
            "chat_id": admin_id,
            "text": text,
            "parse_mode": "HTML",
        })


# ══════════════════════════════════════════════════════════════════════════════
#  Проверка одного сервера
# ══════════════════════════════════════════════════════════════════════════════

async def check_server(server: dict) -> Optional[tuple[str, ScanStats]]:
    """Запускает воркер, сохраняет результат. Возвращает (name, stats) или None."""
    name = server["name"]
    log.info("[%s] Запуск проверки…", name)

    ok, output, jsonl_bytes = await asyncio.to_thread(
        run_check,
        server["ip"],
        server["ssh_credentials"],
        name,
        server["id"],
    )

    ts = datetime.now().isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH) as db:
        await update_server_status(db, server["id"], "ok" if ok else "error", ts)

    if not ok:
        log.error("[%s] Ошибка: %s", name, output[-300:])
        tg_send(
            f"❌ <b>Автопроверка · {name}</b>\n\n"
            f"<code>{output[:700]}</code>"
        )
        return None

    stats = parse_results_jsonl(jsonl_bytes or b"")
    log.info("[%s] ✅%d ❌%d ⏱%.0fs", name, stats.working, stats.errors, stats.elapsed_sec)

    async with aiosqlite.connect(DB_PATH) as db:
        await save_result(
            db,
            server_id    = server["id"],
            checked_at   = ts,
            total        = stats.total,
            working      = stats.working,
            blocked      = stats.blocked,
            inconclusive = stats.inconclusive,
            elapsed_sec  = stats.elapsed_sec,
            min_rtt      = stats.min_rtt,
            avg_rtt      = stats.avg_rtt,
            max_rtt      = stats.max_rtt,
            blocked_snis = stats.blocked_snis,
        )
        await prune_old_results(db, server["id"], keep=HISTORY_KEEP)

    return name, stats


# ══════════════════════════════════════════════════════════════════════════════
#  Главный процесс
# ══════════════════════════════════════════════════════════════════════════════

async def run(server_id: Optional[int], dry_run: bool) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await init_db(db)
        if server_id is not None:
            s = await get_server(db, server_id)
            servers = [s] if s else []
        else:
            servers = await get_servers(db)

    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    if not servers:
        log.warning("Нет серверов для проверки")
        tg_send(
            f"🕐 <b>Автопроверка SNI · {now}</b>\n\n"
            "📭 Серверов нет. Добавьте их через /start в боте."
        )
        return 0

    if dry_run:
        print(f"\nDRY RUN — серверов: {len(servers)}")
        for s in servers:
            print(f"  • [{s['id']}] {s['name']}  {s['ip']}")
        return 0

    # Уведомление о старте
    tg_send(
        f"🕐 <b>Автопроверка начата · {now}</b>\n"
        f"Серверов: <b>{len(servers)}</b>\n"
        "Отчёты придут по завершении каждого."
    )

    # Параллельный запуск
    collected: list[tuple[str, ScanStats]] = []
    failed:    list[str] = []
    lock = asyncio.Lock()

    async def _one(srv: dict) -> None:
        result = await check_server(srv)
        async with lock:
            if result:
                # Одиночный отчёт по серверу
                name, stats = result
                tg_send(format_report(name, stats,
                                      checked_at=datetime.now().isoformat("seconds")))
                collected.append(result)
            else:
                failed.append(srv["name"])

    await asyncio.gather(*[_one(s) for s in servers])

    # Сводный отчёт (когда серверов > 1)
    if len(servers) > 1 and collected:
        tg_send(format_summary_report(collected))

    # Финальная строка с итогом
    if failed:
        tg_send(
            f"⚠️ <b>Автопроверка завершена с ошибками</b>\n"
            f"✅ Успешно: {len(collected)}  ❌ Ошибки: {len(failed)}\n"
            f"Не прошли: {', '.join(failed)}"
        )

    log.info("Готово. Успешно: %d, Ошибок: %d", len(collected), len(failed))
    return 1 if failed else 0


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(description="SNI автопроверка")
    ap.add_argument("--server-id", type=int, default=None,
                    help="Проверить только один сервер по ID")
    ap.add_argument("--dry-run",   action="store_true",
                    help="Показать список без запуска")
    args = ap.parse_args()

    log.info("══ Автопроверка SNI · %s ══",
             datetime.now().strftime("%d.%m.%Y %H:%M"))
    sys.exit(asyncio.run(run(args.server_id, args.dry_run)))


if __name__ == "__main__":
    main()
