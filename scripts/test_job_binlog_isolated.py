"""Native MySQL regression with a new data directory/random loopback port; never starts Docker.

Only a test-owned mysqld process is started and stopped. No owner configuration,
business DB, VHDX, datasets or model service is read. Test artefacts stay private.
"""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'backend')]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mysqld',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    if output.exists():raise RuntimeError('Use a new test directory; never overwrite existing files')
    if shutil.disk_usage(output.parent).free<2*1024**3:raise RuntimeError('Test output volume needs 2GiB free')
    output.mkdir(parents=True)
    if os.name=='nt':
        sid=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,check=True).stdout
        match=re.search(r'S-1-5-[0-9-]+',sid)
        if not match:raise RuntimeError('Cannot determine local test directory owner')
        subprocess.run(['icacls',str(output),'/inheritance:r','/grant:r','*'+match.group()+':(OI)(CI)F',
            '*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    data=output/'data';data.mkdir();temporary=output/'tmp';temporary.mkdir()
    password=secrets.token_hex(24)
    initfile=output/'root-init.sql'
    initfile.write_text("ALTER USER 'root'@'localhost' IDENTIFIED BY '"+password+"';\n"
        "CREATE USER IF NOT EXISTS 'dp_fixture'@'127.0.0.1' IDENTIFIED BY '"+password+"';\n"
        "GRANT ALL PRIVILEGES ON *.* TO 'dp_fixture'@'127.0.0.1' WITH GRANT OPTION;\n",encoding='ascii')
    with socket.socket() as reservation:reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    version=subprocess.run([str(args.mysqld),'--no-defaults','--version'],capture_output=True,text=True,encoding='utf-8',errors='replace',check=True).stdout.strip()
    with (output/'initialize.log').open('w',encoding='utf-8') as log:
        subprocess.run([str(args.mysqld),'--no-defaults','--initialize-insecure','--basedir='+str(args.mysqld.parent.parent),
            '--datadir='+str(data)],stdout=log,stderr=subprocess.STDOUT,timeout=90,check=True)
    log=(output/'server.log').open('w',encoding='utf-8')
    process=subprocess.Popen([str(args.mysqld),'--no-defaults','--console','--basedir='+str(args.mysqld.parent.parent),
        '--datadir='+str(data),'--tmpdir='+str(temporary),'--init-file='+str(initfile),
        '--bind-address=127.0.0.1','--port='+str(port),'--mysqlx=0','--skip-name-resolve',
        '--innodb-buffer-pool-size=64M','--performance-schema=OFF','--max-connections=20','--max-allowed-packet=64M',
        '--log-bin=binlog','--binlog-format=ROW','--binlog-row-image=FULL','--max-binlog-size=67108864',
        *(['--no-monitor'] if os.name=='nt' else [])],
        stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    connection=None;database=None
    try:
        import pymysql
        last_connection_error=None
        for _ in range(40):
            if process.poll() is not None:raise RuntimeError('Isolated MySQL exited during startup; inspect private server.log')
            try:
                connection=pymysql.connect(host='127.0.0.1',port=port,user='dp_fixture',password=password,
                    connect_timeout=1,read_timeout=10,write_timeout=10,autocommit=True)
                break
            except pymysql.Error as exc:last_connection_error=(type(exc).__name__,exc.args[0]);time.sleep(.3)
        if connection is None:raise RuntimeError('Isolated MySQL startup timed out: '+str(last_connection_error))
        with connection.cursor() as cursor:
            cursor.execute('SELECT @@datadir,@@log_bin,@@binlog_format,@@binlog_row_image')
            actual,enabled,format_,image=cursor.fetchone()
            if Path(actual).resolve()!=data.resolve() or not enabled or format_!='ROW' or image!='FULL':
                raise RuntimeError('Test instance identity/logging contract did not match; no DB was created')
            name='dp_payload_test_'+uuid4().hex[:12]
            cursor.execute('CREATE DATABASE `'+name+'` CHARACTER SET utf8mb4')
        from sqlalchemy import create_engine,select,text,update
        from sqlalchemy.engine import URL
        from app.db import Base,Database,now
        from app.models import User,Space,Dataset,Version,Job,JobPayload
        from app.config import Settings
        from app.job_payloads import canonical,HOT_BYTES,hydrate_job
        url=URL.create('mysql+pymysql',username='dp_fixture',password=password,host='127.0.0.1',port=port,database=name)
        cfg=Settings(_env_file=None,environment='test',database_url=url.render_as_string(hide_password=False),
            secret_key='isolated-binlog-only-01234567890123456789')
        database=Database(cfg.database_url,cfg);Base.metadata.create_all(database.engine)
        with database.Session() as session:
            user=User(name='Synthetic binlog test',username='synthetic-benchmark',password_hash='not-a-login-secret',verified=True)
            session.add(user);session.flush()
            space=Space(name='Synthetic test',owner_id=user.id);session.add(space);session.flush()
            dataset=Dataset(name='Synthetic test',owner_id=user.id,space_id=space.id);session.add(dataset);session.flush()
            version_row=Version(dataset_id=dataset.id,number=1,creator_id=user.id,source='synthetic',license='test')
            session.add(version_row);session.flush()
            identity={'space_id':space.id,'user_id':user.id,'version_id':version_row.id};session.commit()
        manifest=[{'id':f'{index:032x}','size':15000,'sha256':__import__('hashlib').sha256(str(index).encode()).hexdigest()}
                  for index in range(60000)]
        legacy_id=uuid4().hex;fixed_id=uuid4().hex
        with database.engine.begin() as transaction:
            transaction.execute(Job.__table__.insert(),{'id':legacy_id,**identity,'kind':'quality','status':'running',
                'parameters':{'input_assets':manifest},'result':{},'idempotency_key':'legacy-control'})
        with database.Session() as session:
            session.add(Job(id=fixed_id,**identity,kind='quality',status='running',
                parameters={'input_assets':manifest},result={},idempotency_key='fixed-control'));session.commit()
        def log_bytes():
            with connection.cursor() as cursor:cursor.execute('SHOW BINARY LOGS');return sum(row[1] for row in cursor.fetchall())
        def heartbeats(identifier,count):
            before=log_bytes()
            for index in range(count):
                with database.engine.begin() as transaction:
                    transaction.execute(update(Job).where(Job.id==identifier).values(
                        lease_until=now()+timedelta(minutes=5,seconds=index),progress=index%99))
            return log_bytes()-before
        legacy_bytes=heartbeats(legacy_id,10)
        fixed_bytes=heartbeats(fixed_id,100)
        with database.Session() as session:
            legacy=session.get(Job,legacy_id);legacy.status='queued';session.commit()
            fixed=session.get(Job,fixed_id)
            hot_bytes=len(canonical(fixed.parameters))+len(canonical(fixed.result))
            hydrate_job(session,fixed);assert fixed.parameters['input_assets']==manifest
            session.commit()
        compacted_legacy_bytes=heartbeats(legacy_id,100)
        assert hot_bytes<=2*HOT_BYTES
        assert fixed_bytes/100<32768 and compacted_legacy_bytes/100<32768
        assert legacy_bytes/10>100*(fixed_bytes/100)
        result={'passed':True,'isolated':True,'docker_started':False,'business_services_accessed':False,
            'mysql_version':version,'log_bin':True,'format':'ROW','row_image':'FULL','input_files':60000,
            'legacy':{'updates':10,'binlog_bytes':legacy_bytes},'fixed':{'updates':100,'binlog_bytes':fixed_bytes},
            'compacted_legacy':{'updates':100,'binlog_bytes':compacted_legacy_bytes},'fixed_hot_json_bytes':hot_bytes,
            'per_update_reduction':round((legacy_bytes/10)/(fixed_bytes/100),2)}
        (output/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result),flush=True)
    finally:
        if database is not None:database.engine.dispose()
        if connection is not None:
            try:
                with connection.cursor() as cursor:cursor.execute('SHUTDOWN')
            except Exception:pass
            connection.close()
        try:process.wait(timeout=20)
        except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=10)
        log.close()


if __name__=='__main__':main()
