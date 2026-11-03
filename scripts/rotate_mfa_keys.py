"""Explicit MFA re-encryption. Dry-run by default; never generates/prints keys."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sqlalchemy import select
from app.config import Settings
from app.db import Database
from app.models import User
from app.security import cipher


def rotate(database,settings,apply=False):
    if not settings.mfa_encryption_keys:raise ValueError('Configure a dedicated primary MFA key first')
    encryption=cipher(settings);cursor='';checked=0
    while True:
        with database.Session() as db:
            query=select(User).where(User.id>cursor,User.mfa_secret.is_not(None)).order_by(User.id).limit(100)
            rows=db.scalars(query.with_for_update() if apply else query).all()
            if not rows:break
            for user in rows:
                raw=user.mfa_secret.encode();encryption.decrypt(raw)
                if apply:user.mfa_secret=encryption.rotate(raw).decode()
                checked+=1;cursor=user.id
            if apply:db.commit()
    return {'checked':checked,'reencrypted':checked if apply else 0,'dry_run':not apply,
            'primary_key_id':hashlib.sha256(settings.mfa_encryption_keys[0].encode()).hexdigest()[:16]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--apply',action='store_true',help='Explicitly re-encrypt account MFA values')
    args=parser.parse_args();settings=Settings(_env_file=args.env_file)
    database=Database(settings.database_url,settings)
    try:print(json.dumps(rotate(database,settings,args.apply)))
    finally:database.engine.dispose()


if __name__=='__main__':main()
