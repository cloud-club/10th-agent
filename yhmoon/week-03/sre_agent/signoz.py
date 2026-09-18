"""Official SigNoz MCP, exposing only explicitly selected observation tools."""
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

READ_TOOLS = {
    'signoz_list_metrics', 'signoz_query_metrics', 'signoz_get_field_keys',
    'signoz_get_field_values', 'signoz_list_alerts', 'signoz_list_alert_rules',
    'signoz_get_alert', 'signoz_get_alert_history', 'signoz_list_services',
    'signoz_get_service_top_operations', 'signoz_search_logs',
    'signoz_aggregate_logs', 'signoz_search_traces', 'signoz_aggregate_traces',
    'signoz_get_trace_details',
}


@asynccontextmanager
async def connect(url: str, key: str):
    if not key:
        raise ValueError('SIGNOZ_API_KEY is required')
    if not url.startswith(('https://', 'http://localhost:', 'http://127.0.0.1:')):
        raise ValueError('Use HTTPS or a localhost port-forward for MCP')
    async with httpx.AsyncClient(headers={'SIGNOZ-API-KEY': key}, timeout=90) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=90)) as session:
                await session.initialize()
                yield session


async def discover(session):
    result = []
    cursor = None
    while True:
        page = await session.list_tools(cursor=cursor)
        result.extend(t for t in page.tools if t.name in READ_TOOLS)
        cursor = page.nextCursor
        if not cursor:
            return result
