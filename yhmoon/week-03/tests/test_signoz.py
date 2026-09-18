import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sre_agent.signoz import discover


def test_discovery_paginates_and_drops_write_tools():
    pages = [SimpleNamespace(tools=[SimpleNamespace(name='signoz_search_logs'), SimpleNamespace(name='signoz_delete_dashboard')], nextCursor='next'),
             SimpleNamespace(tools=[SimpleNamespace(name='signoz_list_metrics')], nextCursor=None)]
    session = SimpleNamespace(list_tools=AsyncMock(side_effect=pages))
    tools=asyncio.run(discover(session))
    assert [t.name for t in tools]==['signoz_search_logs','signoz_list_metrics']
    assert session.list_tools.await_args_list[1].kwargs == {'cursor':'next'}
