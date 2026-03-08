"""
SSH-взаимодействие с воркерами (paramiko).

  deploy_worker — первичная установка: зависимости + файлы
  run_check     — запуск sni.py и загрузка results.jsonl
"""

import io
import logging
from pathlib import Path
from typing import Optional

from config import SSH_USER, SSH_PORT, REMOTE_DIR, CONCURRENCY

log = logging.getLogger(__name__)

# sni.py лежит в папке worker/ рядом с bot/
_LOCAL_SNI_PY = Path(__file__).parent.parent / "worker" / "sni.py"

_DEPS_CMD = (
    "export DEBIAN_FRONTEND=noninteractive && "
    "apt-get update -qq && "
    "apt-get install -y -qq python3 python3-pip && "
    "pip3 install tqdm colorama --quiet --break-system-packages 2>/dev/null || "
    "pip3 install tqdm colorama --quiet"
)


# ══════════════════════════════════════════════════════════════════════════════
#  Внутренние утилиты
# ══════════════════════════════════════════════════════════════════════════════

def _cred_kwargs(ip: str, cred: str) -> dict:
    """Разбирает строку с SSH-данными в kwargs для paramiko."""
    import io
    import paramiko
    base: dict = {
        "hostname": ip,
        "username": SSH_USER,
        "port":     SSH_PORT,
        "timeout":  30,
    }
    if cred.startswith("password:"):
        base["password"] = cred[len("password:"):]
    elif cred.startswith("pkey:"):
        # Приватный ключ вставлен напрямую (PEM-текст после префикса pkey:)
        pem = cred[len("pkey:"):].strip().replace("\\n", "\n")
        buf = io.StringIO(pem)
        try:
            base["pkey"] = paramiko.RSAKey.from_private_key(buf)
        except paramiko.ssh_exception.SSHException:
            try:
                buf.seek(0)
                base["pkey"] = paramiko.Ed25519Key.from_private_key(buf)
            except Exception:
                buf.seek(0)
                base["pkey"] = paramiko.ECDSAKey.from_private_key(buf)
    else:
        base["key_filename"] = cred
    return base


