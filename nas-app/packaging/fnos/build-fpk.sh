#!/usr/bin/env bash
# 构建飞牛 .fpk 安装包。
#
# 用法：
#   packaging/fnos/build-fpk.sh                 # 用 image.conf 里的镜像名打在线包
#   packaging/fnos/build-fpk.sh --with-image    # 同时构建并打入离线镜像 tar（体积大）
#   packaging/fnos/build-fpk.sh --image ghcr.io/me/nas-app:0.19.0 --with-image
#   packaging/fnos/build-fpk.sh --push-image    # 构建并推送多架构镜像后打包
#
# 有 fnpack 时使用官方工具；没有则按官方包结构手工打包（app/ 打成外层 app.tgz）。
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
# Windows 挂载目录下 manifest/image.conf 为 CRLF，需去掉 sed 带出的 \r
VERSION="$(sed -n 's/^version=//p' manifest | head -1 | tr -d '\r')"
APPNAME="$(sed -n 's/^appname=//p' manifest | head -1 | tr -d '\r')"
IMAGE="$(sed -n 's/^IMAGE=//p' app/docker/image.conf | head -1 | tr -d '\r')"
WITH_IMAGE=0
PUSH_IMAGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-image) WITH_IMAGE=1; shift ;;
    --push-image) PUSH_IMAGE=1; WITH_IMAGE=1; shift ;;
    --image) IMAGE="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

for required in manifest config/privilege config/resource ICON.PNG ICON_256.PNG app/ui/config app/ui/images/icon_64.png app/docker/image.conf cmd/main; do
  [[ -e "$required" ]] || { echo "缺少打包必需文件: $required" >&2; exit 1; }
done

PY=()
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys' >/dev/null 2>&1; then
    PY=("$candidate")
    break
  fi
done
if [[ ${#PY[@]} -eq 0 ]] && command -v py >/dev/null 2>&1 && py -3 -c 'import sys' >/dev/null 2>&1; then
  PY=(py -3)
fi
[[ ${#PY[@]} -gt 0 ]] || { echo "需要可用的 python3 做清单校验" >&2; exit 1; }
"${PY[@]}" - "$VERSION" <<'PY'
import json, sys
from pathlib import Path
version = sys.argv[1]
for name in ("app/ui/config", "wizard/install", "wizard/config", "wizard/uninstall", "config/privilege"):
    json.loads(Path(name).read_text(encoding="utf-8"))
allowed_types = {"tips", "password", "radio", "checkbox", "text", "input", "select", "number"}
for name in ("wizard/install", "wizard/config", "wizard/uninstall"):
    for step in json.loads(Path(name).read_text(encoding="utf-8")):
        for item in step.get("items", []):
            assert item.get("type") in allowed_types, f"{name} 含 fnpack 不支持的控件类型: {item.get('type')}"
manifest = Path("manifest").read_text(encoding="utf-8")
assert f"version={version}" in manifest, "版本号与 manifest 不一致"
print(f"清单校验通过：{version}")
PY

chmod +x cmd/* 2>/dev/null || true

if [[ "$WITH_IMAGE" == "1" ]]; then
  command -v docker >/dev/null 2>&1 || { echo "需要 docker 才能构建离线镜像" >&2; exit 1; }
  echo "构建镜像 $IMAGE ..."
  docker build -t "$IMAGE" "$ROOT"
  if [[ "$PUSH_IMAGE" == "1" ]]; then
    echo "推送镜像（amd64 + arm64）..."
    docker buildx build --platform linux/amd64,linux/arm64 -t "$IMAGE" --push "$ROOT"
  fi
  echo "导出离线镜像 app/docker/nas-app-image.tar ..."
  docker save "$IMAGE" -o app/docker/nas-app-image.tar
fi

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/${APPNAME}-${VERSION}.fpk"

if command -v fnpack >/dev/null 2>&1; then
  echo "使用 fnpack 打包 ..."
  rm -f ./*.fpk
  fnpack build --directory .
  built="$(ls -t ./*.fpk 2>/dev/null | head -1 || true)"
  [[ -n "$built" ]] || { echo "fnpack 未生成 fpk" >&2; exit 1; }
  mv "$built" "$OUT"
else
  echo "未找到 fnpack，按 fnOS 官方包结构手工打包（外层 app.tgz + 清单/图标/脚本）"
  STAGE="$(mktemp -d)"
  trap 'rm -rf "$STAGE"' EXIT
  # 应用负载：app/ 目录内容打成 app.tgz，解压后直接落在 target 根目录（docker/、ui/）
  mapfile -t APP_ENTRIES < <(find app -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)
  tar -czf "$STAGE/app.tgz" -C app "${APP_ENTRIES[@]}"
  cp -a manifest ICON.PNG ICON_256.PNG cmd config wizard "$STAGE/"
  # fnpack 会在 manifest 里写入 checksum = app.tgz 的 md5，这里保持一致
  printf 'checksum=%s\n' "$(md5sum "$STAGE/app.tgz" | cut -d' ' -f1)" >> "$STAGE/manifest"
  chmod +x "$STAGE"/cmd/* 2>/dev/null || true
  rm -f "$OUT"
  tar -czf "$OUT" -C "$STAGE" manifest app.tgz cmd config wizard ICON.PNG ICON_256.PNG
fi

echo "完成：$OUT"
ls -lh "$OUT"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$OUT"
fi
