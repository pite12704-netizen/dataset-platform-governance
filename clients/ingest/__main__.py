"""本地目录或 MySQL + MinIO 批量接入；密码及令牌不通过命令行参数传递。"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='本地目录 / 客户 MySQL + MinIO 批量接入')
    sub = parser.add_subparsers(dest='command', required=True)
    exporter = sub.add_parser('export')
    exporter.add_argument('--config', required=True)
    exporter.add_argument('--output', required=True)
    local = sub.add_parser('prepare-local', help='扫描本地图片目录并生成清单，不复制原图')
    local.add_argument('--input-dir', required=True)
    local.add_argument('--output', required=True)
    local.add_argument('--source-id', required=True)
    local.add_argument('--source', required=True)
    local.add_argument('--license', required=True)
    uploader = sub.add_parser('upload')
    uploader.add_argument('--manifest', required=True)
    uploader.add_argument('--platform', required=True)
    uploader.add_argument('--batch', required=True)
    uploader.add_argument('--token-env', default='DP_IMPORT_TOKEN')
    uploader.add_argument('--ca-file')
    uploader.add_argument('--wait-seconds', type=int, default=600)
    uploader.add_argument('--retry-scans', action='store_true')
    uploader.add_argument('--input-dir', help='本地目录清单必须显式指定原图根目录')
    args = parser.parse_args()
    try:
        if args.command == 'prepare-local':
            from .local_source import prepare_local
            summary = prepare_local(args.input_dir, args.output, args.source_id, args.source, args.license)
            print(json.dumps(summary, ensure_ascii=False))
            return 0 if summary['complete'] else 2
        if args.command == 'export':
            from .exporter import export
            config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
            summary = export(config, args.output)
            print(json.dumps(summary, ensure_ascii=False))
            return 0 if summary['complete'] else 2
        from .uploader import PlatformClient, upload
        token = os.environ.get(args.token_env) or getpass.getpass('输入批次专用凭据（不回显）：')
        client = PlatformClient(args.platform, token, args.ca_file)
        try:
            upload(args.manifest, args.batch, client, max(0, args.wait_seconds), args.retry_scans,
                   input_dir=args.input_dir)
            return 0
        finally:
            client.close()
    except KeyboardInterrupt:
        print('已中断；重新运行可从服务器确认的进度续传。', file=sys.stderr)
        return 130
    except Exception as exc:
        from .uploader import IngestFailure
        from .exporter import SourceError
        print(str(exc) if isinstance(exc, (IngestFailure, SourceError)) else '接入失败（' + type(exc).__name__ + '）；请检查本机配置和导出错误清单。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
