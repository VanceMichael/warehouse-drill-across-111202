"""事实、维度与指标的声明，以及声明自校验。

声明是查询规划器的唯一输入来源：调用方给出的字段必须命中这里注册的名字，
规划器不会把调用方字符串拼进 SQL。
"""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class DeclarationError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__('事实声明校验失败: ' + '; '.join(self.errors))


@dataclass(frozen=True)
class FactDecl:
    name: str
    table: str
    grain: tuple
    columns: tuple
    measures: tuple
    partition_key: str

    @property
    def dimensions(self):
        return self.grain


@dataclass(frozen=True)
class MeasureDecl:
    name: str
    fact: str
    kind: str
    column: str = None
    numerator: 'MeasureDecl' = None
    denominator: 'MeasureDecl' = None


@dataclass(frozen=True)
class DimDecl:
    name: str
    column: str
    derived: bool = False


@dataclass(frozen=True)
class Declaration:
    facts: tuple
    dimensions: tuple
    measures: tuple
    day_columns: dict

    def dimension(self, name):
        for item in self.dimensions:
            if item.name == name:
                return item
        return None

    def measure(self, name):
        for item in self.measures:
            if item.name == name:
                return item
        return None

    def fact(self, name):
        for item in self.facts:
            if item.name == name:
                return item
        return None


FACTS = tuple(
    FactDecl(
        name=name,
        table=name,
        grain=grain,
        columns=columns,
        measures=measures,
        partition_key='day',
    )
    for name, grain, columns, measures in (
        ('impressions', ('day', 'campaign', 'channel'), ('event', 'day', 'campaign', 'channel', 'viewer'),
         ('impressions', 'unique_viewers')),
        ('clicks', ('day', 'campaign'), ('event', 'day', 'campaign', 'viewer'),
         ('clicks', 'unique_clickers')),
        ('budgets', ('day', 'campaign'), ('budget_key', 'day', 'campaign', 'amount'),
         ('budget', 'days_with_budget')),
    )
)

GRAIN_DIMENSIONS = ('day', 'campaign', 'channel')
DIMENSIONS = (
    DimDecl('day', 'day'),
    DimDecl('campaign', 'campaign'),
    DimDecl('channel', 'channel'),
    DimDecl('week', 'day', derived=True),
)

MEASURES = (
    MeasureDecl('impressions', 'impressions', 'count'),
    MeasureDecl('clicks', 'clicks', 'count'),
    MeasureDecl('unique_viewers', 'impressions', 'distinct', column='viewer'),
    MeasureDecl('unique_clickers', 'clicks', 'distinct', column='viewer'),
    MeasureDecl('days_with_budget', 'budgets', 'distinct', column='day'),
    MeasureDecl('budget', 'budgets', 'sum', column='amount'),
    MeasureDecl('ctr', None, 'ratio',
                numerator=MeasureDecl('clicks', 'clicks', 'count'),
                denominator=MeasureDecl('impressions', 'impressions', 'count')),
)

DECLARED = Declaration(
    facts=FACTS,
    dimensions=DIMENSIONS,
    measures=MEASURES,
    day_columns={'day': 'day', 'week': 'day'},
)


def _measure_tree(measure, ident, needs_columns, errors, fact_name):
    """把指标展开成可计算的叶子组合。比率可跨事实，按共同粒度对齐后再组合。"""
    if measure.kind in ('count', 'sum', 'distinct'):
        fact = ident.fact(fact_name) if fact_name else None
        if fact is None:
            errors.append(f'指标 {measure.name} 引用了未注册的事实 {measure.fact}')
            return
        if measure.fact != fact.name:
            errors.append(f'指标 {measure.name} 引用了事实 {measure.fact}，不在事实 {fact.name}')
        if measure.kind in ('sum', 'distinct') and not measure.column:
            errors.append(f'指标 {measure.name} 缺少列')
        if measure.column and measure.column not in fact.columns:
            errors.append(f'指标 {measure.name} 的列 {measure.column} 不在事实 {fact.name} 的物理列中')
        needs_columns.add(measure.name)
        return
    if measure.kind != 'ratio':
        errors.append(f'指标 {measure.name} 的类型 {measure.kind} 未知')
        return
    left, right = measure.numerator, measure.denominator
    if left is None or right is None:
        errors.append(f'比率指标 {measure.name} 需要可组合的分子与分母')
        return
    if left.name == right.name:
        errors.append(f'比率指标 {measure.name} 的分子与分母不能是同一个指标')
    for part in (left, right):
        registered = ident.measure(part.name)
        if registered is None:
            errors.append(f'比率指标 {measure.name} 的分量 {part.name} 未注册为指标')
            continue
        if registered.kind == 'ratio':
            errors.append(f'比率指标 {measure.name} 的分量不能是比率')
            continue
        if part.name == measure.name:
            errors.append(f'比率指标 {measure.name} 不能以自身为分量')
        _measure_tree(registered, ident, needs_columns, errors, registered.fact)
        needs_columns.add(part.name)


