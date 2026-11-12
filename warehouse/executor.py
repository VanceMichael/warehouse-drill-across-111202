"""跨事实执行：每个事实独立聚合，再按合法共同粒度对齐。

顺序固定为 事实内聚合（集合去重/十进制求和）→ 分组结果 → 共同维度对齐
→ 比率在合并后的分子分母上计算。不把每日 distinct 相加，也不把不同粒度的
事实直接拼成宽表来放大预算。
"""

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from .declaration import DECLARED, fingerprint
from .planner import PLANNER_VERSION, QueryRejected, build_plan


def _iso_week(value):
    try:
        return date.fromisoformat(value).strftime('%G-W%V')
    except (TypeError, ValueError):
        return None


class _MoneySum:
    """SQLite 自定义聚合：金额用 Decimal 逐行累加，返回十进制文本。"""

    def __init__(self):
        self.total = Decimal(0)

    def step(self, value):
        if value is None:
            return
        try:
            amount = Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError('金额不是合法的十进制文本')
        if not amount.is_finite():
            raise ValueError('金额必须是有限数')
        self.total += amount

    def finalize(self):
        return format(self.total, 'f')


def register_functions(connection):
    """SQLite 侧只用注册过的函数，不接受调用方拼接表达式。"""
    connection.create_function('iso_week', 1, _iso_week, deterministic=True)
    connection.create_aggregate('money_sum', 1, _MoneySum)


def _expressions(plan):
    expressions = []
    for name in plan.common_grain:
        dimension = DECLARED.dimension(name)
        source = f'iso_week("{dimension.column}")' if dimension.derived else f'"{dimension.column}"'
        expressions.append((name, source))
    return expressions


def _where(plan, fact):
    clauses = []
    parameters = []
    for name in ('campaign', 'channel'):
        if name in plan.filters and name in fact.grain:
            clauses.append(f'"{name}" = ?')
            parameters.append(plan.filters[name])
    return clauses, parameters


def _leg(plan, fact):
    """单一事实的聚合查询；分区范围与筛选值都以参数绑定。"""
    expressions = _expressions(plan)
    selects = [f'{source} AS "{name}"' for name, source in expressions]
    for name in plan.leaves[fact.name]:
        measure = DECLARED.measure(name)
        if measure.kind == 'count':
            selects.append(f'COUNT(*) AS "{name}"')
        elif measure.kind == 'distinct':
            selects.append(f'COUNT(DISTINCT "{measure.column}") AS "{name}"')
        elif measure.kind == 'sum':
            selects.append(f'money_sum("{measure.column}") AS "{name}"')
        else:
            raise QueryRejected('declaration_invalid', f'指标 {name} 不是可聚合的叶子指标')
    clauses, parameters = _where(plan, fact)
    clauses.append(f'"{fact.partition_key}" BETWEEN ? AND ?')
    parameters.extend([plan.start, plan.end])
    where = ' WHERE ' + ' AND '.join(clauses)
    group = f' GROUP BY {", ".join(source for _, source in expressions)}' if expressions else ''
    sql = f'SELECT {", ".join(selects)} FROM "{fact.table}"{where}{group}'
    return sql, parameters


def aggregate_fact(connection, plan, fact_name):
    """事实粒度内的先行聚合：每个事实单独执行，绝不与其他事实联接。

    空分区（例如更正后当日无记录）在结果层按“无行”处理，不伪造零行。
    """
    fact = DECLARED.fact(fact_name)
    sql, parameters = _leg(plan, fact)
    groups = {}
    for record in connection.execute(sql, parameters).fetchall():
        key = tuple(record[name] for name in plan.common_grain)
        entry = {}
        for name in plan.leaves[fact_name]:
            if DECLARED.measure(name).kind == 'sum':
                entry[name] = Decimal(record[name] or '0')
            else:
                entry[name] = record[name]
        groups[key] = entry
    return groups


def _partition_days(connection, table, plan):
    """扫描分区以版本登记为准，包含更正后为空的日分区。"""
    from .catalog import partition_range
    return set(partition_range(connection, table, plan.start, plan.end))


def _scanned(connection, fact, plan, versions):
    days = sorted(_partition_days(connection, fact.table, plan))
    partitions = []
    for day in days:
        version = versions.get(day)
        partitions.append({
            'partition': day,
            'digest': version['digest'] if version else None,
            'generation': version['generation'] if version else None,
        })
    return partitions


def _versions(connection, table, plan):
    from .catalog import current_versions
    return current_versions(connection, table, _partition_days(connection, table, plan))


def _identity(measure):
    return Decimal(0) if measure.kind == 'sum' else 0


