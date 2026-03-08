#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SNI Monitoring Panel — Telegram Bot.
Управление только через inline-кнопки. Единственная команда: /start.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)

from config import BOT_TOKEN, ADMIN_IDS, DB_PATH, SNI_LIST_PATH, HISTORY_KEEP, CRON_HOUR, CRON_MINUTE
from db import (
    init_db, add_server, get_servers, get_server,
    delete_server, update_server_status,
    save_result, get_recent_results, get_result, prune_old_results,
)
from ssh_worker import deploy_worker, run_check
from report import (
    ScanStats, parse_results_jsonl,
    format_report, format_history_list,
    format_history_detail, format_summary_report,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


# ══════════════════════════════════════════════════════════════════════════════
#  FSM: добавление сервера (3 шага)
# ══════════════════════════════════════════════════════════════════════════════

class AddServer(StatesGroup):
    waiting_name = State()
    waiting_ip   = State()
    waiting_cred = State()


# ══════════════════════════════════════════════════════════════════════════════
#  Клавиатуры
# ══════════════════════════════════════════════════════════════════════════════

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖥  Серверы",              callback_data="menu:servers")],
        [InlineKeyboardButton(text="➕  Добавить сервер",      callback_data="menu:add_server")],
        [InlineKeyboardButton(text="🚀  Проверить все сейчас", callback_data="menu:check_all")],
        [InlineKeyboardButton(text="⏰  Расписание",           callback_data="menu:schedule")],
    ])


def kb_servers_list(servers: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for s in servers:
        icon = _status_icon(s["status"])
        rows.append([InlineKeyboardButton(
            text=f"{icon} {s['name']}  ({s['ip']})",
            callback_data=f"srv:open:{s['id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_server_card(server_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="▶️ Проверить",  callback_data=f"srv:check:{server_id}"),
            InlineKeyboardButton(text="🚀 Развернуть", callback_data=f"srv:deploy:{server_id}"),
        ],
        [
            InlineKeyboardButton(text="📂 История",   callback_data=f"hist:list:{server_id}"),
            InlineKeyboardButton(text="🗑 Удалить",    callback_data=f"srv:delete:{server_id}"),
        ],
        [InlineKeyboardButton(text="◀️ К списку",     callback_data="menu:servers")],
    ])


def kb_history_list(server_id: int, results: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for r in results:
        errors = r["blocked"] + r["inconclusive"]
        icon   = "✅" if errors == 0 else ("⚠️" if errors < 5 else "❌")
        pct    = round(r["working"] / r["total"] * 100) if r["total"] else 0
        ts     = _dt_short(r["checked_at"])
        rows.append([InlineKeyboardButton(
            text=f"{icon} {ts}  ·  {pct}%  ❌ {errors}",
            callback_data=f"hist:view:{server_id}:{r['id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ К серверу",
                                      callback_data=f"srv:open:{server_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_history_detail(server_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К истории", callback_data=f"hist:list:{server_id}")],
        [InlineKeyboardButton(text="🖥 К серверу",  callback_data=f"srv:open:{server_id}")],
    ])


def kb_confirm_delete(server_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"srv:confirm_del:{server_id}"),
        InlineKeyboardButton(text="❌ Отмена",      callback_data=f"srv:open:{server_id}"),
    ]])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="menu:cancel"),
    ]])


def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main"),
    ]])


def kb_after_check(server_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Снова",     callback_data=f"srv:check:{server_id}"),
            InlineKeyboardButton(text="📂 История",   callback_data=f"hist:list:{server_id}"),
        ],
        [
            InlineKeyboardButton(text="◀️ К серверу", callback_data=f"srv:open:{server_id}"),
            InlineKeyboardButton(text="🏠 Главное",   callback_data="menu:main"),
        ],
    ])


# ══════════════════════════════════════════════════════════════════════════════
#  Вспомогательные функции
# ══════════════════════════════════════════════════════════════════════════════

