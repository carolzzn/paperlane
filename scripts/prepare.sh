#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  task_uid=$(id -u)
  task_gid=$(id -g)
  sed -e "s/^PUID=.*/PUID=$task_uid/" -e "s/^PGID=.*/PGID=$task_gid/" .env.example > .env
  chmod 600 .env
fi
mkdir -p data photos/待打印 photos/已完成
printf '%s\n' '目录已创建。请确认 .env 中的 PUID/PGID 与目录属主匹配；打印机可在网页设置中添加。' '然后运行：docker compose up -d --build'
