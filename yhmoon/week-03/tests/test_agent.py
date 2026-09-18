import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sre_agent.agent import Audit, run_agent
from sre_agent.kubernetes import Kubectl
from sre_agent.signoz import READ_TOOLS


def test_only_observation_tools_exposed():
    assert 'signoz_search_logs' in READ_TOOLS
    assert not any(x in name for name in READ_TOOLS for x in ('create', 'update', 'delete', 'execute'))


def test_audit_redacts_secret(tmp_path):
    audit = Audit(str(tmp_path), ('secret-value',))
    audit.write('test', value='secret-value')
    assert 'secret-value' not in audit.path.read_text()
    json.loads(audit.path.read_text())
    assert audit.path.stat().st_mode & 0o777 == 0o600


def test_loop_returns_tool_result_and_final_report(tmp_path):
    tool = SimpleNamespace(name='signoz_list_services', description='services', inputSchema={'type':'object'})
    session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(structuredContent=None, model_dump=lambda **_: {'data': ['web']})))
    responses = [
        {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'1','type':'function','function':{'name':'signoz_list_services','arguments':'{}'}}]},'finish_reason':'tool_calls'}]},
        {'choices':[{'message':{'role':'assistant','content':'관측 완료'},'finish_reason':'stop'}]},
    ]
    class Response:
        def __init__(self, body): self.body=body
        def raise_for_status(self): pass
        def json(self): return self.body
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.side_effect = [Response(x) for x in responses]
    with patch('sre_agent.agent.discover', AsyncMock(return_value=[tool])), patch('sre_agent.agent.httpx.AsyncClient', return_value=client):
        answer, report = asyncio.run(run_agent(session, Kubectl('test',{'demo'}), '점검', Audit(str(tmp_path)), 'https://llm.example/v1', 'test'))
    assert answer == report.read_text() == '관측 완료'
    session.call_tool.assert_awaited_once_with('signoz_list_services', {'searchContext':'점검'})
    messages=client.post.call_args.kwargs['json']['messages']
    assert any(m['role']=='tool' and 'web' in m['content'] for m in messages)


def test_audit_sensitive_fields_remain_valid_json(tmp_path):
    audit = Audit(str(tmp_path))
    audit.write('test', password='abc', nested={'authorization':'Bearer xyz'}, message='token=abc')
    record=json.loads(audit.path.read_text())
    assert record['password']=='[REDACTED]'
    assert record['nested']['authorization']=='[REDACTED]'
    assert record['message']=='token=[REDACTED]'