def _get_client(ip: str, cred: str):
    """Создаёт и возвращает подключённый SSHClient."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(**_cred_kwargs(ip, cred))
    return client


def _exec(client, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
    """Выполняет команду, возвращает (exit_code, stdout, stderr)."""
    _, out, err = client.exec_command(cmd, timeout=timeout)
    exit_code   = out.channel.recv_exit_status()
    return (
        exit_code,
        out.read().decode("utf-8", errors="replace"),
        err.read().decode("utf-8", errors="replace"),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Публичное API
# ══════════════════════════════════════════════════════════════════════════════

def deploy_worker(ip: str, cred: str, sni_list_path: Path) -> tuple[bool, str]:
    """
    Разворачивает воркер на удалённом сервере:
      1. Создаёт REMOTE_DIR
      2. Устанавливает python3 + pip + tqdm + colorama
      3. Копирует sni.py и список SNI
    Возвращает (success, error_message).
    """
    if not _LOCAL_SNI_PY.exists():
        return False, f"sni.py не найден локально: {_LOCAL_SNI_PY}"

    client = None
    try:
        client = _get_client(ip, cred)

        # Создаём директорию
        code, _, err = _exec(client, f"mkdir -p {REMOTE_DIR}")
        if code != 0:
            return False, f"mkdir завершился с кодом {code}: {err}"

        # Устанавливаем зависимости
        log.info("[%s] Устанавливаю зависимости…", ip)
        code, _, err = _exec(client, _DEPS_CMD, timeout=300)
        if code != 0:
            return False, f"Установка зависимостей (код {code}): {err[:600]}"

        # Копируем файлы через SFTP
        sftp = client.open_sftp()
        try:
            sftp.put(str(_LOCAL_SNI_PY), f"{REMOTE_DIR}/sni.py")
            log.info("[%s] sni.py скопирован.", ip)

            if sni_list_path.exists():
                sftp.put(str(sni_list_path), f"{REMOTE_DIR}/sni.txt")
                log.info("[%s] sni.txt скопирован.", ip)
            else:
                log.warning("[%s] SNI-список не найден: %s", ip, sni_list_path)
        finally:
            sftp.close()

        return True, ""

    except Exception as e:
        log.exception("[%s] Ошибка деплоя", ip)
        return False, str(e)
    finally:
        if client:
            client.close()


def run_check(ip: str, cred: str,
              server_name: str,
              server_id: int) -> tuple[bool, str, Optional[bytes]]:
    """
    Запускает sni.py на удалённом сервере, скачивает results.jsonl.
    Возвращает (success, log_output, jsonl_bytes).
    jsonl_bytes=None при ошибке, b"" если файл пустой.
    """
    client = None
    try:
        client = _get_client(ip, cred)

        # Очищаем предыдущие результаты
        _exec(client, f"rm -f {REMOTE_DIR}/scan_out/results.jsonl")

        cmd = (
            f"cd {REMOTE_DIR} && "
            f"python3 sni.py "
            f"  --server-ip {ip} "
            f"  --server-id '{server_name}' "
            f"  --sni-path sni.txt "
            f"  --out-dir scan_out "
            f"  --no-color "
            f"  --no-fsync "
            f"  --concurrency {CONCURRENCY} "
            f"2>&1"
        )
        log.info("[%s] Запускаю проверку…", ip)
        code, output, _ = _exec(client, cmd, timeout=900)  # 15 минут максимум
        log.info("[%s] Завершено (код %d). Последние строки:\n%s",
                 ip, code, output[-400:])

        if code != 0:
            return False, output, None

        # Скачиваем results.jsonl
        sftp = client.open_sftp()
        buf  = io.BytesIO()
        try:
            sftp.getfo(f"{REMOTE_DIR}/scan_out/results.jsonl", buf)
            jsonl_bytes = buf.getvalue()
        except (FileNotFoundError, IOError):
            log.warning("[%s] results.jsonl не найден после выполнения", ip)
            jsonl_bytes = b""
        finally:
            sftp.close()

        return True, output, jsonl_bytes

    except Exception as e:
        log.exception("[%s] Ошибка при запуске проверки", ip)
        return False, str(e), None
    finally:
        if client:
            client.close()


# ══════════════════════════════════════════════════════════════════════════════
#  Локальный режим (без SSH)
# ══════════════════════════════════════════════════════════════════════════════

def _is_local(cred: str) -> bool:
    return cred.strip() == "local"


def deploy_worker_local(sni_list_path: Path) -> tuple[bool, str]:
    """Устанавливает зависимости и копирует файлы локально."""
    import subprocess, shutil, os

    local_dir = Path(REMOTE_DIR)
    local_dir.mkdir(parents=True, exist_ok=True)

    # Копируем sni.py
    if not _LOCAL_SNI_PY.exists():
        return False, f"sni.py не найден: {_LOCAL_SNI_PY}"
    shutil.copy2(str(_LOCAL_SNI_PY), str(local_dir / "sni.py"))

    # Копируем sni.txt
    if sni_list_path.exists():
        shutil.copy2(str(sni_list_path), str(local_dir / "sni.txt"))

    # Устанавливаем зависимости
    pip_cmd = ["pip3", "install", "tqdm", "colorama", "--quiet", "--break-system-packages"]
    r = subprocess.run(pip_cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        # fallback без --break-system-packages
        r = subprocess.run(pip_cmd[:-1], capture_output=True, text=True, timeout=120)

    log.info("[local] Деплой завершён в %s", local_dir)
    return True, ""


def run_check_local(server_name: str, server_id: int) -> tuple[bool, str, Optional[bytes]]:
    """Запускает sni.py локально, возвращает results.jsonl."""
    import subprocess, shutil, socket

    local_dir = Path(REMOTE_DIR)
    out_dir   = local_dir / "scan_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"

    # Очищаем предыдущий результат
    if results_path.exists():
        results_path.unlink()

    local_ip = _get_local_ip()

    cmd = [
        "python3", str(local_dir / "sni.py"),
        "--server-ip", local_ip,
        "--server-id", server_name,
        "--sni-path",  str(local_dir / "sni.txt"),
        "--out-dir",   str(out_dir),
        "--no-color",
        "--no-fsync",
        "--concurrency", str(CONCURRENCY),
    ]
    log.info("[local] Запускаю: %s", " ".join(cmd))

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=900, cwd=str(local_dir),
        )
        output = r.stdout + r.stderr
        log.info("[local] Завершено (код %d). Последние строки:\n%s",
                 r.returncode, output[-400:])

        if r.returncode != 0:
            return False, output, None

        if results_path.exists():
            jsonl_bytes = results_path.read_bytes()
        else:
            log.warning("[local] results.jsonl не найден")
            jsonl_bytes = b""

        return True, output, jsonl_bytes

    except subprocess.TimeoutExpired:
        return False, "Таймаут 15 минут превышен", None
    except Exception as e:
        log.exception("[local] Ошибка запуска")
        return False, str(e), None


def _get_local_ip() -> str:
    """Определяет внешний IP этого сервера."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
