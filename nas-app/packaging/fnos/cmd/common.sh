#!/bin/bash
# 生命周期脚本公共函数。所有脚本都以 root 运行，仅用于管理 Docker 容器。
set -u

APP_NAME="nas-media"
CONTAINER="nas-media-web"
CONTAINER_PORT=8669
CONTAINER_MCP_PORT=8670

DEST="${TRIM_APPDEST:-/var/apps/${APP_NAME}/target}"
ETC="${TRIM_PKGETC:-/var/apps/${APP_NAME}/etc}"
VAR="${TRIM_PKGVAR:-/var/apps/${APP_NAME}/var}"
TMP="${TRIM_PKGTMP:-/tmp}"

log_error() {
  local message="$*"
  if [ -n "${TRIM_TEMP_LOGFILE:-}" ]; then
    printf '%s\n' "$message" >> "$TRIM_TEMP_LOGFILE"
  fi
  printf '%s\n' "$message" >&2
}

load_image() {
  IMAGE=""
  if [ -f "$DEST/docker/image.conf" ]; then
    # shellcheck disable=SC1090
    . "$DEST/docker/image.conf"
  fi
  IMAGE="${IMAGE:-ghcr.io/maotaozi54/nas-app:latest}"
}

app_port() {
  local port=""
  [ -f "$ETC/port" ] && port="$(cat "$ETC/port" 2>/dev/null)"
  case "$port" in
    ''|*[!0-9]*) port="$CONTAINER_PORT" ;;
  esac
  if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    port="$CONTAINER_PORT"
  fi
  printf '%s' "$port"
}

app_mcp_port() {
  local port=""
  [ -f "$ETC/mcp_port" ] && port="$(cat "$ETC/mcp_port" 2>/dev/null)"
  case "$port" in
    ''|*[!0-9]*) port="$CONTAINER_MCP_PORT" ;;
  esac
  if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    port="$CONTAINER_MCP_PORT"
  fi
  printf '%s' "$port"
}

# MCP 端口只映射到宿主机 loopback；端口被占用时禁用 MCP，不能让 Web UI 起不来。
mcp_port_free() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnH "sport = :$port" 2>/dev/null | grep -q . && return 1
    return 0
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}$" && return 1
    return 0
  fi
  return 0
}

read_token() {
  [ -f "$ETC/token" ] && cat "$ETC/token" 2>/dev/null || true
}

container_running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" = "true" ]
}

stop_container() {
  docker stop -t 20 "$CONTAINER" >/dev/null 2>&1 || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}

ensure_image() {
  docker image inspect "$IMAGE" >/dev/null 2>&1 && return 0
  local tar="$DEST/docker/nas-app-image.tar"
  if [ -f "$tar" ]; then
    log_error "正在导入离线镜像 $tar"
    docker load -i "$tar" >/dev/null 2>&1 || true
    docker image inspect "$IMAGE" >/dev/null 2>&1 && return 0
  fi
  log_error "正在拉取镜像 $IMAGE"
  docker pull "$IMAGE" >/dev/null 2>&1 && return 0
  return 1
}

