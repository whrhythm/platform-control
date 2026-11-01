import hashlib
import json
from pathlib import Path
from cluster_transport import remote

document = json.loads(remote(["kubectl", "-n", "platform-system", "get", "configmap", "platform-ca", "-o", "json"]))
certificate = document["data"]["internal-ca.crt"]
path = Path(__file__).resolve().parents[1] / "release" / "internal-ca.crt"
path.write_text(certificate)
print("Public CA exported; SHA256=" + hashlib.sha256(certificate.encode()).hexdigest())
