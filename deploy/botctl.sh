#!/usr/bin/env bash
# Управление ботом на сервере с Мака: ./deploy/botctl.sh status|logs|restart|stop|start|deploy|backup|pull-backups
# Бот работает на сервере (служба avia-bot). На Маке бота НЕ запускать: два бота на один токен мешают друг другу.
set -euo pipefail

HOST="${AVIA_HOST:-root@193.41.86.92}"
KEY="${AVIA_KEY:-$HOME/.ssh/aeza_pool}"
DIR="/home/avia/avia-bot"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH_OPTS=(-i "$KEY" -o BatchMode=yes -o ConnectTimeout=15)

remote() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

case "${1:-}" in
  status)
    remote 'systemctl is-active avia-bot; systemctl status avia-bot --no-pager -n 0 | sed -n 1,4p; journalctl -u avia-bot -n 5 --no-pager'
    ;;
  logs)
    remote "journalctl -u avia-bot -n ${2:-60} --no-pager"
    ;;
  restart|stop|start)
    remote "systemctl $1 avia-bot && sleep 3 && systemctl is-active avia-bot || true"
    ;;
  deploy)
    echo "Тесты…"
    (cd "$ROOT" && .venv/bin/python -m pytest -q 2>&1 | tail -2)
    echo "Копирую код на сервер…"
    rsync -a --delete \
      --exclude .venv --exclude state --exclude debug --exclude .env \
      --exclude .git --exclude .pytest_cache --exclude __pycache__ \
      -e "ssh ${SSH_OPTS[*]}" "$ROOT"/ "$HOST:$DIR/"
    remote "chown -R avia:avia $DIR && chmod 600 $DIR/.env && systemctl restart avia-bot && sleep 5 && systemctl is-active avia-bot && journalctl -u avia-bot -n 3 --no-pager"
    ;;
  backup)
    remote "cd $DIR && sudo -u avia .venv/bin/python -c 'from avia.config import load_settings; from avia.backup import backup_all; s=load_settings(); print(len(backup_all(s.storage_dir, s.backup_dir, s.backup_keep)), \"копий создано\")'"
    ;;
  pull-backups)
    # копии баз с сервера на Мак (без --delete: старые копии на Маке остаются); папка по умолчанию ~/avia-backups
    DEST="${AVIA_BACKUP_DEST:-$HOME/avia-backups}"
    mkdir -p "$DEST"
    rsync -a -e "ssh ${SSH_OPTS[*]}" "$HOST:$DIR/state/backups/" "$DEST/"
    echo "Копии на Маке: $DEST ($(find "$DEST" -type f | wc -l | tr -d ' ') файлов, $(du -sh "$DEST" | cut -f1))"
    ;;
  *)
    echo "Использование: $0 status|logs [N]|restart|stop|start|deploy|backup|pull-backups"
    exit 1
    ;;
esac
