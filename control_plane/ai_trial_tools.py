"""Reviewed public read tools for private assistant trials."""
import re
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import select

from .ai_application_contracts import ToolRevision
from .mcp_catalog import published, safe_schema
from .models import MCPServer


REFERENCE = r'^mcp:(?:deepwiki|mcp-[0-9a-f]{32}):[A-Za-z0-9_-]{1,64}:[1-9][0-9]{0,8}$'
PERSONAL_REFERENCE = r'^action:[A-Za-z0-9_-]{1,64}:[0-9a-f]{64}$'
TRIAL_REFERENCE = f'(?:{REFERENCE}|{PERSONAL_REFERENCE})'


def descriptors(db, settings):
    if not settings.mcp_gateway_url or len(settings.mcp_gateway_token) < 32:
        return
    # Query persisted review records only. The legacy programming catalog can synthesize entries.
    for row in db.scalars(select(MCPServer).where(MCPServer.state == 'published').order_by(MCPServer.id)):
        if not eligible(db, settings, row):
            continue
        for tool in row.approved:
            if approved_read(tool):
                yield describe(row, tool)


def eligible(db, settings, row):
    return bool(row and row.checked_at and row.addresses and row.config['auth'] == 'none'
                and published(db, settings, row))


def approved_read(tool):
    return (tool.get('permission') == 'read' and tool.get('fingerprint')
            and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', tool['name'])
            and safe_schema(tool['inputSchema']))


def describe(row, tool):
    return {
        'reference': f"mcp:{row.id}:{tool['name']}:{row.revision}",
        'name': tool['name'],
        'service': row.config['name'],
        'description': (tool.get('description') or row.config['description'])[:500],
        'data_notice': row.config['data_notice'],
        'input_schema': tool['inputSchema'],
    }


def find(db, settings, reference):
    if not settings.mcp_gateway_url or len(settings.mcp_gateway_token) < 32:
        raise HTTPException(409, 'ai_trials_tool_unavailable')
    _, server_id, name, revision = reference.split(':')
    row = db.get(MCPServer, server_id)
    if eligible(db, settings, row) and row.revision == int(revision):
        for tool in row.approved:
            if tool['name'] == name and approved_read(tool):
                return row, tool
    raise HTTPException(409, 'ai_trials_tool_unavailable')


def snapshot_reference(reference, project_id):
    return ToolRevision(tool_id=reference, grant_id=project_id,
                        definition_digest=sha256(reference.encode()).hexdigest())


def fixed_tool(db, settings, snapshot, project_id):
    tools = [tool for tool in snapshot.tools if tool.tool_id not in ('file_search', 'calculator')]
    if not tools:
        return None
    if (len(tools) != 1 or not re.fullmatch(TRIAL_REFERENCE, tools[0].tool_id)
            or tools[0] != snapshot_reference(tools[0].tool_id, project_id)):
        raise HTTPException(409, 'ai_trials_tool_unavailable')
    if re.fullmatch(PERSONAL_REFERENCE, tools[0].tool_id):
        # LibreChat owns the action and verifies its author and definition on each trial run.
        return None
    return find(db, settings, tools[0].tool_id)
