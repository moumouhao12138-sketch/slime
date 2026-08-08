#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
DOCKER_BINARY=${SLIME_DOCKER_BINARY:-docker}
IMAGE=slime-cairn-kali:0.0.21
BASE_IMAGE=docker.1ms.run/kalilinux/kali-rolling
KALI_APT_MIRROR=https://mirrors.ustc.edu.cn/kali/
KALI_APT_VERIFY_PEER=false
INSTALL_NATIVE_AGENTS=true
INSTALL_REFERENCE_ASSETS=false
LAB_NETWORK=slime-cairn-lab
WAIT_SECONDS=180

usage() {
  cat <<'EOF'
Usage: sh scripts/finish-docker.sh [options]

  --docker-binary PATH
  --image NAME
  --base-image NAME
  --kali-apt-mirror URL
  --kali-apt-verify-peer true|false
  --install-native-agents true|false
  --install-reference-assets true|false
  --lab-network NAME
  --wait-seconds NUMBER
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --docker-binary) DOCKER_BINARY=$2; shift 2 ;;
    --image) IMAGE=$2; shift 2 ;;
    --base-image) BASE_IMAGE=$2; shift 2 ;;
    --kali-apt-mirror) KALI_APT_MIRROR=$2; shift 2 ;;
    --kali-apt-verify-peer) KALI_APT_VERIFY_PEER=$2; shift 2 ;;
    --install-native-agents) INSTALL_NATIVE_AGENTS=$2; shift 2 ;;
    --install-reference-assets) INSTALL_REFERENCE_ASSETS=$2; shift 2 ;;
    --lab-network) LAB_NETWORK=$2; shift 2 ;;
    --wait-seconds) WAIT_SECONDS=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v "$DOCKER_BINARY" >/dev/null 2>&1 && [ ! -x "$DOCKER_BINARY" ]; then
  echo "Docker CLI not found: $DOCKER_BINARY" >&2
  exit 1
fi

deadline=$(( $(date +%s) + WAIT_SECONDS ))
while ! "$DOCKER_BINARY" info --format '{{.ServerVersion}}' >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Docker Engine did not become ready within $WAIT_SECONDS seconds" >&2
    exit 1
  fi
  sleep 3
done

"$DOCKER_BINARY" build \
  --progress=plain \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --build-arg "KALI_APT_MIRROR=$KALI_APT_MIRROR" \
  --build-arg "KALI_APT_VERIFY_PEER=$KALI_APT_VERIFY_PEER" \
  --build-arg "INSTALL_NATIVE_AGENTS=$INSTALL_NATIVE_AGENTS" \
  --build-arg "INSTALL_REFERENCE_ASSETS=$INSTALL_REFERENCE_ASSETS" \
  --tag "$IMAGE" \
  "$PROJECT_ROOT/worker"

if ! "$DOCKER_BINARY" network inspect "$LAB_NETWORK" >/dev/null 2>&1; then
  "$DOCKER_BINARY" network create --driver bridge --internal "$LAB_NETWORK" >/dev/null
fi

"$DOCKER_BINARY" run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges "$IMAGE" \
  python3 -c "import json,shutil; d=json.load(open('/opt/slime-cairn/tools.json')); print('tools',len(d),'agents',{n:bool(shutil.which(n)) for n in ('codex','claude','pi')})"

echo "Docker/Kali ready: $IMAGE"
