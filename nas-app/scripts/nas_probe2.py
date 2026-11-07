"""NAS 探测二：确认 nas_app 进程的工作目录、端口，以及 HTTP 实际返回的静态文件版本。

只读：/proc 读取、curl、sha256sum、grep 配置。
"""
from __future__ import annotations

import os
import sys

HOST = os.environ.get("NAS_HOST", "192.168.1.100")
USER = os.environ.get("NAS_USER", "test")

REMOTE = r"""
PID=$(pgrep -f 'python -m nas_app' | head -1)
echo "=== PID $PID ==="
echo "--- cwd ---"; ls -l /proc/$PID/cwd 2>/dev/null
echo "--- root ---"; ls -l /proc/$PID/root 2>/dev/null
echo "--- exe ---"; ls -l /proc/$PID/exe 2>/dev/null
echo "--- cmdline ---"; tr '\0' ' ' < /proc/$PID/cmdline 2>/dev/null; echo
echo "--- environ(NAS) ---"; tr '\0' '\n' < /proc/$PID/environ 2>/dev/null | grep -i 'nas\|port\|token' | head -10
echo "--- cgroup(判断是否容器) ---"; head -3 /proc/$PID/cgroup 2>/dev/null

echo "=== 源码目录里的端口配置 ==="
grep -o '"port"[^,}]*' /vol1/1003/nas-app-test/*.json 2>/dev/null | head -5
ls -1 /vol1/1003/nas-app-test/ 2>/dev/null | head -20

echo "=== 各监听端口取 /static/app.js ==="
for p in 8089 8898 8899 8000 8669 15005 42423 10001 80; do
  out=$(curl -s -m 3 -o /tmp/probe_app.js -w '%{http_code} %{size_download}' "http://127.0.0.1:$p/static/app.js" 2>/dev/null)
  code=$(echo "$out" | cut -d' ' -f1)
  if [ "$code" = "200" ]; then
    h=$(sha256sum /tmp/probe_app.js | cut -d' ' -f1)
    echo "PORT $p app.js $out sha=$h"
  fi
done

echo "=== 本地新文件 sha ==="
sha256sum /vol1/1003/nas-app-test/nas_app/web/app.js /vol1/1003/nas-app-test/nas_app/web/style.css 2>/dev/null

echo "=== 容器相关（只读） ==="
docker ps --format '{{.ID}} {{.Names}} {{.Image}} {{.Ports}}' 2>&1 | head -10
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
            "NAS 探测二", f"{USER}@{HOST} 的 SSH 密码（仅内存）", show="*", parent=root
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
