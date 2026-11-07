"""NAS 部署二：定位 docker overlay2 里运行中的那份 app.js 并原地覆盖。

只读探测 + 尝试同 inode 覆盖（test 无 root 时可能失败，失败会如实报告）。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

HOST = os.environ.get("NAS_HOST", "192.168.1.100")
USER = os.environ.get("NAS_USER", "test")
REPO = Path(__file__).resolve().parents[1]

FILES = [
    ("nas_app/web/app.js", "app/nas_app/web/app.js", "app.js"),
    ("nas_app/web/index.html", "app/nas_app/web/index.html", "index.html"),
    ("nas_app/web/style.css", "app/nas_app/web/style.css", "style.css"),
    ("nas_app/toolkit.py", "app/nas_app/toolkit.py", "toolkit.py"),
]

REMOTE = r"""
SERVED_SHA=7f0e3962f9995255dd857d0b7d3ac28032522402a91e5b8e5639f0665bf8780a
log(){ echo "== $*"; }

log "1. docker 目录权限"
ls -ld /vol3 /vol3/docker /vol3/docker/overlay2 /vol3/docker/containers 2>&1 | head -6
id

log "2. 容器配置（读容器 8021c28ca984 的挂载/upperdir）"
CFG=/vol3/docker/containers/8021c28ca984d547c333e17b506245f0bb2784e7de66afb2294acfe68f1963c7/config.v2.json
if [ -r "$CFG" ]; then
  python3 - "$CFG" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print("Name:",d.get("Name"),"Image:",d.get("Image"))
for m in d.get("MountPoints",{}).values():
    print("Mount:",m.get("Source"),"->",m.get("Destination"),"rw=",m.get("RW"))
PY
else
  log "CFG_UNREADABLE $CFG"
fi

log "3. overlay2 里的 nas_app/web/app.js 候选"
find /vol3/docker/overlay2 -maxdepth 8 -path '*app/nas_app/web/app.js' 2>/dev/null | head -10 | while read -r f; do
  echo "CAND $f $(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)"
done

log "4. 与线上版本一致的层文件（serve sha=$SERVED_SHA）"
find /vol3/docker/overlay2 -maxdepth 8 -path '*app/nas_app/web/app.js' 2>/dev/null | while read -r f; do
  s=$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)
  [ "$s" = "$SERVED_SHA" ] && echo "MATCH $f"
done

log "5. sudo / docker 权限"
sudo -n true 2>/dev/null && echo "SUDO_OK" || echo "NO_SUDO"
docker ps >/dev/null 2>&1 && echo "DOCKER_OK" || echo "NO_DOCKER"

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
            "NAS 部署二", f"{USER}@{HOST} 的 SSH 密码（仅内存）", show="*", parent=root
        )
        root.destroy()
        if pw:
            return pw
    except Exception as exc:
        print(f"[warn] GUI 不可用：{exc}", file=sys.stderr)
    import getpass

    return getpass.getpass(f"{USER}@{HOST} password: ")


def main() -> int:
    import paramiko

    pw = ask_password()
    if not pw:
        return 1
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=pw, timeout=20, allow_agent=False, look_for_keys=False)
    del pw
    stdin, stdout, stderr = cli.exec_command("bash -s", get_pty=False)
    stdin.write(REMOTE)
    stdin.channel.shutdown_write()
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace").strip()
    if err:
        print("[stderr] " + err)
    cli.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
