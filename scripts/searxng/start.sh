#!/usr/bin/env bash
# 启动 Gleam 自托管 SearXNG（幂等；Docker 单容器，无 compose）。
# 安全边界：仅绑定 127.0.0.1:8888；无 host networking、无 --privileged、
# 无 docker socket 挂载；绝不触碰无关容器（含 8080 上的 originblame-demo）。
# 生成的 secret 与镜像 digest 均为 git 忽略的本地文件，不入库。
set -euo pipefail

IMAGE="searxng/searxng:latest"
CONTAINER="gleam-searxng"
HOST_BIND="127.0.0.1"
HOST_PORT="8888"
CONTAINER_PORT="8080"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HERE/settings.yml"
ENV_FILE="$HERE/.searxng.env"
DIGEST_FILE="$HERE/digest.txt"
HEALTH_URL="http://127.0.0.1:${HOST_PORT}/healthz"
WAIT_SECS="45"

[ -f "$SETTINGS" ] || { echo "ERROR: missing $SETTINGS" >&2; exit 1; }

# 镜像在场检查：优先复用已拉取镜像；缺失才拉取（离线环境会在此显式失败）
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "image $IMAGE not found locally, pulling ..."
    docker pull "$IMAGE"
fi

record_digest() {
    docker image inspect -f '{{index .RepoDigests 0}}' "$IMAGE" > "$DIGEST_FILE" 2>/dev/null \
        || docker image inspect -f '{{.Id}}' "$IMAGE" > "$DIGEST_FILE"
    chmod 600 "$DIGEST_FILE" 2>/dev/null || true
}

port_in_use() {
    # 任何监听 8888 的 socket（含 0.0.0.0/[::]）都视为占用
    ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "(^|:)${HOST_PORT}\$"
}

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    # 归属校验：容器名相同但镜像不是 searxng/searxng 的一律拒绝触碰
    c_image="$(docker container inspect -f '{{.Config.Image}}' "$CONTAINER")"
    case "$c_image" in
        searxng/searxng*) ;;
        *) echo "ERROR: container '$CONTAINER' exists but image is '$c_image' (not searxng/searxng); refusing to touch it." >&2; exit 1 ;;
    esac
    state="$(docker container inspect -f '{{.State.Status}}' "$CONTAINER")"
    if [ "$state" = "running" ]; then
        echo "reusing running container $CONTAINER"
    else
        echo "starting existing container $CONTAINER (state: $state)"
        docker start "$CONTAINER" >/dev/null
    fi
else
    if port_in_use; then
        echo "ERROR: port ${HOST_PORT} already in use by a non-$CONTAINER process; refusing to bind." >&2
        exit 1
    fi
    # 生成 secret（幂等）：仅写入 git 忽略的 .searxng.env，权限 600
    if [ ! -f "$ENV_FILE" ]; then
        umask 077
        printf 'SEARXNG_SECRET=%s\n' \
            "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > "$ENV_FILE"
        echo "generated $ENV_FILE (git-ignored)"
    fi
    echo "creating container $CONTAINER ($HOST_BIND:$HOST_PORT -> $CONTAINER_PORT)"
    docker run -d \
        --name "$CONTAINER" \
        --publish "${HOST_BIND}:${HOST_PORT}:${CONTAINER_PORT}" \
        --env-file "$ENV_FILE" \
        --env FORCE_OWNERSHIP=false \
        --volume "$SETTINGS:/etc/searxng/settings.yml:ro" \
        --security-opt no-new-privileges \
        "$IMAGE" >/dev/null
fi

record_digest

# 就绪等待：/healthz 返回 2xx
echo -n "waiting for $HEALTH_URL "
deadline=$(( $(date +%s) + WAIT_SECS ))
while :; do
    if curl -sf --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
        echo " OK"
        echo "$CONTAINER ready at http://${HOST_BIND}:${HOST_PORT} (digest: $(cat "$DIGEST_FILE"))"
        exit 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo " TIMEOUT after ${WAIT_SECS}s" >&2
        docker logs --tail 20 "$CONTAINER" >&2 || true
        exit 1
    fi
    echo -n "."
    sleep 2
done
