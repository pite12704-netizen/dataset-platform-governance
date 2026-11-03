"""明确创建本地企业账号；交互输入密码，无固定 admin 密码，不导入示例数据。"""
import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.auth import add_user
from app.common import audit
from app.config import Settings
from app.db import Database
from app.schemas import Register
from sqlalchemy.engine import make_url
from pydantic import ValidationError

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',default='.env.business.local')
    parser.add_argument('--email')
    parser.add_argument('--username')
    parser.add_argument('--name',required=True)
    parser.add_argument('--admin',action='store_true')
    args=parser.parse_args()
    if not args.email and not args.username:
        parser.error('Specify --username or --email')
    try:
        settings=Settings(_env_file=args.env_file)
    except (ValueError,OSError):
        parser.error('Deployment configuration is invalid; check the local file without exposing credentials')
    if make_url(settings.database_url).get_backend_name()!='mysql':
        parser.error('Enterprise account provisioning requires MySQL')
    password=getpass.getpass('Password (at least 12 characters): ')
    if password!=getpass.getpass('Confirm password: '):
        parser.error('Passwords differ')
    try:
        data=Register(email=args.email,username=args.username,name=args.name,password=password)
    except ValidationError:
        parser.error('Invalid account, email, name or password format; credentials were not printed')
    database=Database(settings.database_url,settings)
    try:
        with database.Session() as db:
            user=add_user(db,data,settings,verified=True,admin=args.admin)
            audit(db,None,'operator.user_provision',user.id)
            db.commit()
        print('Account created. Administrators must enroll MFA before administrative actions.')
    finally:
        database.engine.dispose()

if __name__=='__main__':
    main()
