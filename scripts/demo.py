import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from warehouse.store import ObjectStore


def execute(*arguments, stdin=None, expect_ok=True):
    command = [sys.executable, '-m', 'warehouse', *arguments]
    print('$ ' + ' '.join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, input=stdin)
    if expect_ok and result.returncode != 0:
        raise RuntimeError(result.stderr)
    print(result.stdout.strip(), flush=True)
    return json.loads(result.stdout)


def main():
    with tempfile.TemporaryDirectory() as temporary:
        database = str(Path(temporary) / 'warehouse.sqlite3')
        objects = str(Path(temporary) / 'objects')
        imported = execute('import', '--db', database, '--input', str(ROOT / 'examples/source.json'), '--batch', 'daily')
        if not imported['changed']:
            raise RuntimeError('首次导入没有写入')
        repeated = execute('import', '--db', database, '--input', str(ROOT / 'examples/source.json'), '--batch', 'daily')
        if repeated['changed']:
            raise RuntimeError('重复导入发生额外写入')
        calculated = execute('inspect', '--db', database)
        cross = execute('query', '--db', database, '--request', json.dumps({
            'dimensions': ['week'],
            'measures': ['impressions', 'unique_viewers', 'clicks', 'ctr', 'budget'],
            'period': {'start': '2026-10-01', 'end': '2026-10-06'},
        }, ensure_ascii=False))
        if 'business' not in cross or 'explain' not in cross:
            raise RuntimeError('跨事实查询没有返回结果与解释')
        if not cross['explain']['scanned']:
            raise RuntimeError('解释没有列出扫描分区')

        refused = execute('query', '--db', database, '--request', json.dumps({
            'dimensions': ['channel'],
            'measures': ['impressions', 'budget'],
            'period': {'start': '2026-10-01', 'end': '2026-10-06'},
        }, ensure_ascii=False), expect_ok=False)
        if refused.get('code') != 'granularity_rejected':
            raise RuntimeError('渠道维度与预算事实没有被拒绝')

        annotated = execute('query', '--db', database, '--mode', 'annotate', '--request', json.dumps({
            'dimensions': ['channel'],
            'measures': ['impressions', 'budget'],
            'period': {'start': '2026-10-01', 'end': '2026-10-06'},
        }, ensure_ascii=False))
        if annotated['business']['unavailable'][0]['reason'] != 'channel_not_declared':
            raise RuntimeError('annotate 模式没有给出不可用原因')

        corrected = execute('correct', '--db', database, '--input', str(ROOT / 'examples/correction.json'),
                            '--fact', 'clicks', '--day', '2026-10-03', '--batch', 'fix-1')
        if not corrected['changed']:
            raise RuntimeError('更正没有写入')
        after = execute('query', '--db', database, '--request', json.dumps({
            'dimensions': ['day'],
            'measures': ['impressions', 'clicks', 'ctr'],
            'period': {'start': '2026-10-03', 'end': '2026-10-03'},
        }, ensure_ascii=False))
        row = after['business']['rows'][0]
        if row['measures']['clicks']['value'] != 1:
            raise RuntimeError('更正后没有重算点击数')
        if after['cache_key'] == cross['cache_key']:
            raise RuntimeError('更正后缓存键未随输入版本变化')

        exported = execute('export', '--db', database, '--objects', objects)
        restored = ObjectStore(objects).read(exported['object'])
        calculated = execute('inspect', '--db', database)
        expected = calculated if isinstance(calculated, list) else [calculated]
        if restored != expected:
            raise RuntimeError('导出与查询结果不符')
        print('对象校验通过，跨事实查询、拒绝、解释与更正重查均按预期工作', flush=True)


if __name__ == '__main__':
    main()
