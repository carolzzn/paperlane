"""Read-only IPP capability probe for preparing Paperlane v0.2.0.

Only Get-Printer-Attributes is used. No print/validate/update operation.
Identity, URI, UUID, serial number and location are omitted from the report.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from ipp import Printer


ATTRIBUTES = (
    'ipp-versions-supported', 'operations-supported',
    'document-format-supported', 'media-supported', 'media-ready',
    'media-col-database', 'media-col-ready', 'media-size-supported',
    'media-left-margin-supported', 'media-right-margin-supported',
    'media-top-margin-supported', 'media-bottom-margin-supported',
    'printer-resolution-supported', 'printer-resolution-default',
    'print-scaling-supported', 'print-quality-supported',
    'job-creation-attributes-supported', 'copies-supported',
)


def probe(uri):
    requested = [(0x44, 'requested-attributes', ATTRIBUTES[0])]
    requested += [(0x44, '', key) for key in ATTRIBUTES[1:]]
    response = Printer(uri, timeout=10).request(0x000b, requested)
    return {'schema_version': 1, 'operation': 'Get-Printer-Attributes',
            'ipp_status': response['status'],
            'attributes': {key: response['attributes'][key] for key in ATTRIBUTES if key in response['attributes']}}


def main():
    parser = argparse.ArgumentParser(description='只读查询相纸、边距、分辨率与格式能力，不打印；报告排除设备身份和地址。')
    parser.add_argument('uri', nargs='?', default=os.environ.get('PRINTER_URI'))
    args = parser.parse_args()
    if not args.uri:
        parser.error('请提供 ipp:// 地址，或设置 PRINTER_URI 环境变量')
    try:
        print(json.dumps(probe(args.uri), ensure_ascii=False, indent=2))
    except Exception as error:
        print('能力查询失败：' + type(error).__name__ + '。请检查设备连接、IPP 地址与证书。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
