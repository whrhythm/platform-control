"""Official MinIO SDK adapter; only server-generated ownership/identity enters it."""
import json
import time

from .storage_metering import MeasurementError


class ObjectOwnershipError(RuntimeError):
    pass


def missing_admin_resource(error, codes):
    """Pinned SDK exposes status/body through its exception serialization hook."""
    try:
        _, (status, body) = error.__reduce__()
        return str(status) == '404' and len(body) < 65536 and json.loads(body).get('Code') in codes
    except (ValueError, TypeError, AttributeError):
        return False


def child_policy(bucket):
    return {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['s3:GetBucketLocation', 's3:ListBucket', 's3:GetBucketVersioning',
            's3:ListBucketVersions', 's3:ListBucketMultipartUploads'], 'Resource': ['arn:aws:s3:::' + bucket]},
        {'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:GetObjectVersion',
            's3:DeleteObjectVersion', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
         'Resource': ['arn:aws:s3:::' + bucket + '/*']},
        {'Effect': 'Deny', 'Action': ['admin:*'], 'Resource': ['*']},
    ]}


def service_name(row):
    from uuid import UUID
    return 'codechat-' + UUID(row.environment_id).hex[:23]


def canonical_policy(value):
    if value.get('Version') != '2012-10-17' or set(value) - {'Version', 'Statement', 'Id'}:
        raise ObjectOwnershipError()
    result = []
    for statement in value.get('Statement', []):
        if set(statement) - {'Sid', 'Effect', 'Action', 'Resource', 'Condition'} or statement.get('Condition'):
            raise ObjectOwnershipError()
        def items(key):
            values = statement[key]
            values = [values] if isinstance(values, str) else values
            # MinIO normalizes a universal admin resource from '*' to '**'.
            return tuple(sorted('*' if key == 'Resource' and value and set(value) == {'*'} else value for value in values))
        result.append((statement['Effect'], items('Action'), items('Resource')))
    return sorted(result)


class MinioObjectProvider:
    def __init__(self, settings, *, s3=None, admin=None, child_factory=None):
        self.settings = settings
        self.child_factory = child_factory
        if s3 is None or admin is None:
            from minio import Minio, MinioAdmin
            from minio.credentials import StaticProvider
            import urllib3
            def pool():
                return urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=8), retries=False,
                    cert_reqs='CERT_REQUIRED', ca_certs=settings.workspace_service_ca if settings.object_storage_secure else None)
            s3 = Minio(settings.object_storage_endpoint, access_key=settings.object_storage_access_key,
                       secret_key=settings.object_storage_secret_key, secure=settings.object_storage_secure,
                       region='us-east-1', http_client=pool())
            admin = MinioAdmin(endpoint=settings.object_storage_endpoint,
                credentials=StaticProvider(settings.object_storage_access_key, settings.object_storage_secret_key),
                secure=settings.object_storage_secure, region='us-east-1', http_client=pool())
        self.s3, self.admin = s3, admin

    def tags(self, row):
        return {'codechat-environment': row.environment_id, 'codechat-generation': row.generation}

    def verify_bucket(self, row):
        if row.resources.get('endpoint') != self.settings.object_storage_endpoint:
            raise ObjectOwnershipError()
        if not self.s3.bucket_exists(row.bucket_name) or dict(self.s3.get_bucket_tags(row.bucket_name) or {}) != self.tags(row):
            raise ObjectOwnershipError()

    def ensure_bucket(self, row):
        if self.s3.bucket_exists(row.bucket_name):
            self.verify_bucket(row)
            return
        self.s3.make_bucket(row.bucket_name)
        from minio.commonconfig import Tags
        tags = Tags.new_bucket_tags()
        tags.update(self.tags(row))
        self.s3.set_bucket_tags(row.bucket_name, tags)
        self.verify_bucket(row)

    def verify_key(self, row, secret):
        info = json.loads(self.admin.get_service_account(row.access_key))
        policy = info.get('policy')
        if isinstance(policy, str):
            policy = json.loads(policy)
        if (info.get('parentUser') != self.settings.object_storage_access_key or
                info.get('accountStatus') not in ('on', 'enabled') or
                info.get('name') != service_name(row) or
                info.get('description') != row.generation or info.get('impliedPolicy') is not False or
                canonical_policy(policy) != canonical_policy(child_policy(row.bucket_name))):
            raise ObjectOwnershipError()
        from minio import Minio
        import urllib3
        client = self.child_factory(row.access_key, secret) if self.child_factory else Minio(self.settings.object_storage_endpoint, access_key=row.access_key, secret_key=secret,
            secure=self.settings.object_storage_secure, region='us-east-1', http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=3, read=8), retries=False, cert_reqs='CERT_REQUIRED',
                ca_certs=self.settings.workspace_service_ca if self.settings.object_storage_secure else None))
        # Prove possession of the exact persisted secret, not just an account name.
        if not client.bucket_exists(row.bucket_name):
            raise ObjectOwnershipError()

    def ensure_key(self, row, secret):
        from minio.error import MinioAdminException
        try:
            self.admin.get_service_account(row.access_key)
        except MinioAdminException as error:
            if not missing_admin_resource(error, ('XMinioAdminNoSuchServiceAccount', 'NoSuchServiceAccount', 'XMinioAdminNoSuchUser', 'XMinioInvalidIAMCredentials')):
                raise
            value = json.loads(self.admin.add_service_account(access_key=row.access_key, secret_key=secret,
                name=service_name(row), description=row.generation, policy=child_policy(row.bucket_name)))
            value = value.get('credentials', value)
            if value.get('accessKey') != row.access_key or value.get('secretKey') != secret:
                raise ObjectOwnershipError()
        self.verify_key(row, secret)

    def ensure_quota(self, row):
        self.verify_bucket(row)
        target = row.resources['budget_bytes']
        if target <= 0:
            raise ValueError('Invalid object allowance')
        quota = json.loads(self.admin.bucket_quota_get(row.bucket_name))
        if quota.get('quota') != target or quota.get('quotatype') != 'hard':
            # Only an owned, still provisioning bucket receives its initial quota.
            self.admin.bucket_quota_set(row.bucket_name, target)
            quota = json.loads(self.admin.bucket_quota_get(row.bucket_name))
        if quota.get('quota') != target or quota.get('quotatype') != 'hard':
            raise ObjectOwnershipError()

    def sample(self, row, limit):
        self.verify_bucket(row)
        quota = json.loads(self.admin.bucket_quota_get(row.bucket_name))
        budget = quota.get('quota')
        if type(budget) is not int or budget < 0 or quota.get('quotatype') != 'hard':
            raise MeasurementError('object_quota_unverified')
        used, count, deadline = 0, 0, time.monotonic() + 12
        for obj in self.s3.list_objects(row.bucket_name, recursive=True, include_version=True):
            count += 1
            if count > limit or time.monotonic() > deadline:
                raise MeasurementError('object_scan_incomplete')
            if obj.is_delete_marker:
                continue
            if type(obj.size) is not int or obj.size < 0:
                raise MeasurementError('object_size_invalid')
            used += obj.size
            if used >= 2 ** 63:
                raise MeasurementError('object_size_invalid')
        return {'used_bytes': used, 'budget_bytes': budget, 'basis': 'completed_object_versions',
                'scope_id': row.bucket_name}
