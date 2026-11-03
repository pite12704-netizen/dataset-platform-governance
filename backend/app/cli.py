"""【学习标注：命令行工具】初始化管理员或执行运维命令，不承载在线业务接口。"""
import argparse
import getpass
import os
from pathlib import Path

from sqlalchemy import select

from .auth import add_user
from .config import get_settings
from .db import Database
from .models import Identity, User
from .schemas import Register


def main():
    parser = argparse.ArgumentParser(description="数据集平台运维工具")
    parser.add_argument("command", choices=["create-user", "link-identity", "run-pending"])
    parser.add_argument("--email")
    parser.add_argument('--username')
    parser.add_argument("--name", default="管理员")
    parser.add_argument("--admin", action="store_true")
    parser.add_argument("--issuer")
    parser.add_argument("--subject")
    args = parser.parse_args()
    settings = get_settings()
    database = Database(settings.database_url)
    with database.Session() as db:
        if args.command == "create-user":
            password = os.environ.get("DP_BOOTSTRAP_PASSWORD") or getpass.getpass("初始密码（至少12位）: ")
            user = add_user(db, Register(email=args.email, username=args.username, name=args.name, password=password), settings,
                            verified=True, admin=args.admin)
            db.commit()
            print('User created; MFA enrollment required on first privileged login.')
        elif args.command == "link-identity":
            account = args.username or args.email
            if not account:
                parser.error('需要 --username 或 --email')
            column = User.username if args.username else User.email
            user = db.scalar(select(User).where(column == account.lower()))
            if not user or not args.issuer or not args.subject:
                parser.error("需要有效账号、issuer、subject")
            db.add(Identity(user_id=user.id, issuer=args.issuer, subject=args.subject))
            db.commit()
            print("Identity linked.")
        elif args.command == "run-pending":
            from .models import Job
            from .storage import make_storage
            from .tasks import run_job
            ids = list(db.scalars(select(Job.id).where(Job.status == "queued")))
            for identifier in ids:
                run_job(identifier, database, make_storage(settings), settings)
            from .auth import deliver_mail
            deliver_mail(db, settings)
            print(f"Processed {len(ids)} queued tasks.")


if __name__ == "__main__":
    main()
