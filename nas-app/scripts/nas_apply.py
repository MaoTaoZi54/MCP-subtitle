"""NAS 一次性部署+自检：定位容器内 app.js → 原地覆盖 4 个文件 → 8669 端口回读校验。

密码只通过本机弹窗输入并留在进程内存；不写入命令行、不落盘。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

HOST = os.environ.get("NAS_HOST", "192.168.1.100")
USER = os.environ.get("NAS_USER", "test")
REPO = Path(__file__).resolve().parents[1]
STAGE = "/tmp/nas-deploy"

FILES = [
    (REPO / "nas_app/web/app.js", "app/nas_app/web/app.js", "app.js"),
    (REPO / "nas_app/web/index.html", "app/nas_app/web/index.html", "index.html"),
    (REPO / "nas_app/web/style.css", "app/nas_app/web/style.css", "style.css"),
    (REPO / "nas_app/toolkit.py", "app/nas_app/toolkit.py", "toolkit.py"),
]

REMOTE = r"""
STAGE=/tmp/nas-deploy
log(){ echo "== $*"; }
PID=$(pgrep -f 'python -m nas_app' | head -1)
REL_DIR=app/nas_app

log "0. 环境"
id
sudo -n true 2>/dev/null && echo "SUDO_OK" || echo "NO_SUDO"
docker ps >/dev/null 2>&1 && echo "DOCKER_OK" || echo "NO_DOCKER"

log "1. 候选目标（容器根 / 合并层 / 差异层 / 镜像链接层）"
TARGETS=/tmp/nas-targets.txt
: > "$TARGETS"
# a) 直接走 /proc 进容器根
[ -d "/proc/$PID/root/$REL_DIR/web" ] && echo "/proc/$PID/root" >> "$TARGETS"
# b) merged / diff / l
find /vol3/docker/overlay2 -maxdepth 3 -type d -name merged 2>/dev/null >> "$TARGETS"
find /vol3/docker/overlay2 -maxdepth 3 -type d -name diff 2>/dev/null >> "$TARGETS"
find /vol3/docker/overlay2/l -maxdepth 1 -type l 2>/dev/null >> "$TARGETS"
sort -u -o "$TARGETS" "$TARGETS"
while read -r base; do
  [ -d "$base/$REL_DIR" ] && echo "BASE $base"
done < "$TARGETS"

log "2. 覆盖写（同 inode：先截断再写入，保留权限/inode）"
wrote_any=0
deploy_file(){ # $1=暂存文件名 $2=相对目录 $3=basename
  local src="$STAGE/$1" found=0 n=0
  while read -r base; do
    local dst="$base/$2/$3"
    [ -f "$dst" ] || continue
    found=1
    if { : > "$dst"; } 2>/dev/null && cat "$src" > "$dst" 2>/dev/null && cmp -s "$src" "$dst"; then
      echo "WROTE $dst"; n=$((n+1))
    else
      echo "FAIL  $dst ($(ls -l "$dst" 2>/dev/null | awk '{print $1,$3}'))"
    fi
  done < "$TARGETS"
  [ "$found" -eq 0 ] && echo "NO_TARGET $3"
  [ "$n" -gt 0 ] && wrote_any=1
  return 0
}
deploy_file app.js      "$REL_DIR/web" app.js
deploy_file index.html  "$REL_DIR/web" index.html
deploy_file style.css   "$REL_DIR/web" style.css
deploy_file toolkit.py  "$REL_DIR"     toolkit.py

log "3. 线上回读校验（8669）"
for f in app.js index.html style.css; do
  curl -s -m 5 -o /tmp/chk.out -w "%{http_code} %{size_download}" "http://127.0.0.1:8669/static/$f" > /tmp/chk.code 2>/dev/null
  code=$(cut -d' ' -f1 /tmp/chk.code); size=$(cut -d' ' -f2 /tmp/chk.code)
  echo "LIVE $f code=$code size=$size sha=$(sha256sum /tmp/chk.out 2>/dev/null | cut -d' ' -f1)"
done
echo "LOCAL_EXPECT app.js=$(sha256sum "$STAGE/app.js" | cut -d' ' -f1)"
echo "LOCAL_EXPECT index.html=$(sha256sum "$STAGE/index.html" | cut -d' ' -f1)"
echo "LOCAL_EXPECT style.css=$(sha256sum "$STAGE/style.css" | cut -d' ' -f1)"

log "4. toolkit.py 生效方式"
if [ "$(docker ps >/dev/null 2>&1 && echo ok)" = "ok" ] || [ "$(sudo -n true 2>/dev/null && echo ok)" = "ok" ]; then
  echo "RESTART_AVAILABLE"
else
  echo "RESTART_NEEDS_PANEL（Python 改动需在飞牛面板重启容器）"
fi
log "DONE"
"""


def ask_password() -> str:
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        pw = simpledialog.askstring(
            "NAS 部署（最后一次输入）",
            f"{USER}@{HOST} 的 SSH 密码\n密码只在内存中使用，不写入任何文件",
            show="*",
            parent=root,
        )
        root.destroy()
        if pw:
            return pw
    except Exception as exc:
        print(f"[warn] GUI 不可用：{exc}", file=sys.stderr)
    import getpass

    return getpass.getpass(f"{USER}@{HOST} password: ")


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    import paramiko

    for local, _, _ in FILES:
        if not local.is_file():
            print(f"本地缺文件：{local}", file=sys.stderr)
            return 2

    pw = ask_password()
    if not pw:
        print("未输入密码，取消。", file=sys.stderr)
        return 1

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=pw, timeout=20, allow_agent=False, look_for_keys=False)
    del pw
    print(f"已连接 {USER}@{HOST}")

    cli.exec_command(f"mkdir -p {STAGE}")[1].read()
    sftp = cli.open_sftp()
    for local, _, _ in FILES:
        sftp.put(str(local), f"{STAGE}/{local.name}")
        print(f"上传 {local.name} ({local.stat().st_size} B)")
    sftp.close()

    stdin, stdout, stderr = cli.exec_command("bash -s", get_pty=False)
    stdin.write(REMOTE)
    stdin.channel.shutdown_write()
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace").strip()
    if err:
        print("[stderr] " + err)
    cli.close()

    print("----- 本地基准 SHA256 -----")
    for local, crel, _ in FILES:
        print(f"  {crel}  {sha256(local)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
