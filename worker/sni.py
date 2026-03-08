#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SNI Watcher — воркер.
Проверяет SNI-адреса через указанный сервер, сохраняет results.jsonl,
опционально отправляет отчёт и файл в Telegram-бот.

Аргументы:
  --server-ip   IP целевого сервера (обязательно)
  --server-id   Метка сервера в отчёте (например: Finland-1)
  --sni-path    Путь к .txt файлу или папке с .txt (по умолчанию: sni.txt)
  --out-dir     Куда писать results.jsonl (по умолчанию: scan_out)
  --tg-token    Токен бота для отправки отчёта (или TG_BOT_TOKEN)
  --tg-chat-id  chat_id получателя (или TG_CHAT_ID)
  --concurrency Параллельность (по умолчанию: 50)
  --strict      Требовать видимый HTTP-ответ
  --no-color    Отключить цветной вывод
  --no-fsync    Не делать fsync (быстрее, менее надёжно)
"""

import argparse
import asyncio
import json
import os
import signal
import ssl
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Tuple

# ── Цвета (опционально) ───────────────────────────────────────────────────────
try:
    from colorama import init as _cinit, Fore, Style
    _cinit()
    USE_COLOR = True
except Exception:
    class Fore:   # type: ignore
        GREEN = RED = YELLOW = ""
    class Style:  # type: ignore
        RESET_ALL = ""
    USE_COLOR = False

# ── Прогресс-бар (опционально) ────────────────────────────────────────────────
try:
    from tqdm import tqdm as _tqdm
    class tqdm(_tqdm):  # type: ignore
        pass
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    class tqdm:  # type: ignore  # noqa: F811
        def __init__(self, *a, total=0, **kw):
            self._total = total
            self._n = 0
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def update(self, n=1): self._n += n
        def set_postfix(self, **kw): pass
        @staticmethod
        def write(s: str): print(s)


# ══════════════════════════════════════════════════════════════════════════════
#  Конфигурация по умолчанию
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CFG = {
    "port":        443,
    "health_path": "/",
    "timeout":     5.0,
    "concurrency": 50,
    "strict_http": False,
}


# ══════════════════════════════════════════════════════════════════════════════
#  Dataclass результата
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ProbeResult:
    sni:       str
    status:    str            # WORKING | BLOCKED | INCONCLUSIVE
    detail:    str
    rtt_ms:    Optional[int]
    ts:        float
    server_id: str = ""


# ══════════════════════════════════════════════════════════════════════════════
#  Одиночная проверка SNI
# ══════════════════════════════════════════════════════════════════════════════

async def probe_sni(sni: str, cfg: dict, server_id: str = "") -> ProbeResult:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE

    t0 = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host=cfg["server_ip"], port=cfg["port"],
                ssl=ctx, server_hostname=sni,
            ),
            timeout=cfg["timeout"],
        )

        req = (
            f"HEAD {cfg['health_path']} HTTP/1.1\r\n"
            f"Host: {sni}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii", "ignore")
        writer.write(req)
        await writer.drain()

        try:
            data = await asyncio.wait_for(
                reader.read(256), timeout=cfg["timeout"]
            )
        except asyncio.TimeoutError:
            data = b""

        rtt = int((time.perf_counter() - t0) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        if cfg["strict_http"]:
            if data.startswith(b"HTTP/1."):
                status, detail = "WORKING",      f"HTTP OK, {len(data)} bytes"
            elif not data:
                status, detail = "INCONCLUSIVE", "TLS OK, no HTTP bytes"
            else:
                status, detail = "INCONCLUSIVE", f"Non-HTTP: {data[:20]!r}"
        else:
            status, detail = "WORKING", "TLS OK"

        return ProbeResult(sni, status, detail, rtt, time.time(), server_id)

    except asyncio.TimeoutError:
        return ProbeResult(sni, "BLOCKED",      "Timeout",                     None, time.time(), server_id)
    except ssl.SSLError as e:
        return ProbeResult(sni, "BLOCKED",      f"SSL: {e.__class__.__name__}", None, time.time(), server_id)
    except ConnectionResetError:
        return ProbeResult(sni, "BLOCKED",      "TCP reset",                   None, time.time(), server_id)
    except OSError as e:
        return ProbeResult(sni, "BLOCKED",      f"OSError: {e}",               None, time.time(), server_id)
    except Exception as e:
        tb = traceback.format_exc(limit=1).strip()
        return ProbeResult(sni, "INCONCLUSIVE", f"{e.__class__.__name__}: {e} | {tb}", None, time.time(), server_id)


# ══════════════════════════════════════════════════════════════════════════════
#  Загрузка списка SNI
# ══════════════════════════════════════════════════════════════════════════════

def load_sni_list(path: Path) -> List[str]:
    items: List[str] = []

    if path.is_dir():
        for f in sorted(p for p in path.iterdir()
                        if p.suffix.lower() == ".txt" and p.is_file()):
            items.extend(_read_txt(f))
    elif path.is_file():
        items = _read_txt(path)
    else:
        print(f"[WARN] SNI-файл/папка не найден: {path}", file=sys.stderr)
        return []

    # Дедупликация с сохранением порядка
    seen: set = set()
    result = []
    for s in items:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def _read_txt(path: Path) -> List[str]:
    try:
        return [
            ln.strip()
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
    except Exception as e:
        print(f"[WARN] Не удалось прочитать {path}: {e}", file=sys.stderr)
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  Telegram-отправка (без сторонних зависимостей)
# ══════════════════════════════════════════════════════════════════════════════

def _tg_request(token: str, method: str, data: bytes, content_type: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print(f"[TG] {method} ошибка: {e}", file=sys.stderr)
        return False


def tg_send_message(token: str, chat_id: str, text: str) -> bool:
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        ensure_ascii=False,
    ).encode()
    return _tg_request(token, "sendMessage", payload, "application/json")


def tg_send_file(token: str, chat_id: str, path: Path, caption: str = "") -> bool:
    boundary = "SNIBoundary77x"
    CRLF = b"\r\n"

    def field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    file_bytes = path.read_bytes()
    file_part  = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + file_bytes + CRLF

    body = (
        field("chat_id", chat_id)
        + (field("caption", caption) if caption else b"")
        + (field("parse_mode", "HTML") if caption else b"")
        + file_part
        + f"--{boundary}--\r\n".encode()
    )
    return _tg_request(
        token, "sendDocument", body,
        f"multipart/form-data; boundary={boundary}",
    )


def _build_tg_text(server_id: str, ok: int, errors: int,
                   problem_snis: List[str], elapsed: float) -> str:
    lines = [
        f"📊 <b>Отчёт · {server_id or 'unknown'}</b>",
        "",
        f"✅ Успешно:  <b>{ok}</b>",
        f"❌ Ошибки:   <b>{errors}</b>",
        f"⏱ Время:    {elapsed:.0f} с",
    ]
    if problem_snis:
        sample = problem_snis[:10]
        lines += [
            "",
            f"🚫 <b>Проблемные SNI ({len(problem_snis)}):</b>",
            "\n".join(f"  • <code>{s}</code>" for s in sample),
        ]
        if len(problem_snis) > 10:
            lines.append(f"  <i>…и ещё {len(problem_snis) - 10}</i>")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  Основной скан
# ══════════════════════════════════════════════════════════════════════════════

async def run_scan(
    domains:      List[str],
    cfg:          dict,
    out_dir:      Path,
    fsync_on:     bool,
    server_id:    str,
    tg_token:     str,
    tg_chat_id:   str,
) -> None:

    out_dir.mkdir(parents=True, exist_ok=True)
    results_file = out_dir / "results.jsonl"

    # Очищаем файл перед новым запуском
    results_file.write_bytes(b"")

    fh = results_file.open("a", encoding="utf-8", buffering=1)

    def _write(line: str) -> None:
        fh.write(line + "\n")
        fh.flush()
        if fsync_on:
            os.fsync(fh.fileno())

    total = len(domains)
    if total == 0:
        print("[ERR] Список SNI пуст.", file=sys.stderr)
        fh.close()
        return

    ok_cnt = blocked_cnt = inc_cnt = 0
    working_list: List[ProbeResult] = []
    problem_snis: List[str]         = []
    t_start = time.perf_counter()
    stop    = False

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: setattr(sys.modules[__name__], "_stop", True))
        loop.add_signal_handler(signal.SIGINT,  lambda: setattr(sys.modules[__name__], "_stop", True))
    except (NotImplementedError, AttributeError):
        pass

    sem  = asyncio.Semaphore(cfg["concurrency"])
    lock = asyncio.Lock()

    async def _process(sni: str, pbar: tqdm) -> None:
        nonlocal ok_cnt, blocked_cnt, inc_cnt, stop
        async with sem:
            if stop:
                return
            res  = await probe_sni(sni, cfg, server_id)
            line = json.dumps(asdict(res), ensure_ascii=False)

            async with lock:
                _write(line)

            rtt_str = f"{res.rtt_ms} мс" if res.rtt_ms is not None else ""
            color   = (
                Fore.GREEN  if res.status == "WORKING"      else
                Fore.RED    if res.status == "BLOCKED"      else
                Fore.YELLOW
            ) if USE_COLOR else ""
            reset = Style.RESET_ALL if USE_COLOR else ""
            tqdm.write(
                f"[{color}{res.status:12}{reset}] {sni:45} {rtt_str:>8}  {res.detail}"
            )

            if res.status == "WORKING":
                ok_cnt += 1
                working_list.append(res)
            elif res.status == "BLOCKED":
                blocked_cnt += 1
                problem_snis.append(sni)
            else:
                inc_cnt += 1
                problem_snis.append(sni)

            pbar.set_postfix(ok=ok_cnt, blocked=blocked_cnt, inc=inc_cnt, refresh=False)
            pbar.update(1)

    try:
        with tqdm(total=total, unit="sni", dynamic_ncols=True, leave=True) as pbar:
            tasks = [asyncio.create_task(_process(s, pbar)) for s in domains]
            await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        stop = True
        tqdm.write("\n[!] Прервано — записанные данные сохранены.")
    finally:
        fh.close()

    elapsed = time.perf_counter() - t_start

    # ── Топ-30 по RTT ─────────────────────────────────────────────────────────
    print("\n" + "=" * 44)
    print("  Т О П   Р А Б О Ч И Х   S N I  (по RTT)")
    print("=" * 44)
    if not working_list:
        print("  Рабочих SNI не найдено.")
    else:
        working_list.sort(key=lambda r: r.rtt_ms if r.rtt_ms is not None else 999_999)
        for i, r in enumerate(working_list[:30], 1):
            rtt = f"{r.rtt_ms} мс" if r.rtt_ms is not None else "N/A"
            name = f"{Fore.GREEN}{r.sni}{Style.RESET_ALL}" if USE_COLOR else r.sni
            print(f"  {i:2d}. {name:48} {rtt:>8}")

    print(f"\n{'='*44}")
    print(f"  WORKING: {ok_cnt}  |  BLOCKED: {blocked_cnt}  |  INCONCLUSIVE: {inc_cnt}")
    print(f"  Всего:   {ok_cnt+blocked_cnt+inc_cnt}/{total}  |  Время: {elapsed:.1f} с")
    print(f"  Файл:    {results_file}")
    print("=" * 44)

    # ── Отправка в Telegram ───────────────────────────────────────────────────
    if tg_token and tg_chat_id:
        print("\n[TG] Отправляю отчёт…")
        text = _build_tg_text(server_id, ok_cnt, blocked_cnt + inc_cnt,
                               problem_snis, elapsed)
        tg_send_message(tg_token, tg_chat_id, text)

        if results_file.stat().st_size > 0:
            tg_send_file(tg_token, tg_chat_id, results_file,
                         f"results.jsonl · {server_id}")
            print("[TG] Файл отправлен.")
        else:
            print("[TG] Файл пустой, пропускаю.")


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(
        description="SNI Watcher — проверяет SNI через удалённый сервер"
    )
    ap.add_argument("--server-ip",   required=True,
                    help="IP целевого сервера")
    ap.add_argument("--server-id",   default="",
                    help="Метка сервера в отчёте (например: Finland-1)")
    ap.add_argument("--sni-path",    default="sni.txt",
                    help="Путь к .txt или папке с .txt (по умолчанию: sni.txt)")
    ap.add_argument("--out-dir",     default="scan_out",
                    help="Куда писать results.jsonl (по умолчанию: scan_out)")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="Параллельность (по умолчанию: 50)")
    ap.add_argument("--strict",      action="store_true",
                    help="Требовать видимый HTTP-ответ")
    ap.add_argument("--no-color",    action="store_true",
                    help="Отключить цветной вывод")
    ap.add_argument("--no-fsync",    action="store_true",
                    help="Отключить fsync (быстрее, менее надёжно)")
    ap.add_argument("--tg-token",    default=os.environ.get("TG_BOT_TOKEN", ""),
                    help="Токен Telegram-бота (или TG_BOT_TOKEN)")
    ap.add_argument("--tg-chat-id",  default=os.environ.get("TG_CHAT_ID", ""),
                    help="chat_id получателя (или TG_CHAT_ID)")
    args = ap.parse_args()

    global USE_COLOR
    if args.no_color:
        USE_COLOR = False

    cfg = dict(DEFAULT_CFG)
    cfg["server_ip"]  = args.server_ip
    cfg["strict_http"]= args.strict
    if args.concurrency:
        cfg["concurrency"] = max(1, args.concurrency)

    sni_path = Path(args.sni_path)
    domains  = load_sni_list(sni_path)
    if not domains:
        print(f"[ERR] Пустой список SNI. Проверь --sni-path: {sni_path}", file=sys.stderr)
        sys.exit(1)

    print(f"SNI: {len(domains)}  |  target: {cfg['server_ip']}:{cfg['port']}"
          f"  |  strict: {cfg['strict_http']}  |  concurrency: {cfg['concurrency']}")
    if args.server_id:
        print(f"Server-ID: {args.server_id}")
    print(f"Output: {args.out_dir}/results.jsonl\n")

    try:
        asyncio.run(run_scan(
            domains    = domains,
            cfg        = cfg,
            out_dir    = Path(args.out_dir),
            fsync_on   = not args.no_fsync,
            server_id  = args.server_id,
            tg_token   = args.tg_token,
            tg_chat_id = args.tg_chat_id,
        ))
    except KeyboardInterrupt:
        print("\nПрервано. Данные записаны в results.jsonl.")


if __name__ == "__main__":
    main()
