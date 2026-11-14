"""Offline-only first administrator setup for the isolated account experiment."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import tempfile
from pathlib import Path

from app.services.account_store import AccountStore, AuthorizationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在本机初始化账户试验的首位管理员")
    parser.add_argument("--db", required=True, help="独立账户数据库绝对路径")
    parser.add_argument("--username", required=True, help="管理员登录账号")
    parser.add_argument("--name", required=True, help="管理员显示姓名")
    args = parser.parse_args(argv)
    path = Path(args.db)
    if not path.is_absolute():
        print("账户数据库必须使用绝对路径", file=sys.stderr)
        return 2
    path = path.resolve()
    review = Path(os.getenv("STORE_DB_PATH") or (Path(tempfile.gettempdir()) / "agent-t-reviews.db")).resolve()
    draft = os.getenv("RULE_DRAFTS_DB_PATH", "").strip()
    forbidden = [review] + ([Path(draft).resolve()] if draft else [])
    if any(path == other or (path.exists() and other.exists() and os.path.samefile(path, other)) for other in forbidden):
        print("账户数据库必须独立于审查与草稿数据库", file=sys.stderr)
        return 2
    password = getpass.getpass("设置管理员密码（至少 12 个字符）：")
    confirmation = getpass.getpass("再次输入密码：")
    if password != confirmation:
        print("两次密码不一致", file=sys.stderr)
        return 2
    try:
        AccountStore(path).bootstrap_admin(args.username, args.name, password)
    except (AuthorizationError, ValueError, OSError) as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1
    print("本机账户试验的管理员已创建；现有合同接口尚未接入个人权限。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
