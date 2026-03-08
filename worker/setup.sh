#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SNI Worker — установщик «под ключ» для удалённого сервера
# Использование: bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REMOTE_DIR="${REMOTE_DIR:-/opt/sni_worker}"

echo "════════════════════════════════════════"
echo "  SNI Worker Setup"
echo "════════════════════════════════════════"

echo "[1/3] Устанавливаю системные зависимости…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip

echo "[2/3] Создаю рабочую директорию: ${REMOTE_DIR}"
mkdir -p "${REMOTE_DIR}"

echo "[3/3] Устанавливаю Python-пакеты…"
pip3 install tqdm colorama --quiet --break-system-packages 2>/dev/null \
  || pip3 install tqdm colorama --quiet

echo ""
echo "════════════════════════════════════════"
echo "  ✅ Установка завершена!"
echo "  Директория: ${REMOTE_DIR}"
echo ""
echo "  Тест запуска (после копирования файлов):"
echo "  cd ${REMOTE_DIR} && python3 sni.py --server-ip <IP> --sni-path sni.txt --server-id test"
echo "════════════════════════════════════════"
