"""部署 nas-app 改动到飞牛 NAS。

密码通过本机弹窗输入，仅保存在进程内存中：不写入命令行、不落盘、不进任何日志。
流程：SFTP 上传暂存 → 定位 docker 镜像层（overlay2/l）→ 同 inode 覆盖写 → 回读比对 SHA256。
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

# (本地相对路径, 容器内相对路径)
FILES = [
    ("nas_app/web/app.js", "app/nas_app/web/app.js"),
    ("nas_app/web/index.html", "app/nas_app/web/index.html"),
    ("nas_app/web/style.css", "app/nas_app/web/style.css"),
    ("nas_app/toolkit.py", "app/nas_app/toolkit.py"),
]

REMOTE_SCRIPT = r"""set -u
STAGE=/tmp/nas-deploy
log(){ echo "== $*"; }

# 1) 定位 overlay 链接目录（镜像层硬链接都在 overlay2/l 下）
OV=""
for c in /vol1/@docker /vol1/docker /vol1/1000/docker /vol1/@app/docker /vol1/1000/@docker; do
  [ -d "$c/overlay2/l" ] && OV="$c/overlay2/l" && break
done
if [ -z "$OV" ]; then
  D=$(find /vol1 -maxdepth 4 -type d -name overlay2 2>/dev/null | head -1)
  [ -n "$D" ] && [ -d "$D/l" ] && OV="$D/l"
fi
log "overlay-link-dir=${OV:-NOT_FOUND}"

if [ -z "$OV" ]; then
  log "FIND_FALLBACK"
  OV=$(find /vol1 -maxdepth 8 -path '*/l/*/app/nas_app/web/app.js' 2>/dev/null | head -1 | sed 's#/app/nas_app/web/app.js##; s#/\([^/]*\)$##')
  log "overlay-link-dir-fallback=${OV:-NOT_FOUND}"
fi

deploy_one(){ # $1=暂存文件名  $2=容器内相对路径(app/nas_app/...)
  local src="$STAGE/$1" rel="$2" dst found=0
  if [ -n "${OV:-}" ]; then
    for dst in $(find "$OV" -path "*/$rel" 2>/dev/null); do
      found=1
      if { : > "$dst"; } 2>/dev/null && cat "$src" > "$dst" 2>/dev/null; then
        :
      elif chmod u+w "$dst" 2>/dev/null && { : > "$dst"; } 2>/dev/null && cat "$src" > "$dst" 2>/dev/null; then
        :
      else
        log "WRITE_FAIL $dst"
        continue
      fi
      if cmp -s "$src" "$dst"; then log "WROTE $dst"; else log "VERIFY_FAIL $dst"; fi
      log "SHA $rel $dst $(sha256sum "$dst" | cut -d' ' -f1)"
    done
  fi
  # 源码副本（/vol1/1003/nas-app-test/）
  local sc="/vol1/1003/nas-app-test/${rel#app/}"
  if [ -d "$(dirname "$sc")" ]; then
    if cat "$src" > "$sc" 2>/dev/null; then
      log "WROTE_SRC $sc"
      log "SHA_SRC $rel $sc $(sha256sum "$sc" | cut -d' ' -f1)"
    else
      log "WRITE_FAIL_SRC $sc"
    fi
  fi
  [ "$found" -eq 1 ] || log "NOT_FOUND_IN_IMAGE $rel"
}

deploy_one app.js        app/nas_app/web/app.js
deploy_one index.html    app/nas_app/web/index.html
deploy_one style.css     app/nas_app/web/style.css
deploy_one toolkit.py    app/nas_app/toolkit.py

# 2) 重启能力探测（toolkit.py 属 Python 改动，需要重启进程才生效）
if command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1; then
  log "DOCKER_OK"
else
  log "NO_DOCKER (test 用户无 docker 权限，Python 改动需在飞牛面板重启容器)"
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
            "NAS 部署",
            f"输入 {USER}@{HOST} 的 SSH 密码\n（仅保存在内存中，不写入命令行/文件）",
            show="*",
            parent=root,
        )
        root.destroy()
        if pw:
            return pw
    except Exception as exc:  # 弹窗不可用时退回终端隐藏输入
        print(f"[warn] GUI 输入不可用：{exc}", file=sys.stderr)
    import getpass

    return getpass.getpass(f"{USER}@{HOST} password: ")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    try:
        import paramiko
    except ImportError:
        print("缺少 paramiko：py -m pip install paramiko --target %TEMP%\\pyparamiko", file=sys.stderr)
        return 2

    files = [(REPO / rel, crel) for rel, crel in FILES]
    missing = [str(p) for p, _ in files if not p.is_file()]
    if missing:
        print("本地文件缺失: " + ", ".join(missing), file=sys.stderr)
        return 2

    pw = ask_password()
    if not pw:
        print("未输入密码，取消。", file=sys.stderr)
        return 1

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            HOST, username=USER, password=pw, timeout=20,
            allow_agent=False, look_for_keys=False,
        )
    except Exception as exc:
        print(f"SSH 连接失败：{exc}", file=sys.stderr)
        return 1
    finally:
        del pw

    print(f"已连接 {USER}@{HOST}")

    stdin, stdout, stderr = client.exec_command(f"mkdir -p {STAGE} && rm -f {STAGE}/* 2>/dev/null; echo OK")
    if stdout.read().decode().strip() != "OK":
        print("创建暂存目录失败：" + stderr.read().decode(), file=sys.stderr)
        return 1

    sftp = client.open_sftp()
    for local, crel in files:
        sftp.put(str(local), f"{STAGE}/{local.name}")
        print(f"上传 {local.name} ({local.stat().st_size} bytes)")
    sftp.close()

    stdin, stdout, stderr = client.exec_command("bash -s", get_pty=False)
    stdin.write(REMOTE_SCRIPT)
    stdin.channel.shutdown_write()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    print("----- 远端输出 -----")
    print(out.strip())
    if err.strip():
        print("----- 远端 stderr -----")
        print(err.strip())
    print(f"----- 远端退出码 {code} -----")
    client.close()

    # 本地/远端 SHA256 比对（远端行：== SHA <rel> <path> <hash> / == SHA_SRC <rel> <path> <hash>）
    local_hash = {crel: sha256(local) for local, crel in files}
    remote_hash: dict[str, list[str]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "==" and parts[1] in ("SHA", "SHA_SRC"):
            rel, digest = parts[2], parts[4]
            remote_hash.setdefault(rel, []).append(digest)
    print("----- SHA256 比对 -----")
    ok = True
    for _, crel in files:
        want = local_hash[crel]
        got = remote_hash.get(crel, [])
        if not got:
            print(f"  MISS   {crel}")
            ok = False
        for digest in got:
            status = "OK   " if digest == want else "DIFF "
            if digest != want:
                ok = False
            print(f"  {status} {crel}  {digest[:16]}")
    print("部署校验：" + ("全部一致 ✅" if ok else "存在不一致 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
