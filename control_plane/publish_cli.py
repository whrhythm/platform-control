"""User-scoped delivery tool invoked by OpenHands, without cluster credentials."""
import argparse
import json
import os
import ssl
import sys

import httpx


def receipt(result):
    # A bounded machine-readable tool result ends this agent turn. The delivery
    # controller continues independently; the model must not poll/retry it.
    value = {key: result.get(key) for key in ('id', 'status', 'url', 'error_code')}
    print('CODECHAT_DELIVERY_RESULT ' + json.dumps(value, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('publish', 'list', 'start', 'stop', 'delete'))
    parser.add_argument('value', nargs='?', default='')
    args = parser.parse_args()
    key = os.environ.get('CODECHAT_MODEL_KEY', '')
    if not key.startswith('sk-'):
        raise RuntimeError('Authenticated model session required')
    context = ssl.create_default_context(cafile=os.environ['CODECHAT_DELIVERY_CA'])
    headers = {'Authorization': 'Bearer ' + key}
    if os.environ.get('CODECHAT_TASK_ID'):
        headers['X-CodeChat-Task-ID'] = os.environ['CODECHAT_TASK_ID']
    with httpx.Client(base_url=os.environ['CODECHAT_DELIVERY_URL'].rstrip('/') + '/', verify=context,
                      headers=headers, timeout=30, trust_env=False) as client:
        if args.action == 'list':
            response = client.get('apps')
        elif args.action == 'publish':
            response = client.post('apps', params={'path': args.value.removeprefix('./')})
        else:
            response = client.post(f'apps/{args.value}/{args.action}')
        if response.status_code not in (200, 202):
            print(f'平台操作未完成（HTTP {response.status_code}），请在“我的应用”查看状态。')
            receipt({'status': 'request_failed', 'error_code': 'http_' + str(response.status_code)})
            return 2
        result = response.json()
        if args.action == 'list':
            for app in result['apps']:
                print(app['id'], app['project_path'], app['status'], app.get('url') or '')
            return 0
        identifier = result['id']
        receipt(result)
        print('应用ID：' + identifier)
        print('应用状态：' + result['status'])
        if result['status'] == 'failed':
            print('发布失败：' + (result.get('error_code') or 'unknown') + '。本轮禁止自动重试或修改源码来绕过平台错误。')
            return 2
        if result.get('url') and result['status'] == 'running':
            print('访问地址：' + result['url'])
        else:
            print('任务已提交，后台独立执行。现在结束本轮编程对话；不要循环查询或自动重复发布。提交不表示构建或部署成功。')
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('平台发布工具暂时不可用。任务可能已提交，请先从应用面板检查，避免重复操作。')
        receipt({'status': 'request_unknown'})
        sys.exit(2)
