# Managed logical storage accounting

The account covers five distinct components: personal files/caches, managed
PostgreSQL databases, private object storage, the owner's Harbor project, and
additional application volumes. It is a logical account, not node disk usage.
Infrastructure replicas, backups, WAL, shared base images, temporary build layers
and incomplete uploads are outside this scope. Existing file rights are retained.
Category limits apply separately; they are not interchangeable storage credits.

Harbor uses the verified project's native quota counter, never the sum of image
sizes. The project numeric ID and private owner binding must match. A provider
counter is not a measurement of physical registry disk or garbage-collection
overhead. Its native finite limit is displayed as its category allowance.

MinIO allocates one private bucket per personal environment, automatically after
the existing workspace is ready. Generation tags seal ownership. A non-root
provisioner creates a restricted child access key and a 256 MiB test bucket quota;
root credentials are used only in the operator bootstrap. Application/agent
profiles receive only that child's credentials. Object payload accounting sums
completed object versions, with bounded scans; incomplete multipart uploads are
temporary resources and excluded. Application stop/failure/deletion retains
object data. No destructive object/bucket lifecycle action is added in this phase.

The existing applications do not allocate additional PVCs. A complete, verified
PVC inventory containing only the recorded personal PVC establishes zero extra
application-volume allocation. Any unknown extra PVC, truncated inventory, missing
personal PVC or UID mismatch makes this component unknown, not zero.

`GET /internal/v1/workspace/capacity` retains its component list and adds source,
scope, budget and service-state explanations. Only fresh valid samples enter the
measured subtotal. Complete is true only when all five managed components are
fresh and known. A failed provider read retains the previous sample as stale;
absence of a sample remains unknown. Reading the account never calls Harbor or
MinIO with administrative credentials; background controllers gather samples.

`GET /internal/v1/workspace/object-storage` returns public bucket lifecycle data.
The agent equivalent `/agent/v1/object-storage` is owner-key authenticated;
`GET /agent/v1/object-storage/profile?path=` delivers a no-store child profile
only for the owner's existing project directory. No browser profile route exists.
The next publication of that project receives its connection Secret and prefix;
existing running applications are not restarted just to add object storage.

Real money, object/image allowance upgrades, a single interchangeable hard byte
quota and additional application-volume provisioning remain outside this phase.
