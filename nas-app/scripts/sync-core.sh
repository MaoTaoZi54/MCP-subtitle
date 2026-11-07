#!/usr/bin/env bash
# 把 Octop 插件里的能力包同步到独立应用（唯一事实来源：nas-octop-plugin/nas_mcp）。
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
src="$root/../nas-octop-plugin/nas_mcp"
dst="$root/nas_mcp"
if [[ ! -d "$src" ]]; then
  echo "找不到能力包：$src" >&2
  exit 1
fi
rm -rf "$dst"
cp -a "$src" "$dst"
find "$dst" -name '__pycache__' -type d -prune -exec rm -rf {} +
echo "已同步 $(find "$dst" -name '*.py' | wc -l | tr -d ' ') 个模块 → $dst"
