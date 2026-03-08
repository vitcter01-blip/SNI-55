"""
Парсинг results.jsonl и форматирование отчётов для Telegram.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
#  Dataclass
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScanStats:
    total:        int   = 0
    working:      int   = 0
    blocked:      int   = 0
    inconclusive: int   = 0
    elapsed_sec:  float = 0.0

    min_rtt: Optional[int]   = None
    max_rtt: Optional[int]   = None
    avg_rtt: Optional[float] = None

    # [(sni, rtt_ms), ...] — топ-5 самых быстрых
    top_working:    list = field(default_factory=list)
    # Все проблемные SNI (плоский список для сохранения в БД)
    blocked_snis:   list = field(default_factory=list)
    # По типу ошибки: {"Заблокировано": [...], "Таймаут": [...], ...}
    blocked_detail: dict = field(default_factory=dict)

    @property
    def errors(self) -> int:
        return self.blocked + self.inconclusive

    @property
    def success_pct(self) -> float:
        return round(self.working / self.total * 100, 1) if self.total else 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  Парсер JSONL
# ══════════════════════════════════════════════════════════════════════════════

def parse_results_jsonl(jsonl_bytes: bytes) -> ScanStats:
    """Читает bytes с JSONL, возвращает заполненный ScanStats."""
    stats = ScanStats()
    if not jsonl_bytes:
        return stats

    rtts:        list[int]         = []
    working_rtt: list[tuple]       = []   # (sni, rtt_ms)
    ts_min = ts_max = None

    for raw in jsonl_bytes.decode("utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # Пропускаем записи без обязательных полей
        sni    = rec.get("sni",    "")
        status = rec.get("status", "")
        if not sni or status not in ("WORKING", "BLOCKED", "INCONCLUSIVE"):
            continue

        stats.total += 1
        ts     = rec.get("ts")  or 0.0
        rtt    = rec.get("rtt_ms")
        detail = rec.get("detail", "")

        # Временные метки (для elapsed_sec)
        if ts:
            if ts_min is None or ts < ts_min: ts_min = ts
            if ts_max is None or ts > ts_max: ts_max = ts

        if status == "WORKING":
            stats.working += 1
            if isinstance(rtt, (int, float)):
                rtt_int = int(rtt)
                rtts.append(rtt_int)
                working_rtt.append((sni, rtt_int))

        elif status == "BLOCKED":
            stats.blocked += 1
            stats.blocked_snis.append(sni)
            stats.blocked_detail.setdefault("Заблокировано", []).append(sni)

        else:  # INCONCLUSIVE
            stats.inconclusive += 1
            stats.blocked_snis.append(sni)
            if "Timeout" in detail or "timeout" in detail:
                key = "Таймаут"
            elif "SSL" in detail:
                key = "SSL ошибка"
            elif "reset" in detail.lower():
                key = "TCP reset"
            elif "OSError" in detail:
                key = "Сетевая ошибка"
            else:
                key = "Другое"
            stats.blocked_detail.setdefault(key, []).append(sni)

    if ts_min and ts_max and ts_max > ts_min:
        stats.elapsed_sec = ts_max - ts_min

    if rtts:
        stats.min_rtt = min(rtts)
        stats.max_rtt = max(rtts)
        stats.avg_rtt = sum(rtts) / len(rtts)
        working_rtt.sort(key=lambda x: x[1])
        stats.top_working = working_rtt[:5]

    return stats


# ══════════════════════════════════════════════════════════════════════════════
#  Утилиты
# ══════════════════════════════════════════════════════════════════════════════

def _bar(value: int, total: int, width: int = 8) -> str:
    """Текстовый прогресс-бар: ████░░░░  75%"""
    if total == 0:
        return "░" * width + "   0%"
    filled = round(width * value / total)
    pct    = round(100 * value / total)
    return "█" * filled + "░" * (width - filled) + f" {pct:3d}%"


def _rtt(ms: Optional[float]) -> str:
    if ms is None:
        return "—"
    return f"{int(ms)} мс" if ms < 1000 else f"{ms/1000:.1f} с"


def _dt(iso: str) -> str:
    """ISO → 'ДД.ММ.ГГГГ ЧЧ:ММ'"""
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso


def _dt_short(iso: str) -> str:
    """ISO → 'ДД.ММ ЧЧ:ММ'"""
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
    except Exception:
        return iso


# ══════════════════════════════════════════════════════════════════════════════
#  Форматтеры отчётов
# ══════════════════════════════════════════════════════════════════════════════

def format_report(server_name: str, stats: ScanStats,
                  checked_at: Optional[str] = None) -> str:
    """Полный HTML-отчёт по одному прогону."""
    ts = _dt(checked_at) if checked_at else datetime.now().strftime("%d.%m.%Y %H:%M")

    lines = [
        f"📊 <b>Отчёт · {server_name}</b>",
        f"<i>🕐 {ts}</i>",
        "",
        "┌─ <b>Результаты</b>",
        f"│  Всего:        <b>{stats.total}</b>",
        f"│  ✅ Работают:  <b>{stats.working}</b>  {_bar(stats.working, stats.total)}",
        f"│  🔴 Заблок.:   <b>{stats.blocked}</b>  {_bar(stats.blocked, stats.total)}",
        f"│  ⚠️ Неопред.:  <b>{stats.inconclusive}</b>  {_bar(stats.inconclusive, stats.total)}",
        f"└─ ⏱ Время:     <b>{stats.elapsed_sec:.1f} с</b>",
    ]

    if stats.min_rtt is not None:
        lines += [
            "",
            "┌─ <b>Задержка (RTT)</b>",
            f"│  Min: <b>{_rtt(stats.min_rtt)}</b>",
            f"│  Avg: <b>{_rtt(stats.avg_rtt)}</b>",
            f"└─ Max: <b>{_rtt(stats.max_rtt)}</b>",
        ]

    if stats.top_working:
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        lines += ["", "🏆 <b>Топ-5 быстрых SNI:</b>"]
        for i, (sni, rtt) in enumerate(stats.top_working):
            lines.append(f"  {medals[i]} <code>{sni}</code> — {_rtt(rtt)}")

    if stats.errors > 0:
        icons = {
            "Заблокировано": "🔴",
            "Таймаут":       "⏳",
            "SSL ошибка":    "🔒",
            "TCP reset":     "⚡️",
            "Сетевая ошибка":"🌐",
            "Другое":        "❓",
        }
        lines += ["", f"🚫 <b>Проблемные SNI ({stats.errors}):</b>"]
        for err_type, sni_list in stats.blocked_detail.items():
            icon = icons.get(err_type, "❓")
            lines.append(f"\n  {icon} <b>{err_type}</b> — {len(sni_list)} шт.:")
            for sni in sni_list[:6]:
                lines.append(f"    • <code>{sni}</code>")
            if len(sni_list) > 6:
                lines.append(f"    <i>…и ещё {len(sni_list) - 6}</i>")

    return "\n".join(lines)


def format_history_list(server_name: str, results: list[dict]) -> str:
    """Список последних проверок — для экрана истории."""
    if not results:
        return (
            f"📂 <b>История · {server_name}</b>\n\n"
            "Проверок ещё не было.\n"
            "Нажмите <b>▶️ Проверить</b> для первого запуска."
        )

    lines = [f"📂 <b>История проверок · {server_name}</b>\n"]
    for i, r in enumerate(results, 1):
        errors = r["blocked"] + r["inconclusive"]
        icon   = "✅" if errors == 0 else ("⚠️" if errors < 5 else "❌")
        pct    = round(r["working"] / r["total"] * 100) if r["total"] else 0
        rtt_s  = f"avg {_rtt(r['avg_rtt'])}" if r.get("avg_rtt") else ""
        ts_s   = _dt(r["checked_at"])

        lines.append(
            f"{i}. {icon} <b>{ts_s}</b>\n"
            f"   ✅ {r['working']}/{r['total']} ({pct}%)"
            f"  ❌ {errors}"
            f"  ⏱ {r['elapsed_sec']:.0f}с"
            + (f"  📶 {rtt_s}" if rtt_s else "")
        )

    return "\n".join(lines)


def format_history_detail(server_name: str, result: dict) -> str:
    """Полный отчёт для одной исторической записи."""
    # Восстанавливаем ScanStats из строки БД
    stats = ScanStats(
        total        = result["total"],
        working      = result["working"],
        blocked      = result["blocked"],
        inconclusive = result["inconclusive"],
        elapsed_sec  = float(result.get("elapsed_sec") or 0),
        min_rtt      = result.get("min_rtt"),
        max_rtt      = result.get("max_rtt"),
        avg_rtt      = result.get("avg_rtt"),
        blocked_snis = result.get("blocked_snis") or [],
    )
    # В истории тип ошибки не сохраняется — группируем все в одну категорию
    if stats.blocked_snis:
        stats.blocked_detail = {"Проблемные": stats.blocked_snis}

    return format_report(server_name, stats,
                         checked_at=result.get("checked_at"))


def format_summary_report(results: list[tuple]) -> str:
    """Сводный отчёт по всем серверам (после check_all)."""
    now           = datetime.now().strftime("%d.%m.%Y %H:%M")
    total_sni     = sum(s.total   for _, s in results)
    total_working = sum(s.working for _, s in results)
    total_errors  = sum(s.errors  for _, s in results)

    lines = [
        "📋 <b>Сводный отчёт</b>",
        f"<i>🕐 {now}  ·  серверов: {len(results)}</i>",
        "",
        f"🔢 SNI всего: <b>{total_sni}</b>",
        f"✅ Работают: <b>{total_working}</b>  {_bar(total_working, total_sni)}",
        f"❌ Проблем:  <b>{total_errors}</b>  {_bar(total_errors,  total_sni)}",
        "",
        "─" * 30,
    ]

    for name, s in results:
        icon   = "✅" if s.errors == 0 else ("⚠️" if s.errors < 10 else "❌")
        rtt_s  = f"avg {_rtt(s.avg_rtt)}" if s.avg_rtt else "—"
        lines += [
            f"\n{icon} <b>{name}</b>",
            f"   ✅ {s.working}  ❌ {s.errors}"
            f"  ⏱ {s.elapsed_sec:.0f}с  📶 {rtt_s}",
        ]
        if s.blocked_snis:
            sample  = s.blocked_snis[:3]
            more    = len(s.blocked_snis) - len(sample)
            sni_str = ", ".join(f"<code>{x}</code>" for x in sample)
            if more:
                sni_str += f" <i>+{more}</i>"
            lines.append(f"   🚫 {sni_str}")

    return "\n".join(lines)
