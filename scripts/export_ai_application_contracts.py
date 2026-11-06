"""Export P1 input contracts without suggesting that their endpoints are deployed."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control_plane.ai_application_contracts import (  # noqa: E402
    AgentSnapshot, CreateAssistantInput, PublishInput, RunInput, UpdateDraftInput,
)


def main():
    contracts = (AgentSnapshot, CreateAssistantInput, UpdateDraftInput, RunInput, PublishInput)
    document = {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': 'urn:starbridge:ai-application-contracts:v1',
        '$comment': 'P1 internal contracts. Not a declaration of deployed API endpoints.',
        '$defs': {},
    }
    for contract in contracts:
        schema = contract.model_json_schema(ref_template='#/$defs/{model}')
        document['$defs'].update(schema.pop('$defs', {}))
        document['$defs'][contract.__name__] = schema
    destination = ROOT / 'contracts' / 'ai-applications.schema.json'
    destination.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
