#!/bin/bash
# 由 MariaDB 官方 image 在 volume 首次初始化時執行（掛載到 /docker-entrypoint-initdb.d/）。
# image 只會建立 MARIADB_DATABASE 一個庫並授權給 MARIADB_USER；測試用的 <db>_test 要自己建。
# 只在第一次初始化時跑：已存在的 volume 要 `docker compose down -v` 重建才會重新執行。
# 檔案保持「非可執行」時 entrypoint 會 source 它，docker_process_sql helper 可用；
# 若被 chmod +x，則以 mariadb client 執行（fallback 分支）。

TEST_DB="${MARIADB_DATABASE}_test"
SQL="CREATE DATABASE IF NOT EXISTS \`${TEST_DB}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON \`${TEST_DB}\`.* TO '${MARIADB_USER}'@'%';
FLUSH PRIVILEGES;"

if declare -F docker_process_sql > /dev/null; then
  docker_process_sql <<< "$SQL"
else
  mariadb --protocol=socket -uroot -p"${MARIADB_ROOT_PASSWORD}" <<< "$SQL"
fi

echo "[init] created database ${TEST_DB} and granted ALL to ${MARIADB_USER}"
