# Cuttlefish 기기 (Docker)

Docker 컨테이너 안에서 Android Cuttlefish 가상 기기 한 대를 켜고, 호스트의 adb 로 붙을 수 있게 합니다.
Windows 11 + Docker Desktop(WSL2) 에서 2026-11-21 에 확인했습니다.

## 구성

| 항목 | 값 |
|---|---|
| 컨테이너 이미지 | `us-docker.pkg.dev/android-cuttlefish-artifacts/cuttlefish-orchestration/cuttlefish-orchestration:stable` 을 digest `sha256:20917e0072c482ddbd5992d874fee7159e5f98730679ac15c5f92dd8f34a7f7f` 로 고정했습니다. cuttlefish 호스트 패키지 1.57.15 가 들어 있습니다. |
| Android 빌드 | 빌드 ID `16968706`, 타깃 `aosp_cf_x86_64_only_phone-userdebug` (브랜치 `aosp-android-latest-release`). Android 17, SDK 37, `CP2A.260605.016`. 로그인 없이 받습니다. |
| 기기 | x86_64, CPU 4개, 메모리 4096MB, 화면 1080x2400 420dpi, 소프트웨어 GPU(`guest_swiftshader`), 시스템 언어 ko-KR |

파일은 세 개입니다.

- `docker-compose.yml` : 서비스가 두 개 있습니다. `kvm-modules` 는 커널 모듈을 올리고 끝납니다. `cuttlefish` 는 기기를 켭니다.
- `entrypoint.sh` : 컨테이너 시작 스크립트입니다. Android 이미지가 없으면 내려받고, 기기가 없으면 만들고, 있으면 다시 켭니다. 컨테이너가 멈출 때 기기를 끕니다.
- `up.sh` : `docker compose up` 을 실행하고 호스트 adb 를 붙인 뒤 부팅 여부를 출력합니다.

내려받은 Android 이미지와 기기 상태는 docker 볼륨에 들어갑니다. 이 폴더에는 남지 않습니다.

| 볼륨 | 내용 | 실제 디스크 사용량 |
|---|---|---|
| `haebom-device_cf-build` | 호스트 도구와 Android 이미지 | 2.9GB |
| `haebom-device_cf-state` | 기기 상태(userdata 포함), cvd 의 기기 목록 | 처음 0.3GB, 쓰는 만큼 늘어납니다 |

`docker system df -v` 에는 18.6GB 와 2.5GB 로 나옵니다. 이미지 파일이 빈 구간을 포함한 크기(각 8GB)로 계산되기 때문이고, 디스크를 실제로 차지하는 양은 위 표와 같습니다.

## 명령

Git Bash 에서 이 폴더로 들어와 실행합니다.

```bash
# 켜기. 처음에는 이미지를 내려받습니다.
./up.sh

# 위 스크립트가 하는 일을 직접 하려면
docker compose up -d --wait --wait-timeout 2400
adb connect 127.0.0.1:6520
adb -s 127.0.0.1:6520 shell getprop sys.boot_completed   # 1 이 나오면 부팅이 끝난 것입니다

# 진행 상황 보기
docker compose logs -f cuttlefish

# 끄기. 볼륨은 남습니다.
docker compose down

# 기기만 초기 상태로 되돌리기. 이미지는 다시 받지 않습니다.
docker compose down
docker volume rm haebom-device_cf-state

# 전부 지우기
docker compose down -v
```

`docker compose down` 후 다시 `up` 하면 같은 기기가 켜집니다. 설치한 앱과 설정이 남아 있고, 이미지를 다시 받지 않습니다.
`down` 을 하면 기기가 꺼지므로 그때 진행 중이던 adb 작업은 끊깁니다. 다시 켠 뒤 `adb connect 127.0.0.1:6520` 을 실행합니다.

## 포트

모두 호스트의 127.0.0.1 에만 엽니다.

| 호스트 포트 | 용도 |
|---|---|
| 6520/tcp | adb. `adb connect 127.0.0.1:6520` |
| 1443/tcp | WebRTC 화면 페이지(HTTPS, 자체 서명 인증서) |
| 15550-15599/tcp, udp | WebRTC 미디어 |

## 브라우저로 화면 보기

- 기기 목록: `https://127.0.0.1:1443/`
- 기기 화면: `https://127.0.0.1:1443/devices/cvd_1-1-1/files/client.html`

인증서가 자체 서명이라 브라우저 경고가 한 번 나옵니다.
이 PC 의 Chrome(헤드리스) 으로 열어 연결 상태가 `connected` 이고 1080x2400 영상이 재생되는 것을 확인했습니다.

## 걸린 시간과 자원

이 PC(16코어, Docker VM 메모리 31GB) 에서 잰 값입니다.

| 항목 | 값 |
|---|---|
| 처음 켜기 전체 (`docker compose up -d --wait`) | 16분 47초 |
| 그중 이미지 내려받기 | 16분 0초. 내려받기 속도가 약 2MB/s 였습니다. |
| 기기 만들기부터 부팅 완료까지 | 28초 |
| `docker compose down` | 15초 |
| 그 뒤 다시 `./up.sh` | 30~46초 (부팅 18~30초) |
| 내려받는 양 | 2.06GB (호스트 도구 0.90GB, 이미지 1.16GB) |
| 디스크 | 컨테이너 이미지 1.8GB(`docker image ls` 기준) + 볼륨 3.2GB |
| 메모리 (대기 중, `docker stats`) | 3.2~4.6GiB. 기기 메모리 4GB 를 게스트가 쓰는 만큼 늘어납니다. |
| CPU (대기 중, `docker stats`) | 15~25% (코어 하나 기준) |

