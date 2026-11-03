"""为正在运行的旧网页注入预览背压；仅重建 Web 容器，不启动依赖或执行迁移。"""
import hashlib
import json
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    result = subprocess.run(['docker', *args], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise RuntimeError('Docker action failed: ' + args[0])
    return result.stdout.strip()


def identity(name):
    identifier, image, started = docker('inspect', '--format', '{{.Id}}|{{.Image}}|{{.State.StartedAt}}', name).split('|')
    return {'id': identifier, 'image': image, 'started': started}


def pin_web(image):
    path = ROOT/'deployment/compose.env.local'
    lines = [line for line in path.read_text(encoding='utf-8-sig').splitlines() if not line.startswith('DP_WEB_IMAGE=')]
    lines.append('DP_WEB_IMAGE=' + image)
    temporary = path.with_name('compose.env.local.preview.tmp')
    try:
        temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    if (ROOT/'var/run/active-deployment.json').exists():
        raise RuntimeError('The active fresh deployment already includes preview backpressure; use its regular release workflow')
    run = uuid.uuid4().hex[:10]
    output = ROOT/'var/operations'/('preview-hotfix-' + run)
    output.mkdir(parents=True)
    old = identity('dataset-platform-web-1')
    protected = {name: identity('dataset-platform-' + name + '-1')
                 for name in ('api', 'worker', 'vision-worker', 'beat', 'mysql', 'minio', 'redis')}
    base = 'dataset-platform-web:preview-base-' + run
    target = 'dataset-platform-web:preview-hotfix-' + run
    docker('tag', old['image'], base)
    docker('cp', 'dataset-platform-web-1:/srv/index.html', str(output/'index.html'))
    docker('cp', 'dataset-platform-web-1:/data/caddy/pki/authorities/local/root.crt', str(output/'root-ca.crt'))
    javascript = "import {build} from 'esbuild'; await build({entryPoints:[process.argv[1]],outfile:process.argv[2],bundle:true,minify:true,format:'iife',platform:'browser',target:'es2022'});"
    build = subprocess.run(['node', '--input-type=module', '-e', javascript,
        str(ROOT/'frontend/src/preview-bootstrap.ts'), str(output/'preview.js')], cwd=ROOT/'frontend')
    if build.returncode:
        raise RuntimeError('Preview bootstrap build failed')
    payload = (output/'preview.js').read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    script = 'preview-backpressure-' + digest[:12] + '.js'
    (output/'preview.js').rename(output/script)
    html = (output/'index.html').read_text(encoding='utf-8')
    html = re.sub(r'<script\b[^>]*\bdata-preview-backpressure="1"[^>]*>\s*</script>\s*', '', html)
    if html.count('<head>') != 1:
        raise RuntimeError('Unexpected static HTML; refusing to modify it')
    html = html.replace('<head>', '<head>\n<script data-preview-backpressure="1" src="/' + script + '"></script>', 1)
    (output/'index.html').write_text(html, encoding='utf-8')
    (output/'Dockerfile').write_text(f'FROM {base}\nCOPY index.html /srv/index.html\nCOPY {script} /srv/{script}\n', encoding='utf-8')
    docker('build', '--pull=false', '-t', target, str(output))
    override = output/'web.json'
    override.write_text(json.dumps({'services': {'web': {'image': target}}}), encoding='utf-8')
    args = ['compose', '--env-file', 'deployment/compose.env.local', '-f', 'deployment/compose.yml',
            '-f', 'deployment/compose.local.yml', '-f', 'deployment/compose.desktop.local', '-f', str(override)]
    context = ssl.create_default_context(cafile=str(output/'root-ca.crt'))
    try:
        docker(*args, 'up', '-d', '--no-deps', '--no-build', 'web')
        with httpx.Client(verify=context, trust_env=False, timeout=5) as client:
            for attempt in range(15):
                try:
                    page = client.get('https://localhost:8443/')
                    js = client.get('https://localhost:8443/' + script)
                    ready = client.get('https://localhost:8443/health/ready')
                    if page.status_code == js.status_code == ready.status_code == 200 and script in page.text and hashlib.sha256(js.content).hexdigest() == digest:
                        break
                except httpx.HTTPError:
                    pass
                if attempt == 14:
                    raise RuntimeError('Patched website did not pass health/content checks')
                time.sleep(1)
        for name, before in protected.items():
            if identity('dataset-platform-' + name + '-1') != before:
                raise RuntimeError('Non-Web service changed; verification requires review')
        patched = identity('dataset-platform-web-1')
        pin_web(patched['image'])
        (output/'verification.json').write_text(json.dumps({'before': old, 'after': patched,
            'bootstrap_sha256': digest, 'protected_services_unchanged': True, 'health_http': 200,
            'database_migration_executed': False}, indent=2), encoding='utf-8')
        print('Preview Web patch deployed; all non-Web service identities and start times unchanged.')
    except Exception:
        override.write_text(json.dumps({'services': {'web': {'image': old['image']}}}), encoding='utf-8')
        docker(*args, 'up', '-d', '--no-deps', '--no-build', 'web')
        pin_web(old['image'])
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Preview patch failed: ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