def _status_icon(status: str) -> str:
    return {"ok": "✅", "error": "❌", "unknown": "❓"}.get(status, "❓")


def _dt_short(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
    except Exception:
        return iso


def _dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso


async def _edit(msg: Message, text: str,
                reply_markup=None) -> None:
    """Безопасный edit_text — подавляет 'message is not modified'."""
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            log.warning("edit_text error: %s", e)


def _validate_ip(ip: str) -> tuple[bool, str]:
    """Возвращает (True, "") или (False, "описание ошибки")."""
    ip = ip.strip()
    if not ip:
        return False, "IP-адрес не может быть пустым."

    bad = set(ip) - set("0123456789.")
    if bad:
        chars = ", ".join(f"<code>{c}</code>" for c in sorted(bad))
        return False, f"Недопустимые символы: {chars}\nИспользуйте только цифры и точки."

    parts = ip.split(".")
    if len(parts) != 4:
        return False, (
            f"Нужно ровно <b>4 октета</b>, разделённых точками — "
            f"получено <b>{len(parts)}</b>.\n"
            f"Пример: <code>185.23.104.77</code>"
        )

    for i, p in enumerate(parts, 1):
        if not p:
            return False, f"Октет #{i} пустой — лишняя или пропущенная точка."
        if len(p) > 1 and p[0] == "0":
            return False, (
                f"Октет #{i} <code>{p}</code> содержит ведущий ноль.\n"
                f"Напишите <code>{int(p)}</code>."
            )
        v = int(p)
        if not (0 <= v <= 255):
            return False, f"Октет #{i} <code>{p}</code> выходит за пределы 0–255."

    if ip in ("0.0.0.0", "255.255.255.255"):
        return False, f"Адрес <code>{ip}</code> недопустим."

    return True, ""


def _parse_cred(ip: str, cred: str) -> dict:
    from config import SSH_USER, SSH_PORT
    base = {"hostname": ip, "username": SSH_USER, "port": SSH_PORT}
    if cred.startswith("password:"):
        base["password"] = cred[len("password:"):]
    else:
        base["key_filename"] = cred
    return base


def _test_ssh(ip: str, cred: str) -> tuple[bool, str]:
    try:
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(**_parse_cred(ip, cred), timeout=10)
        c.close()
        return True, ""
    except Exception as e:
        return False, str(e)


def _truncate(text: str, limit: int = 4096) -> str:
    """Обрезает текст до лимита Telegram с пометкой."""
    if len(text) <= limit:
        return text
    return text[: limit - 60] + "\n\n<i>…текст обрезан, полный список — в файле</i>"


# ══════════════════════════════════════════════════════════════════════════════
#  Экраны (screen renderers)
# ══════════════════════════════════════════════════════════════════════════════

async def screen_main(target) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        servers = await get_servers(db)

    ok_c  = sum(1 for s in servers if s["status"] == "ok")
    err_c = sum(1 for s in servers if s["status"] == "error")
    unk_c = sum(1 for s in servers if s["status"] == "unknown")

    cron_str = _cron_status()
    text = (
        "🖥 <b>SNI Monitoring Panel</b>\n\n"
        f"Серверов: <b>{len(servers)}</b>   "
        f"✅ {ok_c}  ❌ {err_c}  ❓ {unk_c}\n"
        f"⏰ Автопроверка: {cron_str}"
    )
    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb_main())
    else:
        await _edit(target, text, reply_markup=kb_main())


async def screen_servers(target) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        servers = await get_servers(db)

    if not servers:
        text = "📭 <b>Серверов пока нет.</b>\n\nДобавьте первый через главное меню."
        kb   = kb_back_main()
    else:
        lines = ["🖥 <b>Список серверов:</b>\n"]
        for s in servers:
            icon = _status_icon(s["status"])
            last = _dt_short(s["last_check_time"]) if s["last_check_time"] else "—"
            lines.append(f"{icon} <b>{s['name']}</b>  <code>{s['ip']}</code>  ⏱ {last}")
        text = "\n".join(lines)
        kb   = kb_servers_list(servers)

    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        await _edit(target, text, reply_markup=kb)


