"""One-time IAM bootstrap. Inputs are private stdin, output is metadata only."""
import json
import sys

from .object_storage_provider import canonical_policy, missing_admin_resource


def provisioner_policy():
    return {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['s3:*'], 'Resource': ['arn:aws:s3:::cc-*', 'arn:aws:s3:::cc-*/*']},
        {'Effect': 'Allow', 'Action': ['admin:CreateServiceAccount', 'admin:ListServiceAccounts',
            'admin:GetBucketQuota', 'admin:SetBucketQuota'], 'Resource': ['*']},
    ]}


def bootstrap(admin, access_key, secret_key):
    from minio.error import MinioAdminException
    policy_name = 'codechat-storage-provider-v1'
    try:
        value = json.loads(admin.policy_info(policy_name))
    except MinioAdminException as error:
        if not missing_admin_resource(error, ('XMinioAdminNoSuchPolicy', 'NoSuchPolicy')):
            raise
        admin.policy_add(policy_name, policy=provisioner_policy())
    else:
        policy = value.get('policy', value)
        if isinstance(policy, str):
            policy = json.loads(policy)
        if canonical_policy(policy) != canonical_policy(provisioner_policy()):
            raise RuntimeError('Existing storage policy differs; not overwritten')
    try:
        info = json.loads(admin.user_info(access_key))
    except MinioAdminException as error:
        if not missing_admin_resource(error, ('XMinioAdminNoSuchUser', 'NoSuchUser')):
            raise
        admin.user_add(access_key, secret_key)
        info = json.loads(admin.user_info(access_key))
    if info.get('policyName') not in ('', None, policy_name) or info.get('status') not in ('enabled', 'on'):
        raise RuntimeError('Existing storage user differs; not overwritten')
    if info.get('policyName') != policy_name:
        admin.policy_set(policy_name, user=access_key)


def main():
    from minio import Minio, MinioAdmin
    from minio.credentials import StaticProvider
    import urllib3
    payload = json.load(sys.stdin)
    pool = urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=8), retries=False)
    admin = MinioAdmin(endpoint=payload['endpoint'], credentials=StaticProvider(payload['root_access'], payload['root_secret']), secure=False, http_client=pool)
    bootstrap(admin, payload['access_key'], payload['secret_key'])
    # Credential proof only. The randomly named bucket is never created.
    client = Minio(payload['endpoint'], access_key=payload['access_key'], secret_key=payload['secret_key'], secure=False, http_client=pool)
    client.bucket_exists('cc-bootstrap-credential-proof-missing')
    print('RESULT:Non-root storage provisioner verified; root credentials not retained in runtime configuration')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('RESULT:Storage bootstrap requires inspection (' + type(error).__name__ + ')', file=sys.stderr)
        sys.exit(1)
