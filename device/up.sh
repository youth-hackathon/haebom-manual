#!/usr/bin/env bash
# 기기를 켜고 호스트 adb 를 붙입니다. 이미 켜져 있으면 adb 연결만 다시 확인합니다.
set -euo pipefail
cd "$(dirname "$0")"

SERIAL=127.0.0.1:6520

# 처음에는 이미지 내려받기가 있어 오래 걸립니다. 최대 40분까지 기다립니다.
docker compose up -d --wait --wait-timeout 2400

adb connect "$SERIAL" >/dev/null
adb -s "$SERIAL" wait-for-device
echo "sys.boot_completed=$(adb -s "$SERIAL" shell getprop sys.boot_completed | tr -d '\r')"
adb devices
