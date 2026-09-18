import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

from .agent import Audit, run_agent
from .kubernetes import Kubectl
from .signoz import connect, discover


def approve(command: str) -> bool:
    print('\n실행할 장애 조치:\n' + command)
    if not sys.stdin.isatty():
        print('대화형 터미널이 아니므로 조치를 실행하지 않습니다.')
        return False
    try:
        return input('이 명령을 실행하려면 execute 입력: ').strip() == 'execute'
    except EOFError:
        return False


async def main_async(args):
    load_dotenv(args.env_file)
    url = os.getenv('SIGNOZ_MCP_URL', 'https://signozmcp.hoydev.kr/mcp')
    key = os.getenv('SIGNOZ_API_KEY', '')
    async with connect(url, key) as session:
        if args.command == 'tools':
            for tool in await discover(session):
                print(tool.name)
            return
        if args.command == 'check':
            tools = await discover(session)
            result = await session.call_tool('signoz_list_services', {})
            print(json.dumps({'mcp': url, 'read_tool_count': len(tools),
                              'query': result.model_dump(mode='json')}, ensure_ascii=False, indent=2))
            if result.isError:
                raise RuntimeError('SigNoz query failed')
            return
        kube = Kubectl(os.getenv('KUBE_CONTEXT', ''),
                       {n.strip() for n in os.getenv('KUBE_NAMESPACES', 'default').split(',') if n.strip()},
                       args.execute, approve)
        if not kube.context:
            raise ValueError('Set KUBE_CONTEXT in .env')
        audit = Audit(args.output, (key, os.getenv('LLM_API_KEY', '')))
        try:
            text, report = await run_agent(session, kube, args.question, audit,
                os.getenv('LLM_BASE_URL', 'https://llm-big.hoydev.kr/v1'),
                os.getenv('LLM_MODEL', 'qwen'), os.getenv('LLM_API_KEY', ''), args.max_steps)
        except Exception as exc:
            audit.write('failure', error=type(exc).__name__)
            raise
        print('\n' + text + '\n\n보고서: ' + str(report) + '\n실행 기록: ' + str(audit.path))


def main():
    parser = argparse.ArgumentParser(description='SigNoz + kubectl 인프라 장애 대응 에이전트')
    parser.add_argument('--env-file', default='.env')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('tools', help='사용 가능한 SigNoz 조회 Tool 목록')
    sub.add_parser('check', help='MCP 인증 및 실제 SigNoz 조회 확인')
    run = sub.add_parser('run', help='장애 분석 및 조치')
    run.add_argument('question')
    run.add_argument('--execute', action='store_true', help='각 조치를 터미널에서 확인한 뒤 실행')
    run.add_argument('--max-steps', type=int, choices=range(1, 21), default=10)
    run.add_argument('--output', default='reports')
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print('중단했습니다.', file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        # Avoid rendering HTTP headers or nested authentication exception payloads.
        print(f'실행 실패 ({type(exc).__name__}). 설정, 연결, 실행 기록을 확인하세요.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
