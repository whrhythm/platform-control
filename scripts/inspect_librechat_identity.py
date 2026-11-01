"""Report only whether existing OIDC users have the fields needed for identity binding."""
from cluster_transport import remote

script = """db.users.find({provider:'openid'}, {openidId:1, openidIssuer:1}).forEach(u => print(JSON.stringify({subject:!!u.openidId,issuer:!!u.openidIssuer})))"""
command = 'mongosh --quiet --username root --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin LibreChat --eval "$1"'
output = remote(["kubectl", "-n", "librechat", "exec", "librechat-mongo-0", "--", "sh", "-c", command, "sh", script])
print(output.strip())