def _value(plan, name, groups, key):
    """对齐时某侧无数据的组保留：缺失事实按该指标的加法单位参与，不丢弃整组。"""
    measure = DECLARED.measure(name)
    if measure.kind == 'ratio':
        return _value(plan, measure.numerator.name, groups, key)
    entry = groups.get(measure.fact, {}).get(key)
    if entry is None:
        return _identity(measure)
    value = entry[name]
    if value is None:
        return _identity(measure)
    if measure.kind == 'sum':
        return Decimal(value)
    return int(value)


def _present(plan, name, groups, key):
    measure = DECLARED.measure(name)
    if measure.kind == 'ratio':
        return _present(plan, measure.numerator.name, groups, key) and \
               _present(plan, measure.denominator.name, groups, key)
    fact = measure.fact
    return groups.get(fact, {}).get(key) is not None


def _render(plan, name, groups, key):
    measure = DECLARED.measure(name)
    if measure.kind != 'ratio':
        value = _value(plan, name, groups, key)
        method = {'count': 'count_rows', 'distinct': 'set_union_then_count', 'sum': 'decimal_sum'}[measure.kind]
        reason = None if _present(plan, name, groups, key) else 'no_rows'
        if measure.kind == 'sum':
            return {'value': format(value.quantize(Decimal('0.01')), 'f'), 'reason': reason, 'method': method}
        return {'value': value, 'reason': reason, 'method': method}
    numerator = _value(plan, measure.numerator.name, groups, key)
    denominator = _value(plan, measure.denominator.name, groups, key)
    if denominator == 0:
        return {'value': None, 'reason': 'zero_denominator', 'method': 'ratio_of_composed_parts',
                'numerator': int(numerator), 'denominator': int(denominator)}
    reason = None if _present(plan, name, groups, key) else 'no_rows'
    return {'value': float(numerator / denominator), 'reason': reason, 'method': 'ratio_of_composed_parts',
            'numerator': int(numerator), 'denominator': int(denominator)}


