"""Structured kubectl calls: pinned context, namespace allowlist, no shell."""
import re
import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable

READ_RESOURCES = ['pods', 'deployments', 'statefulsets', 'daemonsets', 'services', 'events', 'replicasets']
OPERATIONS = ['get', 'describe', 'logs', 'rollout_status', 'restart', 'scale', 'undo']
MUTATIONS = {'restart', 'scale', 'undo'}
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'operation': {'type': 'string', 'enum': OPERATIONS},
        'namespace': {'type': 'string'},
        'resource': {'type': 'string', 'enum': READ_RESOURCES},
        'name': {'type': 'string'},
        'replicas': {'type': 'integer', 'minimum': 1, 'maximum': 10},
        'reason': {'type': 'string'},
    },
    'required': ['operation', 'namespace'],
}


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', value):
        raise ValueError('Invalid Kubernetes identifier')
    return value


@dataclass
class Kubectl:
    context: str
    namespaces: set[str]
    execute: bool = False
    approve: Callable[[str], bool] = lambda _: False

    def command(self, args: dict) -> tuple[list[str], bool]:
        import jsonschema
        jsonschema.validate(args, SCHEMA)
        if not self.context:
            raise ValueError('KUBE_CONTEXT must be explicitly configured')
        ns = identifier(args['namespace'])
        if ns not in self.namespaces:
            raise ValueError(f'Namespace not allowed: {ns}')
        op = args['operation']
        resource = args.get('resource', 'pods')
        name = identifier(args['name']) if args.get('name') else None
        cmd = ['kubectl', '--context', self.context, '--namespace', ns, '--request-timeout=20s']
        if op in {'get', 'describe'}:
            cmd += [op, resource] + ([name] if name else [])
            if op == 'get':
                cmd += ['-o', 'json', '--chunk-size=100']
        elif op == 'logs':
            if not name or resource != 'pods':
                raise ValueError('logs requires resource=pods and a pod name')
            cmd += ['logs', name, '--all-containers=true', '--tail=100', '--since=15m', '--limit-bytes=16000']
        else:
            if not name or resource != 'deployments':
                raise ValueError('Rollout/repair requires resource=deployments and a deployment name')
            target = 'deployment/' + name
            if op == 'rollout_status':
                cmd += ['rollout', 'status', target, '--timeout=30s']
            elif op == 'scale':
                if 'replicas' not in args:
                    raise ValueError('scale requires replicas')
                cmd += ['scale', target, '--replicas=' + str(args['replicas'])]
            else:
                cmd += ['rollout', op, target]
        if op in MUTATIONS and not args.get('reason', '').strip():
            raise ValueError('Repair requires an evidence-based reason')
        return cmd, op in MUTATIONS

    def call(self, args: dict) -> dict:
        cmd, mutation = self.command(args)
        shown = shlex.join(cmd)
        if mutation:
            if not self.execute:
                return {'status': 'proposed', 'executed': False, 'command': shown,
                        'reason': args['reason'], 'message': 'Observation mode; rerun with --execute for interactive approval.'}
            if not self.approve(shown + '\nReason: ' + args['reason']):
                return {'status': 'denied', 'executed': False, 'command': shown}
        try:
            p = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=45)
        except subprocess.TimeoutExpired:
            return {'status': 'timeout', 'executed': True, 'command': shown,
                    'message': 'Outcome unknown; inspect current state before any retry.'}
        output = p.stdout
        if args['operation'] == 'get' and p.returncode == 0:
            try:
                data = json.loads(output)
                def compact(item):
                    metadata = item.get('metadata', {})
                    spec = item.get('spec', {})
                    containers = spec.get('containers', spec.get('template', {}).get('spec', {}).get('containers', []))
                    summary = {k: v for k, v in item.items() if k not in {'metadata', 'spec'}}
                    summary['metadata'] = {k: metadata[k] for k in ('name', 'namespace', 'creationTimestamp', 'generation') if k in metadata}
                    summary['spec'] = {k: spec[k] for k in ('replicas', 'nodeName', 'type', 'clusterIP', 'ports') if k in spec}
                    if containers:
                        summary['containers'] = [{k: c[k] for k in ('name', 'image', 'resources') if k in c} for c in containers]
                    return summary
                data = {'items': [compact(x) for x in data['items']]} if 'items' in data else compact(data)
                output = json.dumps(data, ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return {'status': 'success' if p.returncode == 0 else 'error', 'executed': True,
                'command': shown, 'exit_code': p.returncode,
                'stdout': output[:16000], 'stderr': p.stderr[:4000],
                'truncated': len(output) > 16000 or len(p.stderr) > 4000}