# 收集用户授权目录：TRIM_DATA_ACCESSIBLE_PATHS 用 : 分隔（飞牛文档约定）
collect_paths() {
  PATHS=()
  local seen=" " path
  _add_path() {
    [ -n "$1" ] || return 0
    [ -d "$1" ] || return 0
    case "$seen" in
      *" $1 "*) return 0 ;;
    esac
    seen="$seen$1 "
    PATHS+=("$1")
  }
  if [ -n "${TRIM_DATA_ACCESSIBLE_PATHS:-}" ]; then
    local IFS=':'
    for path in $TRIM_DATA_ACCESSIBLE_PATHS; do
      _add_path "$path"
    done
  fi
  if [ -n "${TRIM_DATA_SHARE_PATHS:-}" ]; then
    local IFS=':'
    for path in $TRIM_DATA_SHARE_PATHS; do
      _add_path "$path"
    done
  fi
  if [ -f "$ETC/extra_paths" ]; then
    while IFS= read -r path; do
      _add_path "$path"
    done < "$ETC/extra_paths"
  fi
  [ ${#PATHS[@]} -gt 0 ]
}

_join_allowed() {
  local joined="" path
  for path in "${PATHS[@]}"; do
    joined="${joined:+$joined,}$path"
  done
  printf '%s' "$joined"
}

_run_container() {
  local with_host_gateway="$1"
  local token="$2"
  local allowed="$3"
  local mcp_port="$4"
  local publish_mcp="$5"
  local port
  port="$(app_port)"

  local args=(
    --name "$CONTAINER"
    --restart unless-stopped
    -d
    -p "${port}:${CONTAINER_PORT}"
    -e "NAS_APP_HOST=0.0.0.0"
    -e "NAS_APP_PORT=${CONTAINER_PORT}"
    -e "NAS_APP_TOKEN=${token}"
    -e "NAS_APP_ALLOWED_PATHS=${allowed}"
    -e "NAS_APP_MCP_HOST=0.0.0.0"
    -e "NAS_APP_MCP_PORT=${CONTAINER_MCP_PORT}"
    -e "TZ=${TZ:-Asia/Shanghai}"
    --log-driver json-file
    --log-opt max-size=10m
    --log-opt max-file=3
    -v "${VAR}:/data"
  )
  if [ "$publish_mcp" = "1" ]; then
    args+=(-p "127.0.0.1:${mcp_port}:${CONTAINER_MCP_PORT}")
    args+=(-e "NAS_APP_MCP_ENABLED=1")
  else
    args+=(-e "NAS_APP_MCP_ENABLED=0")
  fi
  if [ "$with_host_gateway" = "1" ]; then
    args+=(--add-host "host.docker.internal:host-gateway")
  fi
  local path
  for path in "${PATHS[@]}"; do
    args+=(-v "${path}:${path}")
  done
  args+=("$IMAGE")
  docker run "${args[@]}" >/dev/null 2>&1
}

_wait_ready() {
  local i state
  for i in $(seq 1 45); do
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER" 2>/dev/null || true)"
    case "$state" in
      healthy|running) return 0 ;;
      exited|dead) break ;;
    esac
    sleep 2
  done
  log_error "容器未能就绪（状态：${state:-unknown}）。最近日志："
  docker logs --tail 40 "$CONTAINER" >> "${TRIM_TEMP_LOGFILE:-/dev/null}" 2>&1 || true
  return 1
}

start_container() {
  command -v docker >/dev/null 2>&1 || { log_error "未找到 docker 命令"; return 1; }
  load_image
  ensure_image || { log_error "无法获取镜像 $IMAGE。可联网重试，或把镜像 tar 放到 $DEST/docker/nas-app-image.tar 后重装。"; return 1; }

  local token allowed
  token="$(read_token)"
  if [ ${#token} -lt 8 ]; then
    log_error "访问令牌缺失或少于 8 位，请在应用设置中重新保存配置。"
    return 1
  fi
  if ! collect_paths; then
    log_error "尚未授权媒体目录。请到「应用设置 → 授权目录」授权媒体库后重试。"
    return 1
  fi
  allowed="$(_join_allowed)"

  local mcp_port publish_mcp
  mcp_port="$(app_mcp_port)"
  publish_mcp=1
  if ! mcp_port_free "$mcp_port"; then
    publish_mcp=0
    log_error "MCP 端口 ${mcp_port} 已被占用，Octop 直连已禁用；Web UI（$(app_port)）不受影响。"
  fi

  if [ "$publish_mcp" = "1" ]; then
    _start_once 1 "$token" "$allowed" "$mcp_port" 1 && return 0
    log_error "带 MCP 端口映射启动失败，改为禁用 MCP 重试（优先保证 Web UI 可用）。"
    _start_once 1 "$token" "$allowed" "$mcp_port" 0 && return 0
    _start_once 0 "$token" "$allowed" "$mcp_port" 1 && return 0
  else
    _start_once 1 "$token" "$allowed" "$mcp_port" 0 && return 0
  fi
  # 旧版 Docker 不支持 host-gateway，或 MCP 映射不可用时，至少把 Web UI 拉起来
  _start_once 0 "$token" "$allowed" "$mcp_port" 0 && return 0
  log_error "docker run 失败，请检查端口 $(app_port) 是否被占用、镜像是否存在。"
  return 1
}

_start_once() {
  local gateway="$1" token="$2" allowed="$3" mcp_port="$4" publish_mcp="$5"
  stop_container
  if ! _run_container "$gateway" "$token" "$allowed" "$mcp_port" "$publish_mcp"; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    return 1
  fi
  _wait_ready
}
