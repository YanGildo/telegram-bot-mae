#!/usr/bin/env bash
# Instala o bot num servidor Ubuntu e deixa ligado 24 h (reinicia sozinho se cair).
# Uso: bash servidor/instalar.sh   (rodar de dentro da pasta do bot ou de qualquer lugar)
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=bot-mae
cd "$APP_DIR"

echo "==> Pasta do bot: $APP_DIR"
if [ ! -f .env ]; then
  echo "==> Primeira instalação: vou criar o arquivo .env."
  read -r -p "Cole o TOKEN do bot mãe (do @BotFather) e aperte Enter: " TK
  read -r -p "Seu ID do Telegram (só números) e aperte Enter: " UID_
  printf 'BOT_TOKEN=%s\nADMIN_IDS=%s\n' "$TK" "$UID_" > .env
  chmod 600 .env
  echo "    .env criado."
fi
# .env vindo do Windows pode ter quebras de linha \r
sed -i 's/\r$//' .env

echo "==> Instalando Python..."
sudo apt-get update -y -qq
sudo apt-get install -y -qq python3 python3-venv >/dev/null

if ! python3 -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)'; then
  echo "ERRO: o Python deste servidor ($(python3 -V)) não é 3.10 a 3.14. Use Ubuntu 22.04 ou 24.04."
  exit 1
fi

echo "==> Instalando dependências..."
python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
mkdir -p dados

echo "==> Criando o serviço $SERVICE..."
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<EOF
[Unit]
Description=Bot mae (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m app
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE
sleep 4
echo
sudo systemctl --no-pager --lines=8 status $SERVICE || true
echo
echo "Pronto. Comandos úteis:"
echo "  ver o que o bot está fazendo:  journalctl -u $SERVICE -f"
echo "  reiniciar:                     sudo systemctl restart $SERVICE"
echo "  parar:                         sudo systemctl stop $SERVICE"
