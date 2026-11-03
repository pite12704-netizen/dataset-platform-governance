"""Refuse an implicit schema transition during ordinary start/reload.

Only inspects Docker metadata and reads migration heads in network-isolated,
secret-free disposable containers. It never queries or migrates a business DB.
"""
import argparse
import json
import re
import subprocess


class MaintenanceRequired(RuntimeError):
    pass


def check(compose_args,candidate=None,runner=subprocess.run):
    def output(args):
        result=runner(['docker',*args],capture_output=True,text=True,encoding='utf-8',errors='replace')
        if result.returncode:raise RuntimeError('Cannot verify deployment schema compatibility; no services changed')
        return result.stdout.strip()
    model=json.loads(output([*compose_args,'config','--format','json']))
    desired=candidate or model['services']['api']['image']
    if not re.fullmatch(r'[A-Za-z0-9_./:@-]{1,512}',desired) or desired.startswith('-'):
        raise RuntimeError('Invalid candidate image identity')
    previous=output([*compose_args,'ps','-a','-q','api']).split()
    if not previous:previous=output([*compose_args,'ps','-a','-q','migrate']).split()
    if not previous:
        volumes=output(['volume','ls','-q','--filter','label=com.docker.compose.project='+model['name']])
        if volumes:raise MaintenanceRequired('Existing volumes have no prior application identity; review the recovery/maintenance procedure')
        return {'compatible':True,'mode':'new-installation'}
    desired_id=output(['image','inspect',desired,'--format','{{.Id}}'])
    def head(image):
        result=output(['run','--rm','--pull','never','--network','none','--entrypoint','alembic',
            image,'-c','backend/alembic.ini','heads'])
        heads=re.findall(r'^([a-f0-9]{12}) \(head\)$',result,re.M)
        if len(heads)!=1:raise RuntimeError('Candidate image does not have one verifiable migration head')
        return heads[0]
    old_images=set()
    for container in previous:
        if not re.fullmatch(r'[a-f0-9]{12,64}',container):raise RuntimeError('Invalid application container identity')
        old_images.add(output(['container','inspect',container,'--format','{{.Image}}']))
    if old_images=={desired_id}:return {'compatible':True,'mode':'same-image'}
    target=head(desired_id)
    if any(head(image)!=target for image in old_images):
        raise MaintenanceRequired('Schema change requires a reviewed maintenance window: verify encrypted backup, drain and stop writers, then migrate and deploy matching services. Ordinary start/reload was stopped.')
    return {'compatible':True,'mode':'same-schema','schema':target}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-image')
    parser.add_argument('compose_args',nargs=argparse.REMAINDER)
    args=parser.parse_args();compose=args.compose_args
    if compose and compose[0]=='--':compose=compose[1:]
    if not compose or compose[0]!='compose':parser.error('Pass selected compose arguments after --')
    try:print(json.dumps(check(compose,args.candidate_image)))
    except (RuntimeError,KeyError,ValueError) as exc:
        print(str(exc) if isinstance(exc,RuntimeError) else 'Cannot verify selected deployment configuration')
        raise SystemExit(3)


if __name__=='__main__':main()
