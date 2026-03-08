#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  SNI Monitoring Panel — установка в один клик
#  Поддерживаемые ОС: Ubuntu 20.04+, Debian 11+
#
#  Использование:
#    sudo bash install.sh
#
#  Переменные окружения (опционально, для неинтерактивной установки):
#    TG_BOT_TOKEN=...  TG_ADMIN_IDS=...  CRON_HOUR=9  bash install.sh
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Цвета ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
ok()  { echo -e "${GREEN}[✓]${RESET} $*"; }
warn(){ echo -e "${YELLOW}[!]${RESET} $*"; }
die() { echo -e "${RED}[✗]${RESET} $*" >&2; exit 1; }
hdr() { echo -e "\n${CYAN}${BOLD}── $* ──${RESET}"; }

# ── Пути ──────────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/sni_monitor"
BOT_DIR="${INSTALL_DIR}/bot"
WORKER_DIR="${INSTALL_DIR}/worker"
VENV="${INSTALL_DIR}/venv"
DATA_DIR="${BOT_DIR}/data"
LOG_DIR="/var/log/sni_monitor"
SVC="sni-monitor"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Баннер ────────────────────────────────────────────────────────────────────
echo -e "${CYAN}${BOLD}"
cat << 'BANNER'
  ╔══════════════════════════════════════╗
  ║   SNI Monitoring Panel              ║
  ║   Установка в один клик             ║
  ╚══════════════════════════════════════╝
BANNER
echo -e "${RESET}"

# ── Root-проверка ─────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Запустите от root: sudo bash install.sh"

# ── Шаг 1: Системные пакеты ───────────────────────────────────────────────────
hdr "Шаг 1 / 5: Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv python3-dev \
    build-essential libssl-dev libffi-dev cron 2>/dev/null
ok "Пакеты установлены"

