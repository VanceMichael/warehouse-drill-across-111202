import json

from . import executor
from .catalog import connect
from .declaration import DECLARED, fingerprint
from .planner import QueryRejected, build_plan


def query(connection, request, mode='reject'):
    """统一入口：校验 → 规划 → 快照执行 → 结果 + 解释。"""
    plan = build_plan(DECLARED, request, mode=mode)
    result = executor.execute(DECLARED, request, connection, mode=mode, plan=plan)
    result['plan'] = describe(plan)
    return result


def describe(plan):
    return {
        'mode': plan.mode,
        'dimensions': list(plan.dimensions),
        'measures': list(plan.measures),
        'period': {'start': plan.start, 'end': plan.end},
        'filters': dict(plan.filters),
        'facts': list(plan.facts),
        'common_grain': list(plan.common_grain),
        'leaves': {fact: list(names) for fact, names in plan.leaves.items()},
        'unavailable': [
            {'measure': item.measure, 'reason': item.reason, 'message': item.message,
             'fact': item.fact, 'dimensions': list(item.dimensions)}
            for item in plan.unavailable
        ],
        'declaration_fingerprint': fingerprint(DECLARED),
    }


def interpret_request(request, mode='reject'):
    """只做校验与规划，返回计划或规范化后的拒绝原因；不读数据。"""
    try:
        plan = build_plan(DECLARED, request, mode=mode)
    except QueryRejected as rejection:
        return {'accepted': False, 'code': rejection.code, 'message': rejection.message, 'detail': rejection.detail}
    return {'accepted': True, 'plan': describe(plan)}


def open_database(path, create_parents=True):
    from pathlib import Path
    if create_parents:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return connect(path)
