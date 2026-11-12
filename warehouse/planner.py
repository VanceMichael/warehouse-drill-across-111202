"""跨事实查询规划：请求校验、粒度判定与拒绝策略。

调用方只提交注册维度、注册指标、周期与筛选值；规划器把它们映射成声明里的
事实与列，不接受任意字段或表达式，也不生成拼接自调用方文本的 SQL。
"""

import re
from dataclasses import dataclass, field
from datetime import date

IDENTIFIER = re.compile(r'^[a-z][a-z0-9_]*$')
REQUEST_KEYS = frozenset({'dimensions', 'measures', 'period', 'campaign', 'channel'})
FILTER_KEYS = ('campaign', 'channel')
TIME_DIMENSIONS = ('day', 'week')
PLANNER_VERSION = 1


class QueryRejected(ValueError):
    code = 'invalid_request'

    def __init__(self, code, message, detail=None):
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


class GranularityRejection(QueryRejected):
    code = 'granularity_rejected'

    def __init__(self, message, detail=None):
        super().__init__(self.code, message, detail)


@dataclass(frozen=True)
class Unavailable:
    measure: str
    reason: str
    message: str
    fact: str = None
    dimensions: tuple = ()


@dataclass(frozen=True)
class Plan:
    declaration: object
    request: dict
    mode: str
    dimensions: tuple
    measures: tuple
    period: tuple
    filters: dict
    facts: tuple
    leaves: dict
    unavailable: tuple
    common_grain: tuple

    @property
    def start(self):
        return self.period[0].isoformat()

    @property
    def end(self):
        return self.period[1].isoformat()


def _problem(field, code, message):
    return {'field': field, 'code': code, 'message': message}


def _names(value, field, problems):
    if not isinstance(value, list):
        problems.append(_problem(field, 'invalid_request', f'{field} 必须是数组'))
        return []
    names = []
    for item in value:
        if not isinstance(item, str) or not IDENTIFIER.fullmatch(item):
            problems.append(_problem(field, 'invalid_identifier', f'{field} 含非注册标识: {item!r}'))
            continue
        names.append(item)
    if len(set(names)) != len(names):
        problems.append(_problem(field, 'invalid_request', f'{field} 含重复名称'))
    return names


def _period(value, problems):
    if value is None:
        problems.append(_problem('period', 'missing_period', '查询必须给出 period 起止日期'))
        return None
    if not isinstance(value, dict) or set(value) != {'start', 'end'}:
        problems.append(_problem('period', 'invalid_request', 'period 只接受 start 与 end'))
        return None
    try:
        start = date.fromisoformat(value['start'])
        end = date.fromisoformat(value['end'])
    except (TypeError, ValueError):
        problems.append(_problem('period', 'invalid_period', 'period 日期需要 YYYY-MM-DD'))
        return None
    if start > end:
        problems.append(_problem('period', 'invalid_period', 'period 起始日期不能晚于结束日期'))
        return None
    return start, end


def _filters(request, problems):
    filters = {}
    for key in FILTER_KEYS:
        if key not in request:
            continue
        value = request[key]
        if not isinstance(value, str) or not value:
            problems.append(_problem(key, 'invalid_request', f'{key} 筛选值必须是非空字符串'))
            continue
        filters[key] = value
    return filters


def validate_request(declaration, request):
    """只校验结构与注册名；不接触数据库，因此恶意字段名不会执行任何 SQL。"""
    problems = []
    if not isinstance(request, dict):
        raise QueryRejected('invalid_request', '请求必须是对象')
    unknown = set(request) - REQUEST_KEYS
    if unknown:
        problems.append(_problem('request', 'invalid_request', f'未登记请求字段: {sorted(unknown)!r}'))
    dimensions = _names(request.get('dimensions', []), 'dimensions', problems)
    measures = _names(request.get('measures', []), 'measures', problems)
    if not measures and not any(item['field'] == 'measures' for item in problems):
        problems.append(_problem('measures', 'invalid_request', '至少需要一个指标'))
    period = _period(request.get('period'), problems)
    filters = _filters(request, problems)
    for field, code in (('dimensions', 'unknown_dimension'), ('measures', 'unknown_measure')):
        names = dimensions if field == 'dimensions' else measures
        for name in names:
            registered = declaration.dimension(name) if field == 'dimensions' else declaration.measure(name)
            if registered is None:
                problems.append(_problem(field, code, f'{field} 未注册名称: {name}'))
    if 'channel' in dimensions and 'channel' in filters:
        problems.append(_problem('channel', 'channel_conflict', 'channel 不能同时作为维度与筛选'))
    if problems:
        if any(item['code'] == 'invalid_identifier' for item in problems):
            code = 'invalid_identifier'
        elif any(item['code'] == 'unknown_dimension' for item in problems):
            code = 'unknown_dimension'
        elif any(item['code'] == 'unknown_measure' for item in problems):
            code = 'unknown_measure'
        elif any(item['code'] == 'invalid_period' for item in problems):
            code = 'invalid_period'
        elif any(item['code'] == 'missing_period' for item in problems):
            code = 'missing_period'
        elif any(item['code'] == 'channel_conflict' for item in problems):
            code = 'channel_conflict'
        else:
            code = 'invalid_request'
        raise QueryRejected(code, '查询请求校验失败', {'problems': problems})
    return dimensions, measures, period, filters


