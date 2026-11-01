"""Delete only exact, marked test users and their sessions from LibreChat."""
import json
from pathlib import Path
import re
from cluster_transport import remote

payload = json.loads((Path(__file__).resolve().parents[1] / "release/live-qa-credentials.json").read_text())
run = payload["run"]
if not re.fullmatch(r"[0-9a-f]{12}", run):
    raise RuntimeError("Invalid QA run marker")
emails = [f"platform-qa-{run}-{suffix}@example.test" for suffix in ("a", "b")]
program = """
const emails = EMAILS;
for (const email of emails) {
  for (const user of db.users.find({email: email}).toArray()) {
    db.sessions.deleteMany({user: user._id});
    db.keys.deleteMany({userId: user._id});
    db.keys.deleteMany({userId: user._id.toString()});
    db.messages.deleteMany({user: user._id.toString()});
    db.conversations.deleteMany({user: user._id.toString()});
    db.users.deleteOne({_id: user._id, email: email});
  }
}
print('RESULT:Removed marked LibreChat QA users and sessions');
""".replace("EMAILS", json.dumps(emails))
output = remote(["kubectl", "-n", "librechat", "exec", "-i", "librechat-mongo-0", "--", "sh", "-c", 'mongosh --quiet --username root --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin LibreChat'], program)
for line in output.splitlines():
    if "RESULT:" in line:
        print(line[line.index("RESULT:"):])
