import hashlib
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .declaration import DECLARED
from .definition import SCHEMA

CONTROL_TABLES = (
    'CREATE TABLE IF NOT EXISTS ingest_batches (batch TEXT PRIMARY KEY, digest TEXT NOT NULL, rows INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS fact_versions (table_name TEXT NOT NULL, partition_value TEXT NOT NULL, digest TEXT NOT NULL, generation INTEGER NOT NULL, rows INTEGER NOT NULL, mutation_id INTEGER NOT NULL, PRIMARY KEY (table_name, partition_value))',
    'CREATE TABLE IF NOT EXISTS fact_version_history (event_id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, partition_value TEXT NOT NULL, digest TEXT NOT NULL, generation INTEGER NOT NULL, rows INTEGER NOT NULL, mutation_id INTEGER NOT NULL, kind TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS mutations (mutation_id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL, detail TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS mutation_partitions (mutation_id INTEGER NOT NULL, table_name TEXT NOT NULL, partition_value TEXT NOT NULL, digest TEXT NOT NULL, generation INTEGER NOT NULL, PRIMARY KEY (mutation_id, table_name, partition_value))',
    'CREATE TABLE IF NOT EXISTS query_cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL, versions TEXT NOT NULL, created TEXT NOT NULL)',
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def connect(path):
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=5000')
    for table, columns in SCHEMA.items():
        connection.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({columns})')
    for statement in CONTROL_TABLES:
        connection.execute(statement)
    return connection


@contextmanager
def transaction(connection):
    connection.execute('BEGIN IMMEDIATE')
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


@contextmanager
def snapshot(connection):
    """一致读事务：同一查询只看到同一组发布代次。"""
    connection.execute('BEGIN')
    try:
        yield connection
    finally:
        connection.rollback()


def _columns(connection, table):
    return [record['name'] for record in connection.execute(f'PRAGMA table_info("{table}")')]


def _normalize_amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('金额需要十进制文本')
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError('金额不是合法的十进制文本')
    if not amount.is_finite() or amount < 0:
        raise ValueError('金额必须是有限的非负数')
    return format(amount, 'f')


def _validate_row(table, row, columns):
    if not isinstance(row, dict) or set(row) != set(columns):
        raise ValueError('记录字段与已登记结构不一致')
    declared = DECLARED.fact(table)
    if declared is None:
        raise ValueError('来源包含未登记的数据集合')
    if set(columns) != set(declared.columns):
        raise ValueError(f'{table} 的物理列与声明不一致')
    for key in ('day', 'campaign'):
        if row[key] in (None, ''):
            raise ValueError(f'{table} 的 {key} 不能为空')
    if 'channel' in row and row['channel'] in (None, ''):
        raise ValueError(f'{table} 的 channel 不能为空')
    if 'amount' in row:
        row['amount'] = _normalize_amount(row['amount'])
    return row['day']


def partition_digest(rows):
    """一个分区的规范化内容摘要，是缓存键里的实际输入版本。"""
    canonical = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')) for row in rows)
    return hashlib.sha256('\n'.join(canonical).encode('utf-8')).hexdigest()


def _version(connection, table, day):
    record = connection.execute(
        'SELECT digest, generation, rows, mutation_id FROM fact_versions WHERE table_name=? AND partition_value=?',
        (table, day),
    ).fetchone()
    if record is None:
        return None
    return dict(record)


def current_versions(connection, table, days):
    """只返回有版本登记的分区；表内无登记的行不参与版本推进，避免伪造代次。"""
    versions = {}
    for day in sorted(days):
        record = _version(connection, table, day)
        if record is None:
            continue
        versions[day] = {
            'digest': record['digest'],
            'generation': record['generation'],
            'rows': record['rows'],
            'mutation_id': record['mutation_id'],
        }
    return versions


def version_rows_digest(connection, table, day):
    """按登记版本计算当前表内该日行的规范化摘要，用于检测混用发布代次。"""
    columns = _columns(connection, table)
    select = ', '.join(f'"{column}"' for column in columns)
    rows = [dict(record) for record in connection.execute(
        f'SELECT {select} FROM "{table}" WHERE day=?', (day,)
    )]
    return partition_digest(rows)


def partition_range(connection, table, start, end):
    """表格与版本登记中实际存在于范围内的分区日。"""
    days = {record[0] for record in connection.execute(
        f'SELECT DISTINCT day FROM "{table}" WHERE day BETWEEN ? AND ?', (start, end)
    )}
    days.update(record[0] for record in connection.execute(
        'SELECT partition_value FROM fact_versions WHERE table_name=? AND partition_value BETWEEN ? AND ?',
        (table, start, end),
    ))
    return days


def cache_lookup(connection, key):
    record = connection.execute('SELECT payload FROM query_cache WHERE cache_key=?', (key,)).fetchone()
    if record is None:
        return None
    return json.loads(record['payload'])


def cache_store(connection, key, payload, declaration_fingerprint, versions):
    from datetime import datetime, timezone
    encoded_versions = json.dumps(versions, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    with transaction(connection):
        connection.execute(
            'INSERT INTO query_cache (cache_key, payload, fingerprint, versions, created) VALUES (?, ?, ?, ?, ?) '
            'ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, fingerprint=excluded.fingerprint, '
            'versions=excluded.versions, created=excluded.created',
            (key, json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
             declaration_fingerprint, encoded_versions, datetime.now(timezone.utc).isoformat()),
        )


def cache_prune(connection, keep=64):
    with transaction(connection):
        connection.execute(
            'DELETE FROM query_cache WHERE cache_key NOT IN (SELECT cache_key FROM query_cache ORDER BY created DESC LIMIT ?)',
            (keep,),
        )


def _next_mutation(connection, origin, detail):
    connection.execute('INSERT INTO mutations (origin, detail) VALUES (?, ?)', (origin, detail))
    return connection.execute('SELECT last_insert_rowid()').fetchone()[0]


def _publish(connection, table, day, rows, origin, mutation_id, kind):
    """整体替换一个事实分区，并记录代次。"""
    previous = _version(connection, table, day)
    digest = partition_digest(rows)
    if previous and previous['digest'] == digest:
        return previous['generation'], previous['rows'], False
    generation = (previous['generation'] if previous else 0) + 1
    columns = _columns(connection, table)
    connection.execute(f'DELETE FROM "{table}" WHERE day=?', (day,))
    names = ', '.join(f'"{column}"' for column in columns)
    placeholders = ', '.join('?' for _ in columns)
    for row in rows:
        connection.execute(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', [row[column] for column in columns])
    connection.execute(
        'INSERT INTO fact_versions (table_name, partition_value, digest, generation, rows, mutation_id) '
        'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(table_name, partition_value) DO UPDATE SET '
        'digest=excluded.digest, generation=excluded.generation, rows=excluded.rows, mutation_id=excluded.mutation_id',
        (table, day, digest, generation, len(rows), mutation_id),
    )
    connection.execute(
        'INSERT INTO fact_version_history (table_name, partition_value, digest, generation, rows, mutation_id, kind) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (table, day, digest, generation, len(rows), mutation_id, kind),
    )
    connection.execute(
        'INSERT INTO mutation_partitions (mutation_id, table_name, partition_value, digest, generation) VALUES (?, ?, ?, ?, ?)',
        (mutation_id, table, day, digest, generation),
    )
    return generation, len(rows), True


def _group(connection, bundle):
    groups = {}
    for table, rows in bundle.items():
        columns = _columns(connection, table)
        for row in rows:
            _validate_row(table, row, columns)
            groups.setdefault((table, row['day']), []).append(row)
    return groups


def load_bundle(connection, bundle, batch):
    if not isinstance(bundle, dict) or not isinstance(batch, str) or not batch:
        raise ValueError('输入需要命名数据集合与批次标识')
    if set(bundle) - set(SCHEMA):
        raise ValueError('来源包含未登记的数据集合')
    digest = hashlib.sha256(encode(bundle)).hexdigest()
    with transaction(connection):
        previous = connection.execute('SELECT digest FROM ingest_batches WHERE batch=?', (batch,)).fetchone()
        if previous:
            if previous['digest'] != digest:
                raise ValueError('同批次内容不一致')
            return False
        for table, rows in bundle.items():
            if not isinstance(rows, list):
                raise ValueError('数据集合必须是记录数组')
        groups = _group(connection, bundle)
        mutation_id = _next_mutation(connection, 'bundle', batch)
        total = 0
        for (table, day), rows in sorted(groups.items()):
            existing = _version(connection, table, day)
            if existing:
                raise sqlite3.IntegrityError(f'UNIQUE constraint failed: {table} 分区 {day} 已发布，更正需显式替换')
            generation, count, _ = _publish(connection, table, day, rows, 'bundle', mutation_id, 'batch')
            total += count
        connection.execute('INSERT INTO ingest_batches VALUES (?, ?, ?)', (batch, digest, total))
    return True


def load_file(connection, path, batch):
    return load_bundle(connection, json.loads(Path(path).read_text(encoding='utf-8')), batch)


def replace_fact(connection, fact_name, day, rows, batch):
    """更正显式事实分区：整体替换该日行，其他分区与其他事实不受影响。"""
    declared = DECLARED.fact(fact_name)
    if declared is None:
        raise ValueError('来源包含未登记的数据集合')
    if not isinstance(rows, list) or not batch:
        raise ValueError('更正需要记录数组与批次标识')
    table = declared.table
    columns = _columns(connection, table)
    for row in rows:
        _validate_row(table, row, columns)
        if row['day'] != day:
            raise ValueError('更正记录必须属于同一分区')
    digest = hashlib.sha256(encode({'fact': fact_name, 'partition': day, 'rows': rows})).hexdigest()
    with transaction(connection):
        previous = connection.execute('SELECT digest FROM ingest_batches WHERE batch=?', (batch,)).fetchone()
        if previous and previous['digest'] != digest:
            raise ValueError('同批次内容不一致')
        if previous:
            current = _version(connection, table, day)
            if current is None:
                return {'generation': 0, 'rows': 0, 'changed': False}
            return {'generation': current['generation'], 'rows': current['rows'], 'changed': False}
        mutation_id = _next_mutation(connection, 'correction', f'{fact_name}:{day}')
        generation, count, changed = _publish(connection, table, day, rows, 'correction', mutation_id, 'correction')
        connection.execute('INSERT INTO ingest_batches VALUES (?, ?, ?)', (batch, digest, count))
    return {'generation': generation, 'rows': count, 'changed': changed}


def version_history(connection, table=None):
    if table:
        records = connection.execute(
            'SELECT table_name, partition_value, digest, generation, rows, mutation_id, kind FROM fact_version_history WHERE table_name=? ORDER BY event_id',
            (table,),
        )
    else:
        records = connection.execute(
            'SELECT table_name, partition_value, digest, generation, rows, mutation_id, kind FROM fact_version_history ORDER BY event_id'
        )
    return [dict(record) for record in records]
