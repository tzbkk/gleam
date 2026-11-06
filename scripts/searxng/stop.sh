#!/usr/bin/env bash
# 停止并删除 Gleam 的 SearXNG 容器（仅 gleam-searxng 一个目标）。
# 归属双重校验（容器名 + 镜像为 searxng/searxng）后才停止/删除；
# 绝不触碰任何无关容器（含 8080 上的 originblame-demo）。
# .searxng.env 与 digest.txt 保留（下次 start 幂等复用）。
set -euo pipefail

CONTAINER="gleam-searxng"

if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "no container named '$CONTAINER'; nothing to do"
    exit 0
fi

c_image="$(docker container inspect -f '{{.Config.Image}}' "$CONTAINER")"
case "$c_image" in
    searxng/searxng*) ;;
    *) echo "ERROR: container '$CONTAINER' has image '$c_image' (not searxng/searxng); refusing to touch it." >&2; exit 1 ;;
esac

state="$(docker container inspect -f '{{.State.Status}}' "$CONTAINER")"
if [ "$state" = "running" ]; then
    echo "stopping $CONTAINER (image: $c_image)"
    docker stop "$CONTAINER" >/dev/null
fi
echo "removing $CONTAINER"
docker rm "$CONTAINER" >/dev/null
echo "done; originblame-demo and other containers untouched"