# Проверяем Python >= 3.10
PY_VER=$(python3 -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')")
PY_OK=$(python3 -c "import sys; v=sys.version_info; print(int(v.major>3 or (v.major==3 and v.minor>=10)))")
[[ "$PY_OK" == "1" ]] || die "Нужен Python 3.10+, установлен $PY_VER"
ok "Python $PY_VER"

# ── Шаг 2: Копирование файлов ─────────────────────────────────────────────────
hdr "Шаг 2 / 5: Копирование файлов"
mkdir -p "${BOT_DIR}" "${WORKER_DIR}" "${DATA_DIR}" "${LOG_DIR}"

for f in bot.py config.py db.py report.py ssh_worker.py cron_check.py requirements.txt; do
    [[ -f "${SCRIPT_DIR}/bot/${f}" ]] || die "Файл не найден: bot/${f}"
    cp "${SCRIPT_DIR}/bot/${f}" "${BOT_DIR}/${f}"
done
ok "Файлы бота скопированы → ${BOT_DIR}"

for f in sni.py requirements.txt setup.sh; do
    [[ -f "${SCRIPT_DIR}/worker/${f}" ]] && cp "${SCRIPT_DIR}/worker/${f}" "${WORKER_DIR}/${f}"
done
ok "Файлы воркера скопированы → ${WORKER_DIR}"

# SNI-список
for candidate in \
    "${SCRIPT_DIR}/bot/data/sni.txt" \
    "${SCRIPT_DIR}/sni.txt" \
    "$(pwd)/sni.txt" \
    "$(pwd)/sni 2.txt"; do
    if [[ -f "$candidate" ]]; then
        cp "$candidate" "${DATA_DIR}/sni.txt"
        CNT=$(wc -l < "${DATA_DIR}/sni.txt")
        ok "SNI-список скопирован (${CNT} строк) → ${DATA_DIR}/sni.txt"
        break
    fi
done
[[ -f "${DATA_DIR}/sni.txt" ]] || { warn "SNI-список не найден. Положите sni.txt в ${DATA_DIR}/"; touch "${DATA_DIR}/sni.txt"; }

# ── Шаг 3: Python-окружение ───────────────────────────────────────────────────
hdr "Шаг 3 / 5: Python-окружение"
python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --upgrade pip --quiet
"${VENV}/bin/pip" install -r "${BOT_DIR}/requirements.txt" --quiet
ok "Окружение готово: ${VENV}"

# ── Шаг 4: Конфигурация ───────────────────────────────────────────────────────
hdr "Шаг 4 / 5: Конфигурация"
ENV_FILE="${BOT_DIR}/.env"

if [[ -f "${ENV_FILE}" ]]; then
    warn ".env уже есть — пропускаю ввод. Редактировать: nano ${ENV_FILE}"
else
    echo ""

    # TG_BOT_TOKEN
    if [[ -z "${TG_BOT_TOKEN:-}" ]]; then
        while true; do
            read -rp "  TG_BOT_TOKEN (от @BotFather): " TG_BOT_TOKEN
            TG_BOT_TOKEN="${TG_BOT_TOKEN// /}"
            [[ "$TG_BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{35,}$ ]] && break
            warn "Неверный формат. Пример: 1234567890:AAF_xxxx"
        done
    fi

    # TG_ADMIN_IDS
    if [[ -z "${TG_ADMIN_IDS:-}" ]]; then
        while true; do
            read -rp "  TG_ADMIN_IDS (Telegram ID, узнать у @userinfobot): " TG_ADMIN_IDS
            TG_ADMIN_IDS="${TG_ADMIN_IDS// /}"
            [[ "$TG_ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]] && break
            warn "Введите числовой ID. Несколько через запятую: 123456,789012"
        done
    fi

    # Время крона
    echo ""
    echo -e "  ${BOLD}Время ежедневной автопроверки:${RESET}"
    CRON_HOUR="${CRON_HOUR:-}"
    CRON_MINUTE="${CRON_MINUTE:-}"
    if [[ -z "$CRON_HOUR" ]]; then
        read -rp "  Час (0-23, по умолч. 9): " CRON_HOUR
        CRON_HOUR="${CRON_HOUR:-9}"
    fi
    if [[ -z "$CRON_MINUTE" ]]; then
        read -rp "  Минута (0-59, по умолч. 0): " CRON_MINUTE
        CRON_MINUTE="${CRON_MINUTE:-0}"
    fi

    cat > "${ENV_FILE}" << ENV
# SNI Monitoring Panel — конфигурация (создан $(date '+%d.%m.%Y %H:%M'))

TG_BOT_TOKEN=${TG_BOT_TOKEN}
TG_ADMIN_IDS=${TG_ADMIN_IDS}

SSH_USER=root
SSH_PORT=22
REMOTE_DIR=/opt/sni_worker
SNI_CONCURRENCY=50
HISTORY_KEEP=20

CRON_HOUR=${CRON_HOUR}
CRON_MINUTE=${CRON_MINUTE}
ENV
    chmod 600 "${ENV_FILE}"
    ok ".env создан"
fi

# Читаем время крона из .env
CRON_HOUR=$(grep  '^CRON_HOUR='   "${ENV_FILE}" | cut -d= -f2 | tr -d ' ' || echo 9)
CRON_MINUTE=$(grep '^CRON_MINUTE=' "${ENV_FILE}" | cut -d= -f2 | tr -d ' ' || echo 0)
CRON_HOUR="${CRON_HOUR:-9}"
CRON_MINUTE="${CRON_MINUTE:-0}"

# ── Шаг 5: Сервис + крон ──────────────────────────────────────────────────────
hdr "Шаг 5 / 5: Автозапуск и расписание"

# Systemd-сервис
cat > "/etc/systemd/system/${SVC}.service" << SVC
[Unit]
Description=SNI Monitoring Panel Bot
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=root
WorkingDirectory=${BOT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV}/bin/python3 ${BOT_DIR}/bot.py
Restart=on-failure
RestartSec=10
StandardOutput=append:${LOG_DIR}/bot.log
StandardError=append:${LOG_DIR}/bot_error.log

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable "${SVC}" --quiet
systemctl restart "${SVC}"
ok "Сервис запущен: ${SVC}"

# Крон-задача
CRON_CMD="${VENV}/bin/python3 ${BOT_DIR}/cron_check.py >> ${LOG_DIR}/cron.log 2>&1"
CRON_JOB="${CRON_MINUTE} ${CRON_HOUR} * * * ${CRON_CMD}"
MARKER="# SNI_MONITOR_DAILY_CHECK"

CURRENT=$(crontab -l 2>/dev/null || true)
CLEAN=$(echo "$CURRENT" | grep -v "cron_check.py\|SNI_MONITOR" || true)
{ echo "$CLEAN"; echo "$MARKER"; echo "$CRON_JOB"; } | crontab -

# Убеждаемся что cron-демон запущен
systemctl enable cron --quiet 2>/dev/null || systemctl enable crond --quiet 2>/dev/null || true
systemctl start  cron 2>/dev/null         || systemctl start  crond 2>/dev/null || true
ok "Крон установлен: каждый день в $(printf '%02d:%02d' ${CRON_HOUR} ${CRON_MINUTE})"

# ── Команда checker ───────────────────────────────────────────────────────────
cat > /usr/local/bin/checker << 'CHECKER_SCRIPT'
#!/usr/bin/env bash
# checker — управление SNI Monitoring Panel

INSTALL_DIR="/opt/sni_monitor"
BOT_DIR="${INSTALL_DIR}/bot"
VENV="${INSTALL_DIR}/venv"
LOG_DIR="/var/log/sni_monitor"
SVC="sni-monitor"
PY="${VENV}/bin/python3"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

_status_icon() {
    systemctl is-active --quiet "${SVC}" && echo -e "${GREEN}●${RESET}" || echo -e "${RED}●${RESET}"
}

_cron_time() {
    crontab -l 2>/dev/null | grep "cron_check.py" | grep -v "^#" | awk '{printf "%02d:%02d", $2, $1}' || echo "выкл"
}

usage() {
    echo -e "${BOLD}Использование:${RESET} checker <команда>"
    echo ""
    echo -e "  ${CYAN}start${RESET}          — запустить бота"
    echo -e "  ${CYAN}stop${RESET}           — остановить бота"
    echo -e "  ${CYAN}restart${RESET}        — перезапустить бота"
    echo -e "  ${CYAN}status${RESET}         — статус всех компонентов"
    echo -e "  ${CYAN}log${RESET}            — логи бота (последние 50 строк)"
    echo -e "  ${CYAN}log -f${RESET}         — логи в реальном времени"
    echo -e "  ${CYAN}run${RESET}            — запустить проверку всех серверов прямо сейчас"
    echo -e "  ${CYAN}run <id>${RESET}       — запустить проверку одного сервера (по ID)"
    echo -e "  ${CYAN}cron on [ЧЧ:ММ]${RESET} — включить автопроверку (по умолч: 09:00)"
    echo -e "  ${CYAN}cron off${RESET}       — выключить автопроверку"
    echo -e "  ${CYAN}cron status${RESET}    — показать расписание"
    echo ""
}

cmd="${1:-}"

case "$cmd" in

  start)
    systemctl start "${SVC}"
    sleep 1
    systemctl is-active --quiet "${SVC}" \
      && echo -e "${GREEN}✓${RESET} Бот запущен" \
      || echo -e "${RED}✗${RESET} Не удалось запустить (checker log)"
    ;;

  stop)
    systemctl stop "${SVC}"
    echo -e "${YELLOW}■${RESET} Бот остановлен"
    ;;

  restart)
    systemctl restart "${SVC}"
    sleep 1
    systemctl is-active --quiet "${SVC}" \
      && echo -e "${GREEN}✓${RESET} Бот перезапущен" \
      || echo -e "${RED}✗${RESET} Ошибка перезапуска (checker log)"
    ;;

  status)
    echo -e "${BOLD}SNI Monitoring Panel — статус${RESET}"
    echo ""
    ICON=$(_status_icon)
    STATE=$(systemctl is-active "${SVC}" 2>/dev/null || echo "unknown")
    echo -e "  Бот:          ${ICON} ${STATE}"
    echo -e "  Автопроверка: ⏰ $(_cron_time)"
    echo -e "  Логи:         ${LOG_DIR}/bot.log"
    if [[ -f "${LOG_DIR}/bot.log" ]]; then
        LAST=$(tail -1 "${LOG_DIR}/bot.log" 2>/dev/null | cut -c1-80)
        [[ -n "$LAST" ]] && echo -e "  Последняя строка: ${LAST}"
    fi
    echo ""
    systemctl status "${SVC}" --no-pager -l 2>/dev/null | tail -5 || true
    ;;

  log)
    shift || true
    if [[ "${1:-}" == "-f" ]]; then
        journalctl -u "${SVC}" -f --no-pager
    else
        journalctl -u "${SVC}" -n 50 --no-pager
    fi
    ;;

  run)
    SERVER_ID="${2:-}"
    if [[ -n "$SERVER_ID" ]]; then
        echo -e "${CYAN}▶ Проверяю сервер #${SERVER_ID}…${RESET}"
        "${PY}" "${BOT_DIR}/cron_check.py" --server-id "${SERVER_ID}"
    else
        echo -e "${CYAN}▶ Запускаю проверку всех серверов…${RESET}"
        "${PY}" "${BOT_DIR}/cron_check.py"
    fi
    ;;

  cron)
    SUBCMD="${2:-status}"
    case "$SUBCMD" in
      on)
        TIME="${3:-09:00}"
        HOUR=$(echo "$TIME"   | cut -d: -f1 | sed 's/^0*//' )
        MINUTE=$(echo "$TIME" | cut -d: -f2 | sed 's/^0*//' )
        HOUR="${HOUR:-9}"
        MINUTE="${MINUTE:-0}"
        CRON_CMD="${VENV}/bin/python3 ${BOT_DIR}/cron_check.py >> ${LOG_DIR}/cron.log 2>&1"
        CRON_JOB="${MINUTE} ${HOUR} * * * ${CRON_CMD}"
        MARKER="# SNI_MONITOR_DAILY_CHECK"
        CURRENT=$(crontab -l 2>/dev/null || true)
        CLEAN=$(echo "$CURRENT" | grep -v "cron_check.py\|SNI_MONITOR" || true)
        { echo "$CLEAN"; echo "$MARKER"; echo "$CRON_JOB"; } | crontab -
        echo -e "${GREEN}✓${RESET} Автопроверка включена: каждый день в $(printf '%02d:%02d' ${HOUR} ${MINUTE})"
        ;;
      off)
        CURRENT=$(crontab -l 2>/dev/null || true)
        echo "$CURRENT" | grep -v "cron_check.py\|SNI_MONITOR" | crontab -
        echo -e "${YELLOW}■${RESET} Автопроверка отключена"
        ;;
      status|*)
        CRON_LINE=$(crontab -l 2>/dev/null | grep "cron_check.py" | grep -v "^#" || true)
        if [[ -n "$CRON_LINE" ]]; then
            T=$(_cron_time)
            echo -e "${GREEN}✓${RESET} Автопроверка включена: каждый день в ${T}"
        else
            echo -e "${YELLOW}■${RESET} Автопроверка выключена"
            echo -e "  Включить: ${CYAN}checker cron on 09:00${RESET}"
        fi
        ;;
    esac
    ;;

  ""|help|--help|-h)
    usage
    ;;

  *)
    echo -e "${RED}Неизвестная команда:${RESET} $cmd"
    echo ""
    usage
    exit 1
    ;;

