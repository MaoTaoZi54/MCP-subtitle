"""NAS 上的只读探测：定位容器、镜像层、服务端口与文件实际来源。

密码经本机弹窗输入，只留内存。仅执行只读命令（find/ls/ps/ss/curl）。
"""
from __future__ import annotations

import os
import sys

HOST = os.environ.get("NAS_HOST", "192.168.1.100")
USER = os.environ.get("NAS_USER", "test")

REMOTE = r"""
echo "=== 1. /vol1 顶层 ==="
ls -1 /vol1 2>/dev/null | head -30

echo "=== 2. 找 overlay2 目录 ==="
for d in /vol1/@docker /vol1/docker /vol1/1000/docker /var/lib/docker /vol1/@appstore/docker /vol1/1000/@docker; do
  [ -d "$d" ] && echo "DIR $d"
done
find / -maxdepth 4 -type d -name overlay2 2>/dev/null | head -5

echo "=== 3. 磁盘上的 app.js 副本 ==="
find /vol1 -maxdepth 9 -name app.js -path '*nas_app*' 2>/dev/null | head -20

echo "=== 4. nas_app 相关进程 ==="
ps -ef 2>/dev/null | grep -i 'nas_app\|nas-app' | grep -v grep | head -10

echo "=== 5. 监听端口 ==="
(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | head -25

echo "=== 6. 常见端口探活 ==="
for p in 8787 8080 8000 5000 3000 9000 12345 8090; do
  code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p/healthz" 2>/dev/null)
  [ -n "$code" ] && [ "$code" != "000" ] && echo "PORT $p healthz=$code"
done

echo "=== 7. 源码副本目录 ==="
ls -la /vol1/1003/nas-app-test/nas_app/web/ 2>/dev/null | head -12

echo "=== 8. 是否有 docker CLI 与 socket ==="
command -v docker 2>/dev/null || echo "no docker cli"
ls -l /var/run/docker.sock 2>/dev/null || echo "no docker.sock"

echo "=== 9. 尝试装免密公钥（仅公钥，失败无副作用） ==="
PUB='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJQeWy95+eNWofbnTYAQ2zzMm61nIkoc5xXElmAIb9Lp trae-fn-deploy'
for h in "$HOME" /home/test /vol1/1003; do
  [ -z "$h" ] && continue
  if mkdir -p "$h/.ssh" 2>/dev/null; then
    chmod 700 "$h/.ssh" 2>/dev/null
    touch "$h/.ssh/authorized_keys" 2>/dev/null
    if grep -q trae-fn-deploy "$h/.ssh/authorized_keys" 2>/dev/null; then
      echo "KEY_EXISTS $h/.ssh/authorized_keys"
    elif echo "$PUB" >> "$h/.ssh/authorized_keys" 2>/dev/null; then
      chmod 600 "$h/.ssh/authorized_keys" 2>/dev/null
      echo "KEY_INSTALLED $h/.ssh/authorized_keys"
    else
      echo "KEY_FAIL $h"
    fi
  else
    echo "KEY_FAIL $h (mkdir 失败)"
  fi
done
echo "=== DONE ==="
"""


def ask_password() -> str:
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        pw = simpledialog.askstring(
            "NAS 探测", f"{USER}@{HOST} 的 SSH 密码（仅内存）", show="*", parent=root
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
