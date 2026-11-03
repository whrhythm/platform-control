"""Owner-scoped object-storage tool used by Coding, never browser credentials."""
import argparse
import json
import os
import ssl
import subprocess
import sys

import httpx

from .tool_output import redacted_output


def child_environment(profile):
    values = profile['environment']
    expected = {'S3_ENDPOINT_URL', 'S3_BUCKET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
                'AWS_DEFAULT_REGION', 'AWS_REGION', 'CODECHAT_OBJECT_PREFIX'}
    if set(values) != expected:
        raise ValueError('Invalid object profile')
    result = {key: value for key, value in os.environ.items() if not key.startswith(
        ('CODECHAT_', 'PLATFORM_', 'AGENT_', 'WORKSPACE_SERVICE_', 'WORKSPACE_TLS_'))}
    result.update(values)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('status', 'run'))
    parser.add_argument('project', nargs='?', default='')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    key = os.environ.get('CODECHAT_MODEL_KEY', '')
    if not key.startswith('sk-'):
        raise ValueError('Authenticated model session required')
    with httpx.Client(base_url=os.environ['CODECHAT_DELIVERY_URL'].rstrip('/') + '/',
        verify=ssl.create_default_context(cafile=os.environ['CODECHAT_DELIVERY_CA']),
        headers={'Authorization': 'Bearer ' + key}, timeout=30, trust_env=False) as client:
        response = client.get('object-storage' if args.action == 'status' else 'object-storage/profile',
                              params={'path': args.project.removeprefix('./')} if args.action == 'run' else {})
        if response.status_code != 200:
            print('对象存储暂未就绪，请在空间账本查看状态；未自动重试。')
            return 2
        if args.action == 'status':
            print(json.dumps(response.json(), ensure_ascii=False))
            return 0
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        if not command:
            raise ValueError('A project command is required')
        environment = child_environment(response.json())
        with subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors='replace') as child:
            for line in redacted_output(child.stdout, (environment['AWS_SECRET_ACCESS_KEY'], environment['AWS_ACCESS_KEY_ID'])):
                print(line, end='')
            return child.wait()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('对象存储工具未完成；连接凭据不会显示，请检查空间状态。')
        sys.exit(2)
