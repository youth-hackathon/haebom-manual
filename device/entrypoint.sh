#!/usr/bin/env bash
# Cuttlefish 컨테이너의 시작 스크립트입니다.
# 1) 이미지에 들어 있는 서비스(네트워크 브리지, operator, host orchestrator)를 띄웁니다.
# 2) Android 이미지가 볼륨에 없으면 내려받습니다.
# 3) 기기가 없으면 만들고, 있으면 다시 켭니다.
# 4) 컨테이너가 멈출 때 기기를 끕니다.
set -u

BUILD_DIR=/cf/build
ADB=/usr/lib/cuttlefish-common/bin/adb
SERIAL=0.0.0.0:6520
READY=/run/device-ready

: "${CF_BUILD:=16968706/aosp_cf_x86_64_only_phone-userdebug}"
: "${CF_CPUS:=4}"
: "${CF_MEMORY_MB:=4096}"
: "${CF_WIDTH:=1080}"
: "${CF_HEIGHT:=2400}"
: "${CF_DPI:=420}"
: "${CF_GPU_MODE:=guest_swiftshader}"
: "${CF_LOCALE:=ko-KR}"
: "${CF_TIMEZONE:=Asia/Seoul}"
# 한글 키보드. AOSP 기본 키보드에는 한글 자판이 없어서 공개 소스 키보드 HeliBoard(GPL-3.0)를 받아 설치합니다. 비우면 건너뜁니다.
: "${CF_KEYBOARD_URL=https://github.com/HeliBorg/HeliBoard/releases/download/v4.2/HeliBoard_4.2-release.apk}"
: "${CF_KEYBOARD_SHA256=69b2b2fb349934371b1ed2d3569541cc0bda987f8aaf5eb5f5b157961f76de86}"
: "${CF_KEYBOARD_ID=helium314.keyboard/.latin.LatinIME}"

log() { echo "[device] $(date -u +%H:%M:%S) $*"; }

rm -f "$READY"
cd /root

for dev in /dev/kvm /dev/vhost-net /dev/vhost-vsock /dev/net/tun; do
  if [ ! -e "$dev" ]; then
    log "$dev 가 없습니다. 커널 모듈이 올라가 있는지 확인하십시오 (README 참고)."
    exit 1
  fi
done

# 이미지의 /root/run_services.sh 와 같은 순서입니다. 마지막의 tail 만 뺐습니다.
service busybox-syslogd start
service nginx start
service cuttlefish-host-resources start
service cuttlefish-operator start
service cuttlefish-host_orchestrator start

if [ ! -f "$BUILD_DIR/.fetched" ]; then
  log "Android 이미지를 내려받습니다: $CF_BUILD"
  if ! cvd fetch --default_build="$CF_BUILD" --target_directory="$BUILD_DIR" --noenable_caching; then
    log "내려받기에 실패했습니다."
    exit 1
  fi
  echo "$CF_BUILD" > "$BUILD_DIR/.fetched"
  log "내려받기가 끝났습니다."
elif [ "$(cat "$BUILD_DIR/.fetched")" != "$CF_BUILD" ]; then
  log "볼륨에 있는 빌드($(cat "$BUILD_DIR/.fetched"))를 그대로 씁니다. $CF_BUILD 로 바꾸려면 볼륨을 지우십시오."
fi

# Docker Desktop 은 컨테이너에서 AF_VSOCK 소켓을 만들지 못하게 막습니다.
# --vhost_user_vsock=true 를 주면 호스트 쪽 통신이 유닉스 소켓으로 바뀌어 이 제한을 피합니다.
# cvd start 는 이전에 준 값을 기억하지 않으므로 만들 때와 다시 켤 때 같은 값을 줍니다.
START_ARGS=(
  --cpus="$CF_CPUS" --memory_mb="$CF_MEMORY_MB"
  --x_res="$CF_WIDTH" --y_res="$CF_HEIGHT" --dpi="$CF_DPI"
  --gpu_mode="$CF_GPU_MODE"
  --vhost_user_vsock=true
)

stop_device() {
  log "기기를 끕니다."
  rm -f "$READY"
  # cvd stop 은 가상 머신을 바로 끄므로, 그 전에 게스트의 쓰기를 디스크로 내립니다.
  timeout 10 "$ADB" connect "$SERIAL" >/dev/null 2>&1
  timeout 10 "$ADB" -s "$SERIAL" shell sync >/dev/null 2>&1
  cvd stop >/dev/null 2>&1
  exit 0
}
trap stop_device TERM INT

