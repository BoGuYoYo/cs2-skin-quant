#!/usr/bin/env python3
"""用密码登录一次，把本机公钥追加到服务器的 authorized_keys，之后即可免密 SSH。

用法：
    python scripts/setup-ssh-key.py --host 192.168.1.109 --user huangzefa
    # 密码从环境变量 SSH_PASSWORD 读，或用 --password 传

只在首次需要 —— 装好之后再跑 deploy-remote.ps1 就是全自动的了。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("缺少 paramiko：pip install paramiko")
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.109")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--user", default="huangzefa")
    ap.add_argument("--password", default=os.environ.get("SSH_PASSWORD", ""))
    ap.add_argument("--pubkey", default=str(Path.home() / ".ssh" / "id_ed25519.pub"))
    args = ap.parse_args()

    if not args.password:
        print("缺少密码：设 SSH_PASSWORD 环境变量或用 --password")
        return 2

    pub = Path(args.pubkey)
    if not pub.exists():
        print(f"公钥不存在：{pub}")
        return 2
    key = pub.read_text(encoding="utf-8").strip()
    if not key.startswith(("ssh-", "ecdsa-")):
        print(f"公钥格式异常：{key[:40]}")
        return 2

    print(f"连接 {args.user}@{args.host}:{args.port} …")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=args.host, port=args.port, username=args.user,
            password=args.password, timeout=15, allow_agent=False, look_for_keys=False,
        )
    except paramiko.AuthenticationException:
        print("✗ 密码错误")
        return 1
    except Exception as e:
        print(f"✗ 连接失败：{e}")
        return 1

    # 用 grep -qF 做幂等：已存在就不重复追加
    remote = (
        "set -e; "
        "mkdir -p ~/.ssh; chmod 700 ~/.ssh; "
        f"touch ~/.ssh/authorized_keys; "
        f"if grep -qF '{key}' ~/.ssh/authorized_keys; then echo ALREADY; "
        f"else echo '{key}' >> ~/.ssh/authorized_keys; echo ADDED; fi; "
        "chmod 600 ~/.ssh/authorized_keys; "
        "echo '--- authorized_keys 行数:' $(wc -l < ~/.ssh/authorized_keys); "
        "echo '--- 目录权限:' $(stat -c '%a %n' ~/.ssh ~/.ssh/authorized_keys | tr '\\n' ' ')"
    )
    stdin, stdout, stderr = client.exec_command(remote)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    client.close()

    if out:
        for line in out.splitlines():
            print(f"  {line}")
    if err:
        print(f"  [stderr] {err}")
    if "ADDED" in out or "ALREADY" in out:
        print("\n✓ 公钥已授权，现在应该可以免密登录了")
        print(f"  验证：ssh {args.user}@{args.host} 'echo OK'")
        return 0
    print("\n⚠ 未确认结果，请手动检查")
    return 1


if __name__ == "__main__":
    sys.exit(main())
