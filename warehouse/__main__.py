import argparse
import json
import sys
from pathlib import Path

from .catalog import connect, load_file, replace_fact
from .declaration import DECLARED
from .domain import overview
from .executor import interpret
from .planner import QueryRejected
from .query import query
from .store import ObjectStore


def _request(value):
    try:
        request = json.loads(value)
    except json.JSONDecodeError as error:
        raise SystemExit(f'查询请求不是合法 JSON: {error}')
    return request


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地数据集合装载、跨事实查询与导出')
    parser.add_argument('command', choices=['import', 'correct', 'inspect', 'explain', 'query', 'export'])
    parser.add_argument('--db', required=True)
    parser.add_argument('--input')
    parser.add_argument('--batch')
    parser.add_argument('--objects')
    parser.add_argument('--request', help='查询请求 JSON；或 - 从标准输入读取')
    parser.add_argument('--fact', help='更正时的事实名')
    parser.add_argument('--day', help='更正时的日分区')
    parser.add_argument('--mode', choices=['reject', 'annotate'], default='reject')
    args = parser.parse_args(argv)
    if args.command in ('import', 'correct') and (not args.input or not args.batch):
        parser.error('导入与更正需要 --input 和 --batch')
    if args.command == 'correct' and (not args.fact or not args.day):
        parser.error('更正需要 --fact 和 --day')
    if args.command in ('query',) and not args.request:
        parser.error('查询需要 --request')
    if args.command == 'explain' and not args.request:
        parser.error('解释需要 --request')
    if args.command == 'export' and not args.objects:
        parser.error('导出需要 --objects')
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(args.db)
    try:
        if args.command == 'import':
            result = {'changed': load_file(connection, args.input, args.batch)}
        elif args.command == 'correct':
            rows = json.loads(Path(args.input).read_text(encoding='utf-8'))
            result = replace_fact(connection, args.fact, args.day, rows, args.batch)
        elif args.command == 'query':
            raw = sys.stdin.read() if args.request == '-' else args.request
            try:
                result = query(connection, json.loads(raw), mode=args.mode)
            except QueryRejected as rejection:
                result = {'accepted': False, 'code': rejection.code,
                          'message': rejection.message, 'detail': rejection.detail}
        elif args.command == 'explain':
            raw = sys.stdin.read() if args.request == '-' else args.request
            try:
                plan, explain = interpret(DECLARED, json.loads(raw), mode=args.mode)
                result = {'accepted': True, 'plan': explain}
            except QueryRejected as rejection:
                result = {'accepted': False, 'code': rejection.code,
                          'message': rejection.message, 'detail': rejection.detail}
        else:
            result = overview(connection)
            if args.command == 'export':
                records = result if isinstance(result, list) else [result]
                result = {'object': ObjectStore(args.objects).put(records), 'rows': len(records)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
