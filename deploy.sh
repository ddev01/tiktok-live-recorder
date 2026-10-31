#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
remote_path=${REMOTE_PATH:-__DEFAULT__}
stack_name=${STACK_NAME:-tt-clipper}
certresolver=${TRAEFIK_CERTRESOLVER:-letsencryptresolver}
healthcheck_url=${HEALTHCHECK_URL:-$(grep -m1 '^HEALTHCHECK_URL=' "$root/.env" 2>/dev/null | cut -d= -f2-)}
mode=deploy

if [[ $# -eq 1 && $1 == --validate ]]; then
  mode=validate
elif [[ $# -ne 0 ]]; then
  echo "Usage: REMOTE_PATH=/path STACK_NAME=name TRAEFIK_CERTRESOLVER=name HEALTHCHECK_URL=url $0 [--validate]" >&2
  exit 2
fi

ssh server bash -s -- "$remote_path" "$stack_name" "$certresolver" "$healthcheck_url" "$mode" <<'REMOTE'
set -Eeuo pipefail

remote_path=$1
stack_name=$2
certresolver=$3
healthcheck_url=$4
mode=$5
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
if [[ $mode == deploy ]]; then
  git pull --ff-only
fi
image_tag=$(git rev-parse --short=12 HEAD)

if ! docker info --format '{{.Swarm.LocalNodeState}}' | grep -qx active; then
  echo "Docker Swarm is not active on this server." >&2
  exit 1
fi
if ! docker secret inspect tiktok_session_ss >/dev/null 2>&1; then
  echo "Required Swarm secret 'tiktok_session_ss' is missing. Provision it on the server, then retry; this script will not create or read it." >&2
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

docker build --file Dockerfile.web --tag "${stack_name}:${image_tag}" .
docker stack deploy --resolve-image never --compose-file docker-stack.yml "$stack_name"
docker stack services "$stack_name"

# Each commit gets its own tag. Keep the deployed image plus the previous
# build as a rollback target, drop anything older.
docker image ls --format '{{.Repository}}:{{.Tag}}' --filter "reference=${stack_name}:*" \
  | grep -vx "${stack_name}:${image_tag}" | tail -n +2 \
  | xargs -r -n1 docker image rm >/dev/null 2>&1 || true
REMOTE
