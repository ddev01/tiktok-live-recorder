#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
remote_path=${REMOTE_PATH:-__DEFAULT__}
stack_name=${STACK_NAME:-tt-clipper}
certresolver=${TRAEFIK_CERTRESOLVER:-letsencryptresolver}
healthcheck_url=${HEALTHCHECK_URL:-$(grep -m1 '^HEALTHCHECK_URL=' "$root/.env" 2>/dev/null | cut -d= -f2-)}
image_tag=$(git -C "$root" rev-parse --short=12 HEAD)
mode=deploy

if [[ $# -eq 1 && $1 == --validate ]]; then
  mode=validate
elif [[ $# -ne 0 ]]; then
  echo "Usage: REMOTE_PATH=/path STACK_NAME=name TRAEFIK_CERTRESOLVER=name HEALTHCHECK_URL=url $0 [--validate]" >&2
  exit 2
fi

# $1 = validate | preflight | deploy. The image is built here for linux/amd64 and
# streamed to the server, so the 2-vCPU VPS never compiles ffmpeg.
remote() {
  ssh server bash -s -- "$remote_path" "$stack_name" "$certresolver" "$healthcheck_url" "$image_tag" "$1" <<'REMOTE'
set -Eeuo pipefail

remote_path=$1
stack_name=$2
certresolver=$3
healthcheck_url=$4
image_tag=$5
mode=$6
if [[ $remote_path == __DEFAULT__ ]]; then
  remote_path="$HOME/tt-clipper"
fi
if [[ $remote_path == '~/'* ]]; then
  remote_path="$HOME/${remote_path#~/}"
fi

if [[ ! $stack_name =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "Invalid STACK_NAME '$stack_name' (use lowercase letters, digits, '-' or '_')." >&2
  exit 2
fi
if [[ ! -d $remote_path ]]; then
  mkdir -p "$(dirname "$remote_path")"
  git clone --branch main https://github.com/ddev01/tiktok-live-recorder.git "$remote_path"
fi
cd "$remote_path"
if [[ $(git rev-parse --show-toplevel) != "$(pwd -P)" ]]; then
  echo "REMOTE_PATH must be the root of the app Git checkout." >&2
  exit 1
fi
if [[ $mode != validate ]]; then
  git pull --ff-only
  if [[ $(git rev-parse --short=12 HEAD) != "$image_tag" ]]; then
    echo "Server checkout is at $(git rev-parse --short=12 HEAD) but the image was built from $image_tag; push the commit first." >&2
    exit 1
  fi
fi

if ! docker info --format '{{.Swarm.LocalNodeState}}' | grep -qx active; then
  echo "Docker Swarm is not active on this server." >&2
  exit 1
fi
if ! docker secret inspect tiktok_session_ss >/dev/null 2>&1; then
  echo "Required Swarm secret 'tiktok_session_ss' is missing. Provision it on the server, then retry; this script will not create or read it." >&2
  exit 1
fi
if ! docker secret inspect game_webhook_secret >/dev/null 2>&1; then
  echo "Required Swarm secret 'game_webhook_secret' is missing. Provision it on the server, then retry; this script will not create or read it." >&2
  exit 1
fi
if ! docker secret inspect game_webhook_token >/dev/null 2>&1; then
  echo "Required Swarm secret 'game_webhook_token' is missing. Provision it on the server, then retry; this script will not create or read it." >&2
  exit 1
fi
if ! docker network inspect infra-public >/dev/null 2>&1; then
  echo "Required external Docker network 'infra-public' is missing." >&2
  exit 1
fi

export STACK_NAME="$stack_name"
export IMAGE_TAG="$image_tag"
export NODE_HOSTNAME="$(docker info --format '{{.Name}}')"
export TRAEFIK_CERTRESOLVER="$certresolver"
export HEALTHCHECK_URL="$healthcheck_url"
docker stack config --compose-file docker-stack.yml >/dev/null

if [[ $mode == validate ]]; then
  echo "Stack configuration and required Swarm resources validated; nothing was deployed."
  exit 0
fi
if [[ $mode == preflight ]]; then
  echo "Server preflight passed for ${stack_name}:${image_tag}; nothing transferred or deployed."
  exit 0
fi

docker stack deploy --resolve-image never --compose-file docker-stack.yml "$stack_name"
docker stack services "$stack_name"

# Each commit gets its own tag. Keep the deployed image plus the previous
# build as a rollback target, drop anything older.
docker image ls --format '{{.Repository}}:{{.Tag}}' --filter "reference=${stack_name}:*" \
  | grep -vx "${stack_name}:${image_tag}" | tail -n +2 \
  | xargs -r -n1 docker image rm >/dev/null 2>&1 || true
REMOTE
}

remote "$mode"
if [[ $mode == validate ]]; then
  exit 0
fi

remote preflight

docker buildx build --platform linux/amd64 --file "$root/Dockerfile.web" \
  --tag "${stack_name}:${image_tag}" --load "$root"

# No registry: stream the image straight into the server's image store.
docker save "${stack_name}:${image_tag}" | gzip -1 | ssh server 'gunzip | docker load'

remote deploy
