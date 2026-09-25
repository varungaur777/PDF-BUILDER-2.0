#!/usr/bin/env bash
# Run the PDF bot on your own Ubuntu server (e.g. Oracle Cloud free tier).
#   bash server-setup.sh          install / update and start
#   bash server-setup.sh config   change token, chat ids or watermark
set -e
cd "$(dirname "$0")"
say() { printf '\n\033[1;32m%s\033[0m\n' "$*"; }

if [ "$1" != "config" ]; then
  if ! command -v docker >/dev/null; then
    say "Installing Docker (2–3 minutes)…"
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER" || true
  fi
  mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  if [ "$mem_mb" -lt 2000 ] && ! swapon --show | grep -q .; then
    say "Adding 2 GB swap (server has ${mem_mb} MB RAM)…"
    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  fi
fi

old() { [ -f .env ] && grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }
ask() {
  local cur; cur=$(old "$1"); cur=${cur:-$3}
  read -r -p "$2 [${cur}]: " val
  printf '%s=%s\n' "$1" "${val:-$cur}" >> .env.new
}
if [ ! -f .env ] || [ "$1" = "config" ]; then
  say "Settings (press Enter to keep the value in [brackets])"
  : > .env.new
  ask TELEGRAM_BOT_TOKEN "Bot token (from @BotFather)" ""
  ask TELEGRAM_CHAT_ID   "Team/admin id(s), comma separated" ""
  ask LOG_CHAT_ID        "Log channel id (blank = no log)" ""
  ask PUBLIC_BOT         "Public bot (anyone can use in DM): true/false" "true"
  mv .env.new .env && chmod 600 .env
fi

if docker info >/dev/null 2>&1; then DC="docker compose"; else DC="sudo docker compose"; fi
say "Building and starting (first time takes ~3 minutes)…"
git pull --ff-only 2>/dev/null || true
$DC up -d --build

say "Done! The bot is running 24x7 and restarts by itself after a reboot."
echo "IMPORTANT: on GitHub open Actions → Telegram bot → ⋯ → Disable workflow,"
echo "           otherwise GitHub and the server fight over the same bot."
echo
echo "See what it's doing:  $DC logs -f --tail 50"
echo "Update after changes: bash server-setup.sh"
echo "Change settings:      bash server-setup.sh config"
