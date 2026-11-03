"""上传前检查 Git 文件，报告路径和行号，不输出凭据内容。"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '.venv', '.venv-cloud', 'node_modules', 'dist', 'var',
            '__pycache__', '.pytest_cache', '.ruff_cache', '.idea', '.claude', '.codex', 'secrets'}
TOKEN_PATTERN = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|'
                           r'sk-(?:proj-)?[A-Za-z0-9_-]{32,}|AKIA[A-Z0-9]{16}|'
                           r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
URL_PATTERN = re.compile(r'(?:mysql(?:\+[a-z0-9]+)?|redis|rediss|postgres(?:ql)?)://'
                         r'[^\s\"\'<>]+', re.I)


def git(*args, **kwargs):
    return subprocess.run(['git', *args], cwd=ROOT, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout


def candidates():
    """显式枚举普通文件，避免把旧仓库目录作为 Git 子模块上传。"""
    paths = []
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in EXCLUDED]
        for name in files:
            path = Path(directory, name)
            if path.is_symlink():
                continue
            paths.append(path.relative_to(ROOT).as_posix())
    if not paths:return []
    payload = b'\0'.join(path.encode() for path in paths) + b'\0'
    result = subprocess.run(['git', 'check-ignore', '--no-index', '--stdin', '-z'],
                            cwd=ROOT, input=payload, capture_output=True)
    if result.returncode not in (0, 1):
        raise RuntimeError('Cannot verify Git ignore rules.')
    ignored = set(result.stdout.decode().split('\0'))
    return sorted(path for path in paths if path not in ignored)


def local_secrets():
    """仅在内存中读取本机 secret 和环境文件，输出不会包含值。"""
    values = set()
    files = list((ROOT / 'deployment' / 'secrets').glob('*'))
    files += list(ROOT.glob('.env*')) + list((ROOT / 'deployment').glob('*.local'))
    active=ROOT/'var/run/active-deployment.json'
    if active.is_file():
        profile=json.loads(active.read_text(encoding='utf-8-sig'))
        if profile.get('secrets_dir'):files+=list(Path(profile['secrets_dir']).glob('*'))
        files += [Path(profile[k]) for k in ('host_env','service_env','compose_env') if profile.get(k)]
    for path in files:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            continue
        content = path.read_text(encoding='utf-8', errors='replace').strip()
        if path.parent.name == 'secrets' and not path.name.endswith('_user'):
            if len(content) >= 8 and '\n' not in content:
                values.add(content)
        for line in content.splitlines():
            key, sep, value = line.partition('=')
            if not sep or key.strip().startswith('#'):
                continue
            value = value.strip().strip('\"\'')
            if not key.upper().endswith('_FILE') and any(
                term in key.upper() for term in ('PASSWORD', 'SECRET', 'API_KEY', 'ACCESS_KEY')
            ):
                if len(value) >= 8:
                    values.add(value)
            if '://' in value:
                try:
                    password = urlsplit(value).password
                    if password and len(password) >= 8:
                        values.add(password)
                except ValueError:
                    pass
    return values


def forbidden_path(path):
    """Ignore rules are not a security boundary: forced/stale staged files are checked too."""
    p=Path(path);name=p.name.lower()
    if any(part in EXCLUDED for part in p.parts):return 'excluded_path'
    if name.startswith('.env') and name not in {'.env.example','.env.template'}:return 'environment_file'
    if name.endswith('.local') or name.endswith('.env.local'):return 'local_configuration'
    if name in {'test-accounts.json','development-accounts.json','platform-state.json'}:return 'account_export'
    if p.parts and p.parts[0] in {'data','datasets','models','public-release'}:return 'runtime_data'
    if name.endswith(('.pem','.key','.p12','.pfx','.db','.sqlite','.sqlite3','.sql','.dump','.bak','.gguf','.onnx','.pt','.pth','.safetensors','.vhd','.vhdx','.zip','.7z','.tar','.tar.gz')):return 'private_or_binary_artifact'
    return None


def findings_for(path, content, secrets):
    findings = []
    for number, line in enumerate(content.splitlines(), 1):
        reason = None
        if TOKEN_PATTERN.search(line):
            reason = 'credential_pattern'
        elif any(secret in line for secret in secrets):
            reason = 'matches_local_credential'
        else:
            for match in URL_PATTERN.finditer(line):
                try:
                    password = urlsplit(match.group()).password
                except ValueError:
                    continue
                placeholder = password and ('{' in password or '<' in password or password in {
                    'password', 'test', 'pass', 'example', 'change-me', 'CHANGE_ME', 'YOUR_PASSWORD'
                })
                fixture = path == 'backend/tests/test_enterprise_operations.py' and password == 'strong-pass'
                if password and not placeholder and not fixture:
                    reason = 'embedded_database_password'
                    break
        if reason:
            findings.append({'path': path, 'line': number, 'reason': reason})
    return findings


def audit(staged=False):
    secrets = local_secrets()
    paths = (git('ls-files', '--cached', '-z').decode().strip('\0').split('\0')
             if staged else candidates())
    findings = []
    gitlinks=set()
    if staged:
        for entry in git('ls-files','--stage','-z').decode().strip('\0').split('\0'):
            if entry and entry.startswith('160000 '):gitlinks.add(entry.split('\t',1)[1])
    size = 0
    for path in filter(None, paths):
        source = ROOT / path
        reason=forbidden_path(path)
        if reason or source.is_symlink() or path in gitlinks:
            findings.append({'path': path, 'reason': reason or 'gitlink_or_symlink'})
            continue
        # 审核上传的 index 内容，避免工作树干净但暂存区仍然含旧密码。
        data = git('show', ':' + path) if staged else source.read_bytes()
        size += len(data)
        if len(data) > 10 * 1024 * 1024:
            findings.append({'path': path, 'reason': 'large_file'})
        if b'\0' in data:
            findings.append({'path': path, 'reason': 'unexpected_binary'})
            continue
        findings.extend(findings_for(path, data.decode('utf-8', errors='replace'), secrets))
    return {'scope': 'staged' if staged else 'worktree', 'files': len(paths),
            'bytes': size, 'findings': findings, 'passed': not findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', action='store_true')
    parser.add_argument('--stage', action='store_true', help='Stage individually after a successful audit.')
    args = parser.parse_args()
    report = audit(args.staged)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['passed']:
        return 1
    if args.stage:
        if args.staged:
            parser.error('--stage and --staged cannot be combined')
        for path in candidates():
            git('add', '--', path)
        report = audit(staged=True)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['passed'] else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
