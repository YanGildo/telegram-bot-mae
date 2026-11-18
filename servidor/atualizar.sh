#!/usr/bin/env bash
# Atualiza o bot depois de extrair um zip novo por cima da pasta.
# Uso: bash servidor/atualizar.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
sudo systemctl restart bot-mae
sleep 3
sudo systemctl --no-pager --lines=5 status bot-mae || true