esac
CHECKER_SCRIPT

chmod +x /usr/local/bin/checker
ok "Команда checker установлена → /usr/local/bin/checker"


# ── Итог ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}"
cat << 'DONE'
  ╔══════════════════════════════════════════╗
  ║   ✅  Установка завершена успешно!       ║
  ╚══════════════════════════════════════════╝
DONE
echo -e "${RESET}"
echo -e "  Конфигурация:  ${BOLD}${ENV_FILE}${RESET}"
echo -e "  Логи бота:     ${BOLD}${LOG_DIR}/bot.log${RESET}"
echo -e "  Логи крона:    ${BOLD}${LOG_DIR}/cron.log${RESET}"
echo ""
echo -e "  Автопроверка:  каждый день в ${BOLD}$(printf '%02d:%02d' ${CRON_HOUR} ${CRON_MINUTE})${RESET}"
echo ""
echo -e "  ${BOLD}Быстрые команды (checker):${RESET}"
echo -e "    ${CYAN}checker status${RESET}             — статус бота и расписания"
echo -e "    ${CYAN}checker run${RESET}                — запустить проверку сейчас"
echo -e "    ${CYAN}checker log -f${RESET}             — логи в реальном времени"
echo -e "    ${CYAN}checker cron on 09:00${RESET}      — включить автопроверку"
echo -e "    ${CYAN}checker restart${RESET}            — перезапустить бота"
echo ""
echo -e "  ${YELLOW}Откройте Telegram и напишите боту /start${RESET}"
echo ""