## 설정 바꾸기

`docker-compose.yml` 의 환경 변수로 바꿉니다. 셸에서 같은 이름의 변수를 주어도 됩니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CF_BUILD` | `16968706/aosp_cf_x86_64_only_phone-userdebug` | 받을 빌드. `aosp-android-latest-release/aosp_cf_x86_64_only_phone-userdebug` 로 주면 그 브랜치의 최신 빌드를 받습니다. |
| `CF_CPUS`, `CF_MEMORY_MB` | 4, 4096 | 기기의 CPU 수와 메모리 |
| `CF_WIDTH`, `CF_HEIGHT`, `CF_DPI` | 1080, 2400, 420 | 화면 |
| `CF_GPU_MODE` | `guest_swiftshader` | GPU 방식 |
| `CF_LOCALE` | `ko-KR` | 시스템 언어. 비우면 건드리지 않습니다. |
| `CF_TIMEZONE` | `Asia/Seoul` | 시간대. 비우면 건드리지 않습니다. |
| `CF_KEYBOARD_URL` | HeliBoard 4.2 의 APK 주소 | 한글 키보드. AOSP 기본 키보드에는 한글 자판이 없어서 공개 소스 키보드 HeliBoard(GPL-3.0)를 받아 설치하고 기본 키보드로 둡니다. 빈 값을 주면 건너뜁니다. |

CPU, 메모리, 화면 값은 `down` 후 `up` 하면 적용됩니다.
`CF_BUILD` 를 바꿀 때는 두 볼륨을 지우고 다시 켭니다(`docker compose down -v`).

## 접근성 서비스 켜기

이 빌드에서 아래 명령이 동작하는 것을 확인했습니다. 설정을 넣으면 서비스가 바로 연결되고 `accessibility_enabled` 도 1 로 바뀝니다.

```bash
adb -s 127.0.0.1:6520 shell settings put secure enabled_accessibility_services <패키지>/<서비스 클래스>
```

앱을 먼저 설치해야 합니다. 설치되지 않은 서비스 이름을 넣으면 명령은 성공하지만 시스템이 값을 바로 지웁니다.

## 알려진 제한

- Docker Desktop 을 다시 시작하면 `kvm_intel`, `vhost_vsock`, `vhost_net` 모듈이 내려갑니다. `docker compose up` 을 하면 `kvm-modules` 서비스가 다시 올립니다. 모듈을 직접 내린 뒤 `./up.sh` 로 기기가 켜지는 것을 확인했습니다. 컨테이너를 `docker start` 로 직접 켜면 이 단계가 빠지므로 `docker compose up` 이나 `./up.sh` 를 씁니다.
- Docker Desktop 은 privileged 컨테이너에서도 `AF_VSOCK` 소켓 생성을 막습니다(`Function not implemented`). 그래서 `--vhost_user_vsock=true` 로 켭니다. 이 값을 빼면 `Setup failed for cuttlefish::ModemSimulator` 로 실패합니다.
- GPU 가속이 없습니다. 화면은 CPU 로 그립니다.
- 컨테이너 하나에 기기 한 대만 켭니다.
- 64비트 전용 빌드입니다(`ro.product.cpu.abilist` 는 `x86_64,arm64-v8a`). 32비트 네이티브 라이브러리만 든 APK 는 설치되지 않습니다.
- 켤 때마다 한국 폰처럼 맞춥니다: 한국어, 한국 시간, 오전/오후 시계, 아래에 버튼 세 개짜리 바, 잠금 화면 없음, 한글 키보드. 이 값들은 기기 상태 볼륨에 남으므로 스크립트는 다를 때만 다시 넣습니다. 이 부분은 켜져 있는 기기에 같은 명령을 직접 넣어 확인했고, 컨테이너를 다시 켜서 돌려 보지는 않았습니다.
- 컨테이너 시작 로그에 `add table bridge cuttlefish_bridge ... No such file or directory` 가 나옵니다. WSL2 커널에 nftables 의 bridge 테이블이 없어서 나오는 것이고, 기기의 네트워크는 동작합니다.

## 일반 Linux VM 으로 옮길 때

Linux VM 에서는 아직 실행해 보지 않았습니다. 달라질 것으로 보는 점입니다.

- 중첩 가상화가 켜진 VM 이어야 `/dev/kvm` 이 있습니다(GCP 는 인스턴스를 만들 때 nested virtualization 을 켭니다).
- `kvm-modules` 서비스는 장치 파일이 이미 있으면 아무것도 하지 않습니다. 없으면 호스트에서 `sudo modprobe vhost_vsock vhost_net` 을 실행합니다. 배포판 커널 모듈은 압축되어 있어 alpine 의 modprobe 로는 올라가지 않을 수 있습니다.
- `AF_VSOCK` 제한은 Docker Desktop 에만 있습니다. `--vhost_user_vsock=true` 는 그대로 두어도 됩니다.
- 지금은 브라우저와 컨테이너가 같은 PC 에 있어서 WebRTC 가 127.0.0.1 로 연결됩니다. 다른 PC 의 브라우저에서 보려면 브라우저가 VM 의 1443 과 15550-15599(tcp, udp) 포트에 닿아야 합니다. `network_mode: host` 로 바꾸고 방화벽에서 이 포트를 허용하는 방법이 있습니다.
- adb 포트 6520 은 인증 없이 기기를 조작할 수 있습니다(`ro.adb.secure` 가 비어 있습니다). 외부로 열지 말고 SSH 터널(`ssh -L 6520:127.0.0.1:6520`) 로 씁니다.