if cvd fleet 2>/dev/null | grep -q '"group_name"'; then
  log "기존 기기를 다시 켭니다."
  # 컨테이너가 강제로 죽었던 경우 기기 목록에 켜진 상태로 남아 있을 수 있습니다.
  cvd stop >/dev/null 2>&1
  cvd start "${START_ARGS[@]}" || { log "기기를 켜지 못했습니다."; exit 1; }
else
  log "기기를 새로 만듭니다."
  cvd create --host_path="$BUILD_DIR" --product_path="$BUILD_DIR" "${START_ARGS[@]}" \
    || { log "기기를 만들지 못했습니다."; exit 1; }
fi
log "부팅이 끝났습니다."

"$ADB" connect "$SERIAL" >/dev/null 2>&1
"$ADB" -s "$SERIAL" wait-for-device

# 시스템 언어는 userdata 에 남습니다. 값이 다를 때만 바꿉니다.
if [ -n "$CF_LOCALE" ]; then
  current=$("$ADB" -s "$SERIAL" shell cmd locale get-device-locale | tr -d '\r')
  if [ "$current" != "$CF_LOCALE" ]; then
    log "시스템 언어를 $current 에서 $CF_LOCALE 로 바꿉니다."
    "$ADB" -s "$SERIAL" shell cmd locale set-device-locale "$CF_LOCALE"
  fi
fi
# 한국 폰처럼: 시간대는 한국, 시계는 오전/오후, 아래 바는 버튼 세 개, 잠금 화면 없음. userdata 에 남으므로 값이 다를 때만 바꿉니다.
if [ -n "$CF_TIMEZONE" ]; then
  current=$("$ADB" -s "$SERIAL" shell getprop persist.sys.timezone | tr -d '\r')
  if [ "$current" != "$CF_TIMEZONE" ]; then
    log "시간대를 $current 에서 $CF_TIMEZONE 로 바꿉니다."
    "$ADB" -s "$SERIAL" shell settings put global auto_time_zone 0
    "$ADB" -s "$SERIAL" shell su 0 cmd time_zone_detector suggest_manual_time_zone --zone_id "$CF_TIMEZONE" >/dev/null 2>&1
  fi
fi
"$ADB" -s "$SERIAL" shell settings put system time_12_24 12
"$ADB" -s "$SERIAL" shell cmd overlay enable-exclusive --category com.android.internal.systemui.navbar.threebutton >/dev/null 2>&1
"$ADB" -s "$SERIAL" shell locksettings set-disabled true >/dev/null 2>&1

# 한글 키보드: 없으면 받아서 설치하고 기본 키보드로 둡니다. 가상 기기에는 하드웨어 키보드가 붙어 있어 화면 키보드가
# 숨겨지므로 보이게 합니다. 받은 파일은 빌드 볼륨에 남겨 다시 받지 않습니다.
if [ -n "$CF_KEYBOARD_URL" ]; then
  if ! "$ADB" -s "$SERIAL" shell pm path "${CF_KEYBOARD_ID%%/*}" | grep -q "package:"; then
    apk=/cf/build/keyboard.apk
    if ! echo "$CF_KEYBOARD_SHA256  $apk" | sha256sum -c - >/dev/null 2>&1; then
      log "한글 키보드를 내려받습니다."
      curl -fsSL -o "$apk" "$CF_KEYBOARD_URL" || log "키보드를 받지 못했습니다."
    fi
    if echo "$CF_KEYBOARD_SHA256  $apk" | sha256sum -c - >/dev/null 2>&1; then
      "$ADB" -s "$SERIAL" install -r "$apk" >/dev/null 2>&1 && log "한글 키보드를 설치했습니다."
    else
      log "키보드 파일의 체크섬이 다릅니다. 설치하지 않습니다."
    fi
  fi
  "$ADB" -s "$SERIAL" shell ime enable "$CF_KEYBOARD_ID" >/dev/null 2>&1
  "$ADB" -s "$SERIAL" shell ime set "$CF_KEYBOARD_ID" >/dev/null 2>&1
  "$ADB" -s "$SERIAL" shell settings put secure show_ime_with_hard_keyboard 1
fi
"$ADB" kill-server >/dev/null 2>&1

touch "$READY"
log "준비되었습니다. 호스트에서 adb connect 127.0.0.1:6520 으로 붙으십시오."

tail -f /dev/null &
wait $!