def _cache_key(plan, versions, declaration_fingerprint):
    payload = {
        'planner': PLANNER_VERSION,
        'mode': plan.mode,
        'fingerprint': declaration_fingerprint,
        'dimensions': list(plan.dimensions),
        'measures': list(plan.measures),
        'period': [plan.start, plan.end],
        'filters': dict(plan.filters),
        'facts': [{'fact': fact, 'leaves': list(plan.leaves[fact])} for fact in sorted(plan.facts)],
        'versions': {f'{table}:{day}': value['digest'] for (table, day), value in sorted(versions.items())},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _fingerprint_versions(versions):
    payload = {f'{table}:{day}': value['digest'] for (table, day), value in sorted(versions.items())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _explain(plan, scanned, cache_key, cache_status, declaration_fingerprint, versions):
    merges = []
    for name in plan.measures:
        measure = DECLARED.measure(name)
        if measure.kind == 'ratio':
            merges.append({
                'measure': name,
                'method': 'ratio_of_composed_parts',
                'numerator': measure.numerator.name,
                'denominator': measure.denominator.name,
                'rule': '先分别按事实聚合分子与分母，在共同粒度上对齐后做除法；不使用平均比率',
            })
        elif measure.kind == 'distinct':
            merges.append({
                'measure': name,
                'method': 'set_union_then_count',
                'column': measure.column,
                'rule': '同一分区内按集合去重后计数；跨日只做集合并集，不把每日 distinct 相加',
            })
        elif measure.kind == 'sum':
            merges.append({'measure': name, 'method': 'decimal_sum', 'column': measure.column,
                           'rule': '按日活动粒度求和在十进制语义下进行，不复制到更细渠道'})
        else:
            merges.append({'measure': name, 'method': 'count_rows', 'rule': '在事实声明的粒度上计数'})
    scanned_detail = []
    for fact_name in plan.facts:
        fact = DECLARED.fact(fact_name)
        partitions = scanned.get(fact.table, [])
        scanned_detail.append({
            'fact': fact_name,
            'table': fact.table,
            'partition_key': fact.partition_key,
            'partitions': partitions,
            'grain': list(fact.grain),
            'leaves': list(plan.leaves[fact_name]),
        })
    return {
        'mode': plan.mode,
        'declaration_fingerprint': declaration_fingerprint,
        'common_grain': list(plan.common_grain),
        'alignment': {
            'rule': '各事实先按声明粒度独立聚合，再在共同维度上对齐；某侧无数据的组保留',
            'dimensions': list(plan.common_grain),
        },
        'merges': merges,
        'scanned': scanned_detail,
        'rejections': [
            {'measure': item.measure, 'reason': item.reason, 'message': item.message,
             'fact': item.fact, 'dimensions': list(item.dimensions)}
            for item in plan.unavailable
        ],
        'cache': {'key': cache_key, 'fingerprint': declaration_fingerprint,
                  'input_versions': _fingerprint_versions(versions), 'status': cache_status},
        'versions': {f'{table}:{day}': value['generation'] for (table, day), value in sorted(versions.items())},
    }


def _keys(plan, groups, scanned):
    """保留某侧无数据的组：把其他事实的分区日也纳入 day 维度的组集合。

    只在 day 是共同维度且维度仅含 day 时合并分区，避免伪造其他维度取值。
    """
    keys = set()
    for fact in plan.facts:
        keys.update(groups.get(fact, {}).keys())
    if plan.common_grain == ('day',):
        for fact in plan.facts:
            for partition in scanned.get(DECLARED.fact(fact).table, []):
                keys.add((partition['partition'],))
    if not plan.common_grain:
        keys.add(())
    return sorted(keys, key=lambda item: tuple('' if part is None else part for part in item))


def _shape(plan, groups, versions, scanned, declaration_fingerprint):
    unavailable_names = {item.measure: item for item in plan.unavailable}
    business = {
        'mode': plan.mode,
        'dimensions': list(plan.common_grain),
        'period': [plan.start, plan.end],
        'filters': dict(plan.filters),
        'rows': [],
        'unavailable': [
            {'measure': item.measure, 'reason': item.reason, 'message': item.message,
             'fact': item.fact, 'dimensions': list(item.dimensions)}
            for item in plan.unavailable
        ],
    }
    for key in _keys(plan, groups, scanned):
        row = {'dimensions': {name: value for name, value in zip(plan.common_grain, key)}}
        rendered = {}
        for name in plan.measures:
            item = unavailable_names.get(name)
            if item is not None:
                rendered[name] = {'value': None, 'reason': item.reason,
                                  'message': item.message, 'method': 'unavailable'}
            else:
                rendered[name] = _render(plan, name, groups, key)
        row['measures'] = rendered
        business['rows'].append(row)
    cache_key = _cache_key(plan, versions, declaration_fingerprint)
    explain = _explain(plan, scanned, cache_key, 'miss', declaration_fingerprint, versions)
    return {'business': business, 'explain': explain, 'cache_key': cache_key}


def execute(declaration, request, connection=None, mode='reject', plan=None):
    """校验、规划、快照执行。连接由调用方提供，快照保证同一发布代次。

    缓存持久化在数据库内，因此重复执行同一请求会在第二次命中。
    """
    from .catalog import cache_lookup, cache_store, snapshot, version_rows_digest

    if connection is None:
        raise QueryRejected('invalid_request', '执行需要数据库连接')
    active = plan if plan is not None else build_plan(declaration, request, mode=mode)
    declaration_fingerprint = fingerprint(declaration)
    register_functions(connection)
    with snapshot(connection):
        versions = {}
        scanned = {}
        groups = {}
        for fact_name in active.facts:
            table = DECLARED.fact(fact_name).table
            fact_versions = _versions(connection, table, active)
            for day, value in fact_versions.items():
                actual = version_rows_digest(connection, table, day)
                if value['digest'] != actual:
                    raise QueryRejected('snapshot_mismatch',
                                        f'{table} 的 {day} 分区版本与当前行内容不一致，拒绝混用发布代次',
                                        {'table': table, 'day': day,
                                         'recorded': value['digest'], 'actual': actual})
            versions.update({(table, day): value for day, value in fact_versions.items()})
            scanned[table] = _scanned(connection, DECLARED.fact(fact_name), active, fact_versions)
            groups[fact_name] = aggregate_fact(connection, active, fact_name)
        key = _cache_key(active, versions, declaration_fingerprint)
        cached = cache_lookup(connection, key)
        if cached is not None:
            cached_explain = dict(cached['explain'])
            cached_explain['cache'] = dict(cached_explain['cache'], status='hit')
            result = {'business': cached['business'], 'explain': cached_explain, 'cache_key': key}
        else:
            result = _shape(active, groups, versions, scanned, declaration_fingerprint)
        payload = {'business': result['business'], 'explain': result['explain']}
    if cached is None:
        from .catalog import cache_prune
        cache_store(connection, key, payload, declaration_fingerprint,
                    {f'{table}:{day}': value['digest'] for (table, day), value in versions.items()})
        cache_prune(connection)
    return result


def interpret(declaration, request, mode='reject'):
    """只做声明校验与静态规划，不读取数据；用于解释与拒绝原因。"""
    plan = build_plan(declaration, request, mode=mode)
    return plan, _explain(plan, {}, None, 'not_executed', fingerprint(declaration), {})
