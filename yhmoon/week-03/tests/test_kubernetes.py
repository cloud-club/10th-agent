import subprocess
from unittest.mock import Mock, patch

import pytest

from sre_agent.kubernetes import Kubectl


def repair(**extra):
    return dict(operation='restart', namespace='demo', resource='deployments', name='web', reason='Crash observed', **extra)


@pytest.mark.parametrize('change', [
    {'namespace': 'kube-system'}, {'name': '--kubeconfig=/tmp/x'},
    {'name': 'web; touch /tmp/x'}, {'operation': 'exec'},
    {'resource': 'secrets'}, {'context': 'other'},
    {'operation': 'scale', 'replicas': 0}, {'operation': 'scale', 'replicas': 100},
    {'operation': 'scale', 'replicas': True}, {'reason': ''},
])
def test_rejects_unsafe_requests(change):
    args = repair()
    args.update(change)
    with patch('subprocess.run') as run:
        with pytest.raises(Exception):
            Kubectl('test-context', {'demo'}).call(args)
        run.assert_not_called()


def test_mutation_default_is_plan():
    with patch('subprocess.run') as run:
        result = Kubectl('test', {'demo'}).call(repair())
        assert result['status'] == 'proposed' and not result['executed']
        run.assert_not_called()


def test_denied_mutation_never_runs():
    with patch('subprocess.run') as run:
        result = Kubectl('test', {'demo'}, True, lambda _: False).call(repair())
        assert result['status'] == 'denied'
        run.assert_not_called()


def test_approved_mutation_pins_context_and_namespace():
    with patch('subprocess.run', return_value=Mock(returncode=0, stdout='restarted', stderr='')) as run:
        result = Kubectl('test', {'demo'}, True, lambda _: True).call(repair())
        assert result['status'] == 'success'
        assert run.call_args.args[0] == ['kubectl', '--context', 'test', '--namespace', 'demo', '--request-timeout=20s', 'rollout', 'restart', 'deployment/web']
        assert run.call_args.kwargs['shell'] is False


def test_timeout_is_unknown_not_success():
    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('kubectl', 45)):
        result = Kubectl('test', {'demo'}).call({'operation': 'get', 'namespace': 'demo'})
        assert result['status'] == 'timeout'
        assert 'unknown' in result['message']


def test_read_failure_and_truncation():
    with patch('subprocess.run', return_value=Mock(returncode=1, stdout='x'*20000, stderr='Forbidden')):
        result = Kubectl('test', {'demo'}).call({'operation': 'get', 'namespace': 'demo'})
        assert result['status'] == 'error' and result['truncated']
        assert len(result['stdout']) == 16000