def validate_declared(declaration):
    """检查注册声明本身：名字、事实、粒度、维度列与比率组合规则。"""
    errors = []
    if not declaration.facts:
        errors.append('没有声明任何事实')
    names = set()
    for fact in declaration.facts:
        if not fact.name or fact.name in names:
            errors.append(f'事实名 {fact.name} 重复或为空')
        names.add(fact.name)
        grain = set(fact.grain)
        duplicates = len(fact.grain) != len(grain)
        if duplicates:
            errors.append(f'事实 {fact.name} 的粒度维度重复')
        if not grain & set(GRAIN_DIMENSIONS):
            errors.append(f'事实 {fact.name} 没有任何合法共同粒度维度')
        for dim in grain:
            if declaration.dimension(dim) is None:
                errors.append(f'事实 {fact.name} 的粒度维度 {dim} 未注册')
        if not fact.columns:
            errors.append(f'事实 {fact.name} 没有物理列')
        for column in fact.columns:
            if column in grain or column == fact.partition_key or column in _key_columns(declaration, fact.name):
                continue
            if not _used_column(declaration, fact.name, column):
                errors.append(f'事实 {fact.name} 的物理列 {column} 不属于粒度也不被指标引用')
        for measure_name in fact.measures:
            measure = declaration.measure(measure_name)
            if measure is None:
                errors.append(f'事实 {fact.name} 的指标 {measure_name} 未注册')
            elif measure.fact != fact.name:
                errors.append(f'事实 {fact.name} 的指标 {measure_name} 属于事实 {measure.fact}')
        if fact.partition_key not in grain:
            errors.append(f'事实 {fact.name} 的分区列 {fact.partition_key} 不在粒度中')
    for fact in declaration.facts:
        for measure_name in fact.measures:
            measure = declaration.measure(measure_name)
            if measure is None or measure.fact != fact.name:
                continue
            _measure_tree(measure, declaration, set(), errors, fact.name)
    products = set()
    for measure in declaration.measures:
        if measure.kind != 'ratio':
            continue
        if measure.numerator is None or measure.denominator is None:
            errors.append(f'比率指标 {measure.name} 缺少分子或分母')
            continue
        product = (measure.numerator.fact, measure.numerator.name, measure.denominator.name)
        if product in products:
            errors.append(f'比率指标 {measure.name} 的分子分母组合重复')
        products.add(product)
        for part in (measure.numerator, measure.denominator):
            registered = declaration.measure(part.name)
            if registered is None:
                errors.append(f'比率指标 {measure.name} 的分量 {part.name} 未注册为指标')
                continue
            if registered.kind == 'ratio':
                errors.append(f'比率指标 {measure.name} 的分量不能是比率')
                continue
            if registered.fact is None or declaration.fact(registered.fact) is None:
                errors.append(f'比率指标 {measure.name} 的分量 {part.name} 找不到所属事实')
                continue
            _measure_tree(registered, declaration, set(), errors, registered.fact)
    if errors:
        raise DeclarationError(errors)


def measure_fingerprint(measure):
    if measure.kind == 'ratio':
        return [measure.kind, measure_fingerprint(measure.numerator), measure_fingerprint(measure.denominator)]
    return [measure.kind, measure.column]


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def fingerprint(declaration):
    """指标定义指纹：事实、粒度、维度与指标语义的稳定摘要。"""
    payload = {
        'facts': [
            {'name': f.name, 'table': f.table, 'grain': list(f.grain),
             'columns': list(f.columns), 'measures': list(f.measures),
             'partition_key': f.partition_key}
            for f in declaration.facts
        ],
        'dimensions': [{'name': d.name, 'column': d.column, 'derived': d.derived} for d in declaration.dimensions],
        'measures': [{'name': m.name, 'fact': m.fact, 'definition': measure_fingerprint(m)} for m in declaration.measures],
        'day_columns': declaration.day_columns,
    }
    encoded = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _key_columns(declaration, fact_name):
    """主键列：来源记录的唯一标识，不参与指标但允许存在。"""
    fact = declaration.fact(fact_name)
    if fact is None:
        return frozenset()
    return frozenset(column for column in fact.columns if column.endswith('_key') or column == 'event')


def _used_column(declaration, fact_name, column):
    for measure in declaration.measures:
        for part in _parts(measure):
            if part.fact == fact_name and part.column == column:
                return True
    return False


def _parts(measure):
    if measure.kind == 'ratio':
        left = _parts(measure.numerator) if measure.numerator is not None else []
        right = _parts(measure.denominator) if measure.denominator is not None else []
        return left + right
    return [measure]