async def screen_server_card(target, server_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        s = await get_server(db, server_id)
    if not s:
        await screen_servers(target)
        return

    icon   = _status_icon(s["status"])
    last   = _dt(s["last_check_time"]) if s["last_check_time"] else "никогда"
    cred_t = "🔐 Пароль" if s["ssh_credentials"].startswith("password:") else "🔑 SSH-ключ"

    text = (
        f"{icon} <b>{s['name']}</b>\n\n"
        f"🌐 IP: <code>{s['ip']}</code>\n"
        f"{cred_t}\n"
        f"📅 Последняя проверка: {last}\n"
        f"📊 Статус: <b>{s['status'].upper()}</b>"
    )
    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML",
                            reply_markup=kb_server_card(server_id))
    else:
        await _edit(target, text, reply_markup=kb_server_card(server_id))


# ══════════════════════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════════════════════

@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext) -> None:
    if msg.from_user.id not in ADMIN_IDS:
        await msg.answer("⛔️ Доступ запрещён.")
        return
    await state.clear()
    await screen_main(msg)


# ══════════════════════════════════════════════════════════════════════════════
#  Главная навигация
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "menu:main")
async def cb_main(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await screen_main(call.message)


@dp.callback_query(F.data == "menu:servers")
async def cb_servers(call: CallbackQuery) -> None:
    await call.answer()
    await screen_servers(call.message)


@dp.callback_query(F.data == "menu:cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Отменено")
    await screen_main(call.message)


# ══════════════════════════════════════════════════════════════════════════════
#  FSM: добавление сервера
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "menu:add_server")
async def cb_add_server(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AddServer.waiting_name)
    await _edit(
        call.message,
        "➕ <b>Добавление сервера</b>\n\n"
        "<b>Шаг 1 / 3</b> — Введите <b>название</b>:\n"
        "<i>Например: Finland-1, Germany-VPS</i>",
        reply_markup=kb_cancel(),
    )


@dp.message(AddServer.waiting_name)
async def fsm_name(msg: Message, state: FSMContext) -> None:
    name = (msg.text or "").strip()
    if not name:
        await msg.answer("⚠️ Название не может быть пустым:",
                         reply_markup=kb_cancel())
        return
    await state.update_data(name=name)
    await state.set_state(AddServer.waiting_ip)
    await msg.answer(
        f"✅ Название: <b>{name}</b>\n\n"
        "<b>Шаг 2 / 3</b> — Введите <b>IP-адрес</b>:\n"
        "<i>Например: 185.23.104.77</i>",
        parse_mode="HTML",
        reply_markup=kb_cancel(),
    )


@dp.message(AddServer.waiting_ip)
async def fsm_ip(msg: Message, state: FSMContext) -> None:
    ip = (msg.text or "").strip()
    valid, reason = _validate_ip(ip)
    if not valid:
        await msg.answer(
            f"❌ <b>Некорректный IP-адрес</b>\n\n{reason}\n\nПопробуйте ещё раз:",
            parse_mode="HTML",
            reply_markup=kb_cancel(),
        )
        return
    await state.update_data(ip=ip)
    await state.set_state(AddServer.waiting_cred)
    await msg.answer(
        f"✅ IP: <code>{ip}</code>\n\n"
        "<b>Шаг 3 / 3</b> — Введите <b>SSH-данные</b>:\n\n"
        "🔑 Путь к ключу: <code>/root/.ssh/id_rsa</code>\n"
        "🔐 Пароль:       <code>password:ваш_пароль</code>",
        parse_mode="HTML",
        reply_markup=kb_cancel(),
    )


