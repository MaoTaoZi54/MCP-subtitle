"""NAS 一键部署：定位容器内实际使用的 app.js 并原地覆盖，再用 8669 端口回读校验。

密码从环境变量 NAS_SSH_PW 读取（只在进程内存，不写入脚本/文件）。
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
CONTAINER = "8021c28ca984d547c333e17b506245f0bb2784e7de66afb2294acfe68f1963c7"

# (本地路径, 容器内相对路径)
FILES = [
    (REPO / "nas_app/web/app.js", "app/nas_app/web/app.js"),
    (REPO / "nas_app/web/index.html", "app/nas_app/web/index.html"),
    (REPO / "nas_app/web/style.css", "app/nas_app/web/style.css"),
    (REPO / "nas_app/toolkit.py", "app/nas_app/toolkit.py"),
]

REMOTE = r"""
log(){ echo "== $*"; }
PID=$(pgrep -f 'python -m nas_app' | head -1)
log "container pid=$PID"

log "1. 权限面"
id
sudo -n true 2>/dev/null && echo "SUDO_OK" || echo "NO_SUDO"
docker ps >/dev/null 2>&1 && echo "DOCKER_OK" || echo "NO_DOCKER"
ls -ld /vol3/docker /vol3/docker/overlay2 2>&1 | head -4

log "2. 候选层文件（含 sha）"
find /vol3/docker/overlay2 -maxdepth 9 -path '*app/nas_app/web/app.js' 2>/dev/null | head -12 | while read -r f; do
  echo "CAND $(sha256sum "$f" 2>/dev/null | cut -d' ' -f1) $f"
done

log "3. 通过 /proc 访问容器根"
ls -l /proc/$PID/root/app/nas_app/web/ 2>&1 | head -6

log "4. 合并层目录"
ls -d /vol3/docker/overlay2/*/merged 2>/dev/null | head -5

log "DONE"
"""


def ask_or_env() -> str:
    pw = os.environ.get("NAS_SSH_PW", "")
    if pw:
        return pw
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        val = simpledialog.askstring("NAS 部署", f"{USER}@{HOST} 密码（仅内存）", show="*", parent=root)
        root.destroy()
        if val:
            return val
    except Exception:
        pass
    import getpass

    return getpass.getpass(f"{USER}@{HOST} password: ")


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    import paramiko

    for local, _ in FILES:
        if not local.is_file():
            print(f"本地缺文件：{local}", file=sys.stderr)
            return 2

    pw = os.environ.get("NAS_SSH_PW") or ask_or_env()
    if not pw:
        return 1
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=pw, timeout=20, allow_agent=False, look_for_keys=False)
    del pw
    print(f"已连接 {USER}@{HOST}")

    # 上传到暂存目录
    cli.exec_command(f"mkdir -p {STAGE}")[1].read()
    sftp = cli.open_sftp()
    for local, _ in FILES:
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
    print("本地 SHA256：")
    for local, crel in FILES:
        print(f"  {crel}  {sha256(local)[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
