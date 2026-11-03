"""OpenHands tool: use a project database without placing credentials in source."""
import argparse
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .tool_output import redacted_output


def child_environment(profile, ca_path):
    values = profile['environment']
    allowed = ('DATABASE_URL', 'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE', 'PGSSLROOTCERT')
    if set(values) != set(allowed) or values['PGSSLMODE'] != 'verify-full':
        raise ValueError('Invalid managed profile')
    connection = urlsplit(values['DATABASE_URL'])
    query = dict(parse_qsl(connection.query))
    query.update(sslmode='verify-full', sslrootcert=ca_path)
    # Platform/model credentials are not inherited by the project process.
    result = {key: value for key, value in os.environ.items()
              if not key.startswith(('CODECHAT_', 'AGENT_', 'PLATFORM_'))}
    result.update(values)
    result['PGSSLROOTCERT'] = ca_path
    result['DATABASE_URL'] = urlunsplit(connection._replace(query=urlencode(query)))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('ensure', 'list', 'run'))
    parser.add_argument('project', nargs='?', default='')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    key = os.environ.get('CODECHAT_MODEL_KEY', '')
    if not key.startswith('sk-'):
        raise RuntimeError('Authenticated model session required')
    context = ssl.create_default_context(cafile=os.environ['CODECHAT_DELIVERY_CA'])
    with httpx.Client(base_url=os.environ['CODECHAT_DELIVERY_URL'].rstrip('/') + '/', verify=context,
                      headers={'Authorization': 'Bearer ' + key}, timeout=30, trust_env=False) as client:
        if args.action == 'ensure':
            response = client.post('databases', json={'project_path': args.project.removeprefix('./')})
        elif args.action == 'list':
            response = client.get('databases')
        else:
            response = client.get('databases/profile', params={'path': args.project.removeprefix('./')})
        if response.status_code not in (200, 202):
            print('数据库操作未完成，HTTP ' + str(response.status_code) + '。请在个人空间的数据库面板查看。')
            return 2
        if args.action != 'run':
            print(json.dumps(response.json(), ensure_ascii=False))
            if args.action == 'ensure':
                print('申请已提交。只有状态 ready 才能连接；请结束本轮并等待就绪，不要自动反复申请。')
            return 0
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        if not command:
            raise ValueError('A project command is required')
        profile = response.json()
        with tempfile.TemporaryDirectory(prefix='codechat-db-') as directory:
            ca = Path(directory) / 'ca.crt'
            ca.write_text(profile['ca'])
            # Forward tool output through a bounded scrubber. Avoid accumulating
            # arbitrary project output or leaking accidental DSN diagnostics.
            env = child_environment(profile, str(ca))
            sensitive = (env['PGPASSWORD'], env['DATABASE_URL'], profile['environment']['DATABASE_URL'])
            with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, errors='replace') as child:
                for chunk in redacted_output(child.stdout, sensitive):
                    print(chunk, end='')
                return child.wait()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('数据库工具暂时不可用，请从数据库面板检查状态。连接凭据不会显示。')
        sys.exit(2)