@dp.message(AddServer.waiting_cred)
async def fsm_cred(msg: Message, state: FSMContext) -> None:
    cred = (msg.text or "").strip()
    data = await state.get_data()
    await state.clear()

    wait = await msg.answer(
        f"⏳ Проверяю SSH-соединение с <code>{data['ip']}</code>…",
        parse_mode="HTML",
    )
    ok, err = await asyncio.to_thread(_test_ssh, data["ip"], cred)

    if not ok:
        await wait.edit_text(
            f"❌ Не удалось подключиться к <b>{data['ip']}</b>:\n"
            f"<code>{err}</code>\n\nСервер <b>не сохранён</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Попробовать снова",
                                      callback_data="menu:add_server")],
                [InlineKeyboardButton(text="🏠 Главное меню",
                                      callback_data="menu:main")],
            ]),
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        server_id = await add_server(db, data["name"], data["ip"], cred)

    await wait.edit_text(
        f"✅ Сервер <b>{data['name']}</b> добавлен!\n\n"
        f"🌐 IP: <code>{data['ip']}</code>\n\n"
        "Нажмите <b>«Развернуть»</b>, чтобы установить sni.py на сервер.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Развернуть воркер",
                                  callback_data=f"srv:deploy:{server_id}")],
            [InlineKeyboardButton(text="◀️ К списку серверов",
                                  callback_data="menu:servers")],
        ]),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Карточка сервера
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("srv:open:"))
async def cb_srv_open(call: CallbackQuery) -> None:
    server_id = int(call.data.split(":")[2])
    await call.answer()
    await screen_server_card(call.message, server_id)


# ══════════════════════════════════════════════════════════════════════════════
#  Развёртывание воркера
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("srv:deploy:"))
async def cb_deploy(call: CallbackQuery) -> None:
    server_id = int(call.data.split(":")[2])
    await call.answer()

    async with aiosqlite.connect(DB_PATH) as db:
        server = await get_server(db, server_id)
    if not server:
        await _edit(call.message, "❌ Сервер не найден.",
                    reply_markup=kb_back_main())
        return

    await _edit(
        call.message,
        f"⏳ Разворачиваю воркер на <b>{server['name']}</b>…\n"
        "Устанавливаю зависимости и копирую файлы.",
    )

    ok, err = await asyncio.to_thread(
        deploy_worker,
        server["ip"], server["ssh_credentials"], SNI_LIST_PATH,
    )

    if ok:
        await _edit(
            call.message,
            f"✅ Воркер успешно развёрнут на <b>{server['name']}</b>!\n\n"
            "Теперь можно запустить проверку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Запустить проверку",
                                      callback_data=f"srv:check:{server_id}")],
                [InlineKeyboardButton(text="◀️ К серверу",
                                      callback_data=f"srv:open:{server_id}")],
            ]),
        )
    else:
        await _edit(
            call.message,
            f"❌ Ошибка развёртывания на <b>{server['name']}</b>:\n\n"
            f"<code>{err[:900]}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить",
                                      callback_data=f"srv:deploy:{server_id}")],
                [InlineKeyboardButton(text="◀️ К серверу",
                                      callback_data=f"srv:open:{server_id}")],
            ]),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Запуск проверки
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("srv:check:"))
async def cb_check(call: CallbackQuery) -> None:
    server_id = int(call.data.split(":")[2])
    await call.answer()
    await _run_check(call.message, server_id)