def _leaf_measures(declaration, name, collected=None):
    collected = collected if collected is not None else []
    measure = declaration.measure(name)
    if measure.kind == 'ratio':
        _leaf_measures(declaration, measure.numerator.name, collected)
        _leaf_measures(declaration, measure.denominator.name, collected)
    else:
        collected.append(measure)
    return collected


def _measure_facts(declaration, name, collected=None):
    collected = collected if collected is not None else []
    measure = declaration.measure(name)
    if measure.kind == 'ratio':
        _measure_facts(declaration, measure.numerator.name, collected)
        _measure_facts(declaration, measure.denominator.name, collected)
    else:
        collected.append(measure.fact)
    return collected


def _missing_dimensions(declaration, fact, dimensions, filters):
    missing = []
    for name in list(dimensions) + list(filters):
        column = declaration.dimension(name).column
        if column not in fact.grain and name not in missing:
            missing.append(name)
    return missing


def _unavailable_for(measure, fact, missing):
    dimensions = tuple(missing)
    if fact.name == 'budgets' and dimensions == ('channel',):
        return Unavailable(
            measure.name,
            'channel_not_declared',
            '预算事实按日活动粒度声明，没有渠道；不得把预算金额复制到各渠道',
            fact.name,
            dimensions,
        )
    return Unavailable(
        measure.name,
        'dimension_not_in_fact_grain',
        f'事实 {fact.name} 的声明粒度 {"/".join(fact.grain)} 不含 {"/".join(dimensions)}，指标无法合法对齐',
        fact.name,
        dimensions,
    )


def build_plan(declaration, request, mode='reject'):
    """把请求编译成执行计划；粒度为不合法时按 mode 拒绝或标记不可用。"""
    if mode not in ('reject', 'annotate'):
        raise QueryRejected('invalid_request', 'mode 只能是 reject 或 annotate')
    dimensions, measures, period, filters = validate_request(declaration, request)
    unavailable = []
    affected = []
    measure_unavailable = {}
    for name in measures:
        measure = declaration.measure(name)
        missing_by_fact = {}
        for fact_name in sorted(set(_measure_facts(declaration, name))):
            fact = declaration.fact(fact_name)
            missing = _missing_dimensions(declaration, fact, dimensions, filters)
            if missing:
                missing_by_fact[fact.name] = missing
        if not missing_by_fact:
            continue
        first = declaration.fact(sorted(missing_by_fact)[0])
        missing = missing_by_fact[first.name]
        entry = _unavailable_for(measure, first, missing)
        unavailable.append(entry)
        measure_unavailable[name] = entry
        affected.append({
            'measure': name,
            'facts': [
                {'fact': fact_name, 'declared_grain': list(declaration.fact(fact_name).grain),
                 'missing_dimensions': values}
                for fact_name, values in sorted(missing_by_fact.items())
            ],
            'declared_grain': list(first.grain),
            'missing_dimensions': list(missing),
            'reason': entry.reason,
        })
    if affected and mode == 'reject':
        raise GranularityRejection(
            '请求粒度与事实声明无法对齐，拒绝执行；可去掉冲突维度，或显式请求 annotate 模式',
            {
                'requested_dimensions': list(dimensions),
                'requested_filters': dict(filters),
                'conflicts': affected,
                'alignment': '各事实先按自身声明的粒度聚合，只允许在共同维度上对齐；预算等缺少渠道的事实不得复制到各渠道',
                'alternatives': ['去掉 channel 维度或筛选', '改用 annotate 模式，返回这些指标不可用及原因'],
            },
        )
    facts = []
    leaves = {}
    for name in measures:
        if name in measure_unavailable:
            continue
        for fact_name in sorted(set(_measure_facts(declaration, name))):
            if fact_name not in facts:
                facts.append(fact_name)
        leaves_by_fact = {}
        for leaf in _leaf_measures(declaration, name):
            leaves_by_fact.setdefault(leaf.fact, set()).add(leaf.name)
        for fact_name, names in leaves_by_fact.items():
            leaves.setdefault(fact_name, set()).update(names)
            if fact_name not in facts:
                facts.append(fact_name)
    return Plan(
        declaration=declaration,
        request={'dimensions': list(dimensions), 'measures': list(measures),
                 'period': {'start': period[0].isoformat(), 'end': period[1].isoformat()},
                 'filters': dict(filters)},
        mode=mode,
        dimensions=tuple(dimensions),
        measures=tuple(measures),
        period=period,
        filters=dict(filters),
        facts=tuple(facts),
        leaves={fact: tuple(sorted(names)) for fact, names in leaves.items()},
        unavailable=tuple(unavailable),
        common_grain=tuple(dimensions),
    )
