# Run using `ak shell` in the existing Authentik worker.
from authentik.blueprints.v1.importer import Importer

importer = Importer.from_string(open("/tmp/platform-blueprint.yaml").read())
valid, logs = importer.validate()
print("RESULT:Blueprint valid=" + str(valid))
for log in logs:
    if not valid:
        print("RESULT:" + log.event + ": " + str(log.attributes))
assert valid