async def _run_check(msg: Message,
                     server_id: int) -> Optional[tuple[str, ScanStats]]:
    """
    Полный цикл проверки одного сервера:
      1. Спиннер ожидания (обновляется каждые 4 с)
      2. SSH-запуск воркера
      3. Сохранение результатов в БД
      4. Отправка отчёта + jsonl-файла
    Возвращает (server_name, stats) или None при ошибке.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        server = await get_server(db, server_id)
    if not server:
        await _edit(msg, "❌ Сервер не найден.", reply_markup=kb_back_main())
        return None

    # ── Спиннер ──────────────────────────────────────────────────────────────
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    stop   = asyncio.Event()

    async def _spin() -> None:
        i = 0
        while not stop.is_set():
            await _edit(
                msg,
                f"{frames[i % len(frames)]} <b>Проверка · {server['name']}</b>\n\n"
                f"🌐 IP: <code>{server['ip']}</code>\n"
                "⏳ Запускаю воркер…",
            )
            await asyncio.sleep(4)
            i += 1

    spin = asyncio.create_task(_spin())

    # ── Запуск воркера через SSH ──────────────────────────────────────────────
    ok, output, jsonl_bytes = await asyncio.to_thread(
        run_check,
        server["ip"], server["ssh_credentials"],
        server["name"], server_id,
    )

    stop.set()
    spin.cancel()
    try:
        await spin
    except asyncio.CancelledError:
        pass

    ts_now = datetime.now().isoformat(timespec="seconds")

    # ── Обновить статус ───────────────────────────────────────────────────────
    async with aiosqlite.connect(DB_PATH) as db:
        await update_server_status(
            db, server_id, "ok" if ok else "error", ts_now
        )

    # ── Ошибка SSH/воркера ────────────────────────────────────────────────────
    if not ok:
        await _edit(
            msg,
            f"❌ <b>Ошибка на {server['name']}</b>\n\n"
            f"<code>{output[:900]}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить",
                                      callback_data=f"srv:check:{server_id}")],
                [InlineKeyboardButton(text="◀️ К серверу",
                                      callback_data=f"srv:open:{server_id}")],
                [InlineKeyboardButton(text="🏠 Главное меню",
                                      callback_data="menu:main")],
            ]),
        )
        return None

    # ── Парсинг ───────────────────────────────────────────────────────────────
    stats = parse_results_jsonl(jsonl_bytes or b"")

    # ── Сохранение в историю ─────────────────────────────────────────────────
    async with aiosqlite.connect(DB_PATH) as db:
        await save_result(
            db,
            server_id    = server_id,
            checked_at   = ts_now,
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
        await prune_old_results(db, server_id, keep=HISTORY_KEEP)

    # ── Отчёт ─────────────────────────────────────────────────────────────────
    report = format_report(server["name"], stats, checked_at=ts_now)
    await _edit(msg, _truncate(report), reply_markup=kb_after_check(server_id))

    # ── Файл с сырыми данными ─────────────────────────────────────────────────
    if jsonl_bytes:
        fname   = f"sni_{server['name']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        caption = (
            f"📎 <b>{server['name']}</b> · "
            f"✅{stats.working} ❌{stats.errors} ⏱{stats.elapsed_sec:.0f}с"
        )
        await msg.answer_document(
            BufferedInputFile(jsonl_bytes, filename=fname),
            caption=caption,
            parse_mode="HTML",
        )

    return server["name"], stats


# ══════════════════════════════════════════════════════════════════════════════
#  История проверок
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("hist:list:"))
async def cb_history_list(call: CallbackQuery) -> None:
    server_id = int(call.data.split(":")[2])
    await call.answer()

    async with aiosqlite.connect(DB_PATH) as db:
        server  = await get_server(db, server_id)
        results = await get_recent_results(db, server_id, limit=10)

    if not server:
        await screen_servers(call.message)
        return

    text = format_history_list(server["name"], results)
    await _edit(call.message, text,
                reply_markup=kb_history_list(server_id, results))


@dp.callback_query(F.data.startswith("hist:view:"))
async def cb_history_view(call: CallbackQuery) -> None:
    # callback_data: hist:view:{server_id}:{result_id}
    parts     = call.data.split(":")
    server_id = int(parts[2])
    result_id = int(parts[3])
    await call.answer()

    async with aiosqlite.connect(DB_PATH) as db:
        server = await get_server(db, server_id)
        result = await get_result(db, result_id)

    if not server or not result:
        # Запись удалена — возвращаем к списку
        async with aiosqlite.connect(DB_PATH) as db:
            results = await get_recent_results(db, server_id, limit=10)
        name = server["name"] if server else "—"
        text = format_history_list(name, results)
        await _edit(call.message, text,
                    reply_markup=kb_history_list(server_id, results))
        return

    text = format_history_detail(server["name"], result)
    await _edit(call.message, _truncate(text),
                reply_markup=kb_history_detail(server_id))


# ══════════════════════════════════════════════════════════════════════════════
#  Удаление сервера
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("srv:delete:"))
async def cb_delete(call: CallbackQuery) -> None:
    server_id = int(call.data.split(":")[2])
    await call.answer()

    async with aiosqlite.connect(DB_PATH) as db:
        server = await get_server(db, server_id)
    if not server:
        await screen_servers(call.message)
        return

    await _edit(
        call.message,
        f"⚠️ Удалить <b>{server['name']}</b> (<code>{server['ip']}</code>)?\n\n"
        "Вся история проверок также будет удалена. Это необратимо.",
        reply_markup=kb_confirm_delete(server_id),
    )


@dp.callback_query(F.data.startswith("srv:confirm_del:"))
async def cb_confirm_delete(call: CallbackQuery) -> None:
    server_id = int(call.data.split(":")[2])
    await call.answer()

    async with aiosqlite.connect(DB_PATH) as db:
        server = await get_server(db, server_id)
        await delete_server(db, server_id)   # CASCADE удалит results

    name = server["name"] if server else f"#{server_id}"
    await _edit(
        call.message,
        f"🗑 Сервер <b>{name}</b> удалён.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ К списку",
                                 callback_data="menu:servers"),
            InlineKeyboardButton(text="🏠 Главное меню",
                                 callback_data="menu:main"),
        ]]),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Проверить все серверы
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "menu:check_all")
async def cb_check_all(call: CallbackQuery) -> None:
    await call.answer()

    async with aiosqlite.connect(DB_PATH) as db:
        servers = await get_servers(db)

    if not servers:
        await _edit(call.message, "📭 Нет серверов для проверки.",
                    reply_markup=kb_back_main())
        return

    await _edit(
        call.message,
        f"🚀 <b>Запускаю проверку на {len(servers)} серверах параллельно…</b>\n\n"
        "Отчёт по каждому придёт отдельным сообщением.\n"
        "В конце — сводный отчёт по всем.",
        reply_markup=kb_back_main(),
    )

    collected: list[tuple[str, ScanStats]] = []
    lock = asyncio.Lock()

    async def _one(server: dict) -> None:
        placeholder = await call.message.answer(
            f"⠋ <b>{server['name']}</b> — ожидание…",
            parse_mode="HTML",
        )
        result = await _run_check(placeholder, server["id"])
        if result is not None:
            async with lock:
                collected.append(result)

    await asyncio.gather(*[_one(s) for s in servers])

    # Сводный отчёт (только если серверов > 1)
    if len(collected) > 1:
        summary = format_summary_report(collected)
        await call.message.answer(
            _truncate(summary),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Проверить все снова",
                                     callback_data="menu:check_all"),
                InlineKeyboardButton(text="🏠 Главное меню",
                                     callback_data="menu:main"),
            ]]),
        )



# ══════════════════════════════════════════════════════════════════════════════
#  Расписание (крон)
# ══════════════════════════════════════════════════════════════════════════════

def _cron_status() -> str:
    """Возвращает строку о текущем статусе крон-задачи."""
    try:
        import subprocess
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5
        )
        if "cron_check.py" in result.stdout:
            # Вытаскиваем время из строки крона
            for line in result.stdout.splitlines():
                if "cron_check.py" in line and not line.startswith("#"):
                    parts = line.split()
                    if len(parts) >= 2:
                        m, h = parts[0], parts[1]
                        try:
                            return f"<b>включена</b> каждый день в {int(h):02d}:{int(m):02d}"
                        except ValueError:
                            pass
            return "<b>включена</b>"
        return "выключена"
    except Exception:
        return "неизвестно"


def _set_cron(hour: int, minute: int) -> tuple[bool, str]:
    """Устанавливает крон-задачу. Возвращает (success, message)."""
    import subprocess
    from pathlib import Path

    venv_py = Path("/opt/sni_monitor/venv/bin/python3")
    system_py = Path("/usr/bin/python3")
    py = str(venv_py) if venv_py.exists() else str(system_py)

    script = Path("/opt/sni_monitor/bot/cron_check.py")
    if not script.exists():
        # Попробуем найти рядом с bot.py
        script = Path(__file__).parent / "cron_check.py"

    log_path = "/var/log/sni_monitor/cron.log"
    cron_cmd = f"{py} {script} >> {log_path} 2>&1"
    new_job  = f"{minute} {hour} * * * {cron_cmd}"
    marker   = "# SNI_MONITOR_DAILY_CHECK"

    try:
        # Читаем текущий crontab
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        current = result.stdout if result.returncode == 0 else ""

        # Убираем старую запись SNI
        lines = [
            ln for ln in current.splitlines()
            if "cron_check.py" not in ln and "SNI_MONITOR" not in ln
        ]
        lines += [marker, new_job]

        new_crontab = "\n".join(lines) + "\n"
        proc = subprocess.run(["crontab", "-"], input=new_crontab,
                               capture_output=True, text=True)
        if proc.returncode != 0:
            return False, proc.stderr[:300]
        return True, ""
    except Exception as e:
        return False, str(e)


def _remove_cron() -> tuple[bool, str]:
    """Удаляет крон-задачу SNI из crontab."""
    import subprocess
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode != 0:
            return True, ""  # уже пусто
        lines = [
            ln for ln in result.stdout.splitlines()
            if "cron_check.py" not in ln and "SNI_MONITOR" not in ln
        ]
        new_crontab = "\n".join(lines) + "\n"
        proc = subprocess.run(["crontab", "-"], input=new_crontab,
                               capture_output=True, text=True)
        return proc.returncode == 0, proc.stderr[:300]
    except Exception as e:
        return False, str(e)


def kb_schedule(has_cron: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_cron:
        rows += [
            [InlineKeyboardButton(text="▶️ Запустить сейчас",  callback_data="cron:run_now")],
            [InlineKeyboardButton(text="🕐 Изменить время",    callback_data="cron:set_time")],
            [InlineKeyboardButton(text="🚫 Отключить авто",    callback_data="cron:disable")],
        ]
    else:
        rows += [
            [InlineKeyboardButton(text="✅ Включить автопроверку", callback_data="cron:enable")],
        ]
    rows.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class SetCronTime(StatesGroup):
    waiting_time = State()


async def screen_schedule(target) -> None:
    status = _cron_status()
    has_cron = "включена" in status

    text = (
        "⏰ <b>Расписание автопроверки</b>\n\n"
        f"Статус: {status}\n\n"
    )
    if has_cron:
        text += (
            "Каждый день бот автоматически:\n"
            "  • проверяет все сервера\n"
            "  • сохраняет результаты в историю\n"
            "  • отправляет отчёт сюда"
        )
    else:
        text += (
            "Автопроверка <b>выключена</b>.\n"
            "Нажмите ниже, чтобы включить ежедневную проверку."
        )

    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML",
                            reply_markup=kb_schedule(has_cron))
    else:
        await _edit(target, text, reply_markup=kb_schedule(has_cron))


@dp.callback_query(F.data == "menu:schedule")
async def cb_schedule(call: CallbackQuery) -> None:
    await call.answer()
    await screen_schedule(call.message)


@dp.callback_query(F.data == "cron:enable")
async def cb_cron_enable(call: CallbackQuery) -> None:
    await call.answer()
    ok, err = _set_cron(CRON_HOUR, CRON_MINUTE)
    if ok:
        await _edit(
            call.message,
            f"✅ <b>Автопроверка включена</b>\n\n"
            f"⏰ Каждый день в <b>{CRON_HOUR:02d}:{CRON_MINUTE:02d}</b>\n\n"
            "Чтобы изменить время — нажмите «🕐 Изменить время».",
            reply_markup=kb_schedule(True),
        )
    else:
        await _edit(
            call.message,
            f"❌ Не удалось установить крон:\n<code>{err}</code>\n\n"
            "Убедитесь что cron установлен: <code>apt install cron</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="menu:schedule"),
            ]]),
        )


@dp.callback_query(F.data == "cron:disable")
async def cb_cron_disable(call: CallbackQuery) -> None:
    await call.answer()
    ok, err = _remove_cron()
    if ok:
        await _edit(
            call.message,
            "🚫 <b>Автопроверка отключена.</b>\n\n"
            "Проверки продолжат работать вручную через кнопку <b>▶️ Проверить</b>.",
            reply_markup=kb_schedule(False),
        )
    else:
        await _edit(
            call.message,
            f"❌ Ошибка: <code>{err}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="menu:schedule"),
            ]]),
        )


@dp.callback_query(F.data == "cron:set_time")
async def cb_cron_set_time(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(SetCronTime.waiting_time)
    await _edit(
        call.message,
        "🕐 <b>Введите новое время автопроверки</b>\n\n"
        "Формат: <code>ЧЧ:ММ</code>\n"
        "Примеры: <code>09:00</code>, <code>23:30</code>, <code>6:00</code>",
        reply_markup=kb_cancel(),
    )


@dp.message(SetCronTime.waiting_time)
async def fsm_cron_time(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()
    await state.clear()

    # Парсим ЧЧ:ММ
    try:
        parts = text.replace(".", ":").split(":")
        hour   = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, IndexError):
        await msg.answer(
            f"❌ Неверный формат: <code>{text}</code>\n"
            "Введите время в формате <code>ЧЧ:ММ</code>, например <code>09:00</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Попробовать снова",
                                     callback_data="cron:set_time"),
                InlineKeyboardButton(text="◀️ Назад",
                                     callback_data="menu:schedule"),
            ]]),
        )
        return

    ok, err = _set_cron(hour, minute)
    if ok:
        await msg.answer(
            f"✅ <b>Расписание обновлено</b>\n\n"
            f"⏰ Автопроверка каждый день в <b>{hour:02d}:{minute:02d}</b>",
            parse_mode="HTML",
            reply_markup=kb_schedule(True),
        )
    else:
        await msg.answer(
            f"❌ Ошибка установки крона: <code>{err}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="menu:schedule"),
            ]]),
        )


@dp.callback_query(F.data == "cron:run_now")
async def cb_cron_run_now(call: CallbackQuery) -> None:
    await call.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        servers = await get_servers(db)

    if not servers:
        await _edit(call.message,
                    "📭 Нет серверов. Добавьте через главное меню.",
                    reply_markup=kb_schedule(True))
        return

    await _edit(
        call.message,
        f"🚀 <b>Запускаю плановую проверку ({len(servers)} серверов)…</b>\n"
        "Отчёты придут отдельными сообщениями.",
        reply_markup=kb_back_main(),
    )

    # Запускаем как check_all
    collected: list[tuple[str, ScanStats]] = []
    lock = asyncio.Lock()

    async def _one(server: dict) -> None:
        placeholder = await call.message.answer(
            f"⠋ <b>{server['name']}</b> — ожидание…", parse_mode="HTML"
        )
        result = await _run_check(placeholder, server["id"])
        if result is not None:
            async with lock:
                collected.append(result)

    await asyncio.gather(*[_one(s) for s in servers])

    if len(collected) > 1:
        await call.message.answer(
            _truncate(format_summary_report(collected)),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⏰ Расписание",  callback_data="menu:schedule"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main"),
            ]]),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Запуск
# ══════════════════════════════════════════════════════════════════════════════

async def on_startup() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await init_db(db)
    log.info("БД инициализирована. Бот запущен.")


async def main() -> None:
    dp.startup.register(on_startup)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
