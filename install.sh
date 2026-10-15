#!/usr/bin/env bash
# Установка интернетометра на Raspberry Pi (Raspberry Pi OS Bookworm и новее).
# Ставит клиент Яндекс Интернетометра, демон со стрелкой и веб-интерфейс.
set -euo pipefail

GO_VERSION="1.27.1"
CLI_REPO="https://github.com/Master290/internetometer-cli.git"

TARGET_USER="${SUDO_USER:-$USER}"
TARGET_DIR="/home/${TARGET_USER}/internetometer"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Зависимости"
sudo apt-get update
sudo apt-get install -y python3 python3-gpiozero python3-lgpio git curl ca-certificates iw iproute2

# ---------------------------------------------------------------- Go
# go.mod у internetometer-cli требует Go 1.25+, а в репозитории Debian лежит 1.19–1.22.
need_go=1
if command -v go >/dev/null 2>&1; then
  have="$(go version | grep -oP 'go\K[0-9]+\.[0-9]+' || echo 0)"
  if [ "$(printf '%s\n1.25\n' "$have" | sort -V | head -1)" = "1.25" ]; then
    need_go=0
    echo "==> Go ${have} подходит"
  else
    echo "==> Go ${have} слишком старый, ставлю ${GO_VERSION}"
  fi
fi

if [ "${need_go}" = "1" ]; then
  case "$(uname -m)" in
    aarch64|arm64) GOARCH="arm64" ;;
    armv7l|armv6l) GOARCH="armv6l" ;;
    x86_64)        GOARCH="amd64" ;;
    *) echo "Неизвестная архитектура $(uname -m)"; exit 1 ;;
  esac
  echo "==> Ставлю Go ${GO_VERSION} для ${GOARCH}"
  sudo apt-get remove -y golang-go >/dev/null 2>&1 || true
  curl -fsSL -o /tmp/go.tar.gz "https://go.dev/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz"
  sudo rm -rf /usr/local/go
  sudo tar -C /usr/local -xzf /tmp/go.tar.gz
  rm -f /tmp/go.tar.gz
  export PATH="/usr/local/go/bin:${PATH}"
  grep -q '/usr/local/go/bin' "/home/${TARGET_USER}/.profile" 2>/dev/null || \
    echo 'export PATH=$PATH:/usr/local/go/bin' >> "/home/${TARGET_USER}/.profile"
fi
export PATH="/usr/local/go/bin:${PATH}"

# ---------------------------------------------------------------- клиент Яндекса
echo "==> Собираю клиент Яндекс Интернетометра"
CLI_DIR="/home/${TARGET_USER}/internetometer-cli"
if [ -d "${CLI_DIR}/.git" ]; then
  git -C "${CLI_DIR}" pull --ff-only
else
  git clone "${CLI_REPO}" "${CLI_DIR}"
fi
cd "${CLI_DIR}"
go mod tidy
go build -o internetometer ./cmd/cli/main.go
sudo install -m 0755 internetometer /usr/local/bin/internetometer
echo "    /usr/local/bin/internetometer готов"

echo "==> Пробный запуск клиента"
/usr/local/bin/internetometer --all --json --lang ru --concurrency 4 | tail -3 || \
  echo "    Клиент отработал с ошибкой — проверьте вручную"

# ---------------------------------------------------------------- демон
echo "==> Копирую файлы в ${TARGET_DIR}"
mkdir -p "${TARGET_DIR}"
if [ "${SRC_DIR}" != "${TARGET_DIR}" ]; then
  cp -f "${SRC_DIR}/internetometer.py" "${SRC_DIR}/index.html" "${TARGET_DIR}/"
  cp -f "${SRC_DIR}/scale-maker.html" "${TARGET_DIR}/" 2>/dev/null || true
  [ -f "${TARGET_DIR}/config.json" ] || cp -f "${SRC_DIR}/config.json" "${TARGET_DIR}/"
fi
chmod +x "${TARGET_DIR}/internetometer.py"
chown -R "${TARGET_USER}:${TARGET_USER}" "${TARGET_DIR}"

echo "==> Проверяю, как разбирается ответ клиента"
python3 "${TARGET_DIR}/internetometer.py" --probe --no-gpio 2>&1 | tail -20 || true

# ---------------------------------------------------------------- каналы
if python3 -c "import json,sys; sys.exit(0 if json.load(open('${TARGET_DIR}/config.json')).get('interfaces') else 1)" 2>/dev/null; then
  echo "==> В конфигурации есть отдельные интерфейсы, готовлю маршруты"
  python3 "${TARGET_DIR}/internetometer.py" --setup-routing > "${TARGET_DIR}/setup-routing.sh"
  chmod +x "${TARGET_DIR}/setup-routing.sh"
  if grep -q "ip rule add" "${TARGET_DIR}/setup-routing.sh"; then
    sudo "${TARGET_DIR}/setup-routing.sh"
    sed "s|/home/pi/internetometer|${TARGET_DIR}|g" "${SRC_DIR}/internetometer-routing.service" \
      | sudo tee /etc/systemd/system/internetometer-routing.service >/dev/null
    sudo systemctl enable internetometer-routing.service
  else
    # режим switch/route: правила маршрутизации не нужны, только права sudo
    sudo "${TARGET_DIR}/setup-routing.sh"
  fi
fi

echo "==> Автозапуск"
sed "s|/home/pi/internetometer|${TARGET_DIR}|g; s|^User=pi$|User=${TARGET_USER}|" \
  "${SRC_DIR}/internetometer.service" | sudo tee /etc/systemd/system/internetometer.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now internetometer.service

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Готово."
echo "  Веб-интерфейс:    http://${IP}:8080/"
echo "  Журнал службы:    sudo journalctl -u internetometer -f"
echo "  Проверить стрелку: python3 ${TARGET_DIR}/internetometer.py --sweep"
