"""本机交互配置 QQ SMTP；隐藏输入授权码，不发送邮件、不重启或迁移业务服务。"""
import getpass
import json
import os
from pathlib import Path
import re
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def write_local(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def configure(root, email, authorization_code):
    email, authorization_code = email.strip().lower(), authorization_code.strip()
    if not re.fullmatch(r'[A-Za-z0-9_.+-]+@(?:qq|foxmail)\.com', email):
        raise ValueError('请填写完整 QQ/foxmail 邮箱地址')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', authorization_code):
        raise ValueError('授权码格式无效，请复制 QQ 邮箱生成的 SMTP 授权码')
    active = root/'var/run/active-deployment.json'
    profile = json.loads(active.read_text(encoding='utf-8')) if active.exists() else None
    if profile and profile.get('kind') != 'fresh-private':
        raise ValueError('当前实例配置无效')
    env = Path(profile['service_env']) if profile else root / 'deployment/service.env.local'
    secret = Path(profile['secrets_dir'])/'smtp_password' if profile else root/'deployment/secrets/smtp_password'
    if not env.is_file():
        raise ValueError('未找到本机 Docker 服务配置，先按 README 完成部署配置')
    settings = {'DP_SMTP_HOST': 'smtp.qq.com', 'DP_SMTP_PORT': '465', 'DP_SMTP_SSL': 'true',
        'DP_SMTP_STARTTLS': 'false', 'DP_SMTP_USERNAME': email, 'DP_MAIL_FROM': email,
        'DP_SMTP_PASSWORD_FILE': '/run/secrets/smtp_password'}
    removed = set(settings) | {'DP_SMTP_PASSWORD', 'DP_SMTP_CA_FILE'}
    lines = [line for line in env.read_text(encoding='utf-8-sig').splitlines()
             if line.partition('=')[0].strip() not in removed]
    lines += [name + '=' + value for name, value in settings.items()]
    write_local(secret, authorization_code)
    if profile:
        host = Path(profile['host_env'])
        host_lines = [line for line in host.read_text(encoding='utf-8-sig').splitlines()
                      if line.partition('=')[0].strip() not in removed]
        host_values = {**settings, 'DP_SMTP_PASSWORD_FILE':str(secret)}
        host_lines += [name+'='+value for name,value in host_values.items()]
        write_local(host,'\n'.join(host_lines)+'\n')
    write_local(env, '\n'.join(lines) + '\n')


def main():
    try:
        email = input('平台发件 QQ 邮箱（完整地址）：')
        code = getpass.getpass('QQ 邮箱 SMTP 授权码（隐藏输入，不是 QQ 登录密码）：')
        configure(ROOT, email, code)
        print('本机 SMTP 配置已保存，未发送邮件、重启或迁移服务。')
        print('enterprise.ps1 会使用当前实例的 SMTP 密钥；重启应用后验证找回密码邮件。')
        return 0
    except (ValueError, OSError):
        print('配置失败：请核对邮箱、授权码格式及本机配置文件权限。未输出凭据。')
        return 1
    except (EOFError, KeyboardInterrupt):
        print('配置已取消。')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
