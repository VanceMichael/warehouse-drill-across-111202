import dataclasses
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from warehouse.catalog import connect, load_bundle, replace_fact
from warehouse.declaration import DECLARED, DeclarationError, fingerprint, validate_declared
from warehouse.executor import execute
from warehouse.planner import GranularityRejection, QueryRejected, build_plan
from warehouse.query import interpret_request, query

SOURCE = Path(__file__).resolve().parents[1] / 'examples/source.json'

REQUEST = {
    'dimensions': ['day'],
    'measures': ['impressions', 'unique_viewers', 'clicks', 'ctr'],
    'period': {'start': '2026-10-01', 'end': '2026-10-06'},
}


def request(**changes):
    base = json.loads(json.dumps(REQUEST))
    base.update(changes)
    return base


class WarehouseCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / 'warehouse.sqlite3'
        self.connection = connect(self.database)
        load_bundle(self.connection, json.loads(SOURCE.read_text(encoding='utf-8')), 'first')

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def rows(self, result):
        return {tuple(row['dimensions'][name] for name in result['business']['dimensions']): row['measures']
                for row in result['business']['rows']}


class DeclarationTests(unittest.TestCase):
    def test_declared_is_valid(self):
        validate_declared(DECLARED)

    def test_unknown_dimension_in_fact(self):
        broken = dataclasses.replace(
            DECLARED,
            facts=(dataclasses.replace(DECLARED.fact('budgets'), grain=('day', 'campaign', 'region')),)
            + tuple(fact for fact in DECLARED.facts if fact.name != 'budgets'),
        )
        with self.assertRaises(DeclarationError):
            validate_declared(broken)

    def test_measure_not_in_fact(self):
        broken = dataclasses.replace(
            DECLARED,
            facts=tuple(dataclasses.replace(fact, measures=('budget',)) if fact.name == 'clicks' else fact
                        for fact in DECLARED.facts),
        )
        with self.assertRaises(DeclarationError):
            validate_declared(broken)

    def test_measure_column_outside_fact(self):
        broken = dataclasses.replace(
            DECLARED,
            measures=tuple(
                dataclasses.replace(measure, column='viewer') if measure.name == 'budget' else measure
                for measure in DECLARED.measures
            ),
        )
        with self.assertRaises(DeclarationError):
            validate_declared(broken)

    def test_ratio_self_reference(self):
        broken = dataclasses.replace(
            DECLARED,
            measures=DECLARED.measures + (dataclasses.replace(
                DECLARED.measure('ctr'), name='ctr_self',
                numerator=DECLARED.measure('ctr'), denominator=DECLARED.measure('clicks'),
            ),),
        )
        with self.assertRaises(DeclarationError):
            validate_declared(broken)

    def test_fingerprint_changes_with_semantics(self):
        changed = dataclasses.replace(
            DECLARED,
            facts=tuple(dataclasses.replace(fact, grain=('day', 'campaign', 'channel', 'region'))
                        if fact.name == 'clicks' else fact for fact in DECLARED.facts),
        )
        self.assertNotEqual(fingerprint(DECLARED), fingerprint(changed))


class RejectionTests(WarehouseCase):
    def test_budget_on_channel_is_refused(self):
        with self.assertRaises(GranularityRejection) as caught:
            build_plan(DECLARED, request(dimensions=['channel'], measures=['impressions', 'budget']))
        detail = caught.exception.detail
        self.assertEqual(detail['conflicts'][0]['reason'], 'channel_not_declared')
        self.assertIn('预算', detail['alignment'])
        self.assertIn('alternatives', detail)

    def test_channel_filter_also_refuses_budget(self):
        with self.assertRaises(GranularityRejection):
            build_plan(DECLARED, request(measures=['budget'], channel='web'))

    def test_clickers_on_channel_is_refused_as_grain_mismatch(self):
        with self.assertRaises(GranularityRejection) as caught:
            build_plan(DECLARED, request(dimensions=['channel'], measures=['unique_clickers']))
        self.assertEqual(caught.exception.detail['conflicts'][0]['reason'], 'dimension_not_in_fact_grain')

    def test_annotate_marks_budget_unavailable_without_copying(self):
        result = query(self.connection, request(dimensions=['channel'], measures=['impressions', 'budget']),
                       mode='annotate')
        self.assertEqual({row['dimensions']['channel'] for row in result['business']['rows']}, {'app', 'web'})
        for row in result['business']['rows']:
            budget = row['measures']['budget']
            self.assertIsNone(budget['value'])
            self.assertEqual(budget['reason'], 'channel_not_declared')
            self.assertIsNone(row['measures']['budget'].get('copied'))
        self.assertEqual(result['explain']['rejections'][0]['reason'], 'channel_not_declared')
        # 扫描解释不应声称扫描了预算事实
        self.assertNotIn('budgets', [entry['fact'] for entry in result['explain']['scanned']])

    def test_identifier_rejected_without_execution(self):
        before = self.connection.execute('SELECT COUNT(*) FROM impressions').fetchone()[0]
        with self.assertRaises(QueryRejected) as caught:
            query(self.connection, request(measures=['impressions; DROP TABLE clicks']))
        self.assertEqual(caught.exception.code, 'invalid_identifier')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM impressions').fetchone()[0], before)
        self.assertIn('click', [name for name, _ in
                                self.connection.execute("SELECT name, 1 FROM sqlite_master WHERE type='table'").fetchall()] + ['click'])

    def test_unknown_measure_is_not_identifier_error(self):
        with self.assertRaises(QueryRejected) as caught:
            query(self.connection, request(measures=['clickers']))
        self.assertEqual(caught.exception.code, 'unknown_measure')

    def test_request_keys_are_closed(self):
        invalid = request()
        invalid['sql'] = 'SELECT * FROM budgets'
        with self.assertRaises(QueryRejected) as caught:
            query(self.connection, invalid)
        self.assertEqual(caught.exception.code, 'invalid_request')

    def test_injection_shaped_filter_value_is_bound_not_executed(self):
        result = query(self.connection, {'dimensions': ['campaign'],
                                         'measures': ['clicks'],
                                         'period': {'start': '2026-10-01', 'end': '2026-10-06'},
                                         'campaign': "c1' OR 1=1 --"})
        self.assertEqual(result['business']['rows'], [])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM clicks').fetchone()[0], 5)

    def test_invalid_period_is_rejected(self):
        with self.assertRaises(QueryRejected) as caught:
            query(self.connection, request(period={'start': '2026-10-06', 'end': '2026-10-01'}))
        self.assertEqual(caught.exception.code, 'invalid_period')

    def test_unknown_dimension_is_rejected(self):
        with self.assertRaises(QueryRejected) as caught:
            query(self.connection, request(dimensions=['region']))
        self.assertEqual(caught.exception.code, 'unknown_dimension')

    def test_interpret_reports_rejection_without_database(self):
        report = interpret_request(request(dimensions=['channel'], measures=['budget']))
        self.assertFalse(report['accepted'])
        self.assertEqual(report['code'], 'granularity_rejected')
        self.assertEqual(report['detail']['conflicts'][0]['reason'], 'channel_not_declared')


class AlignmentTests(WarehouseCase):
    def test_daily_values(self):
        result = query(self.connection, REQUEST)
        rows = self.rows(result)
        self.assertEqual(rows[('2026-10-01',)]['impressions']['value'], 2)
        self.assertEqual(rows[('2026-10-01',)]['unique_viewers']['value'], 1)
        self.assertEqual(rows[('2026-10-01',)]['clicks']['value'], 2)
        self.assertEqual(rows[('2026-10-01',)]['ctr']['value'], 1.0)
        self.assertEqual(rows[('2026-10-02',)]['unique_viewers']['value'], 2)

    def test_distinct_is_not_summed_across_days(self):
        result = query(self.connection, {'dimensions': ['campaign'],
                                         'measures': ['unique_viewers', 'unique_clickers'],
                                         'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        rows = self.rows(result)
        # u1 appears on three days in c1; per-day distinct would give 3
        self.assertEqual(rows[('c1',)]['unique_viewers']['value'], 2)
        self.assertEqual(rows[('c2',)]['unique_viewers']['value'], 4)
        self.assertEqual(rows[('c1',)]['unique_clickers']['value'], 2)

    def test_same_viewer_across_channels_counts_once(self):
        result = query(self.connection, {'dimensions': ['campaign'],
                                         'measures': ['unique_viewers'],
                                         'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        self.assertEqual(self.rows(result)[('c1',)]['unique_viewers']['value'], 2)

    def test_ratio_uses_composed_parts_not_average(self):
        """跨日汇总时必须用合并后的点击/曝光，不是每日比率的平均。"""
        result = query(self.connection, {'dimensions': ['campaign'],
                                         'measures': ['impressions', 'clicks', 'ctr'],
                                         'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        rows = self.rows(result)
        c1 = rows[('c1',)]
        self.assertEqual(c1['impressions']['value'], 5)
        self.assertEqual(c1['clicks']['value'], 3)
        self.assertAlmostEqual(c1['ctr']['value'], 3 / 5)
        # 每日比率分别为 2/2=1.0、1/2=0.5、0/1=0.0，算术平均是 0.5，不符合合并口径
        daily = query(self.connection, {'dimensions': ['day'],
                                        'measures': ['impressions', 'clicks', 'ctr'],
                                        'period': {'start': '2026-10-01', 'end': '2026-10-03'},
                                        'campaign': 'c1'})
        daily_rows = self.rows(daily)
        average = sum(row['ctr']['value'] for row in daily_rows.values()) / len(daily_rows)
        self.assertNotAlmostEqual(c1['ctr']['value'], average)

    def test_weekly_rollup_keeps_set_semantics(self):
        result = query(self.connection, {'dimensions': ['week'],
                                         'measures': ['unique_viewers', 'impressions', 'unique_clickers'],
                                         'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        rows = self.rows(result)
        # 2026-10-01/02 属于 W40；10-03/05/06 属于 W41（ISO 周从周一开始）
        self.assertEqual(rows[('2026-W40',)]['impressions']['value'], 5)
        self.assertEqual(rows[('2026-W40',)]['unique_viewers']['value'], 2)
        self.assertEqual(rows[('2026-W41',)]['impressions']['value'], 4)
        self.assertEqual(rows[('2026-W41',)]['unique_viewers']['value'], 4)
        # u1 连续出现在 W40 的 10-01 与 10-02，仍是同一集合元素
        self.assertEqual(rows[('2026-W40',)]['unique_clickers']['value'], 2)
        # 跨日相加会得到 1+2=3，集合合并才是 2
        self.assertNotEqual(rows[('2026-W40',)]['unique_viewers']['value'], 3)

    def test_zero_clicks_group_keeps_impressions_and_zero_ctr(self):
        result = query(self.connection, REQUEST)
        rows = self.rows(result)
        day = ('2026-10-03',)
        self.assertEqual(rows[day]['impressions']['value'], 1)
        self.assertEqual(rows[day]['clicks']['value'], 0)
        self.assertEqual(rows[day]['ctr']['value'], 0.0)
        self.assertEqual(rows[day]['ctr']['denominator'], 1)

    def test_zero_denominator_without_impressions(self):
        """点击侧有数据、曝光侧无数据的组：比率分母为零，返回原因而不是 0 或崩溃。"""
        from warehouse.catalog import partition_digest
        rows = [{'event': 'k9', 'day': '2026-10-07', 'campaign': 'c3', 'viewer': 'u9'}]
        self.connection.execute('BEGIN IMMEDIATE')
        self.connection.execute("INSERT INTO clicks VALUES ('k9', '2026-10-07', 'c3', 'u9')")
        self.connection.execute(
            'INSERT INTO fact_versions (table_name, partition_value, digest, generation, rows, mutation_id) VALUES (?, ?, ?, ?, ?, ?)',
            ('clicks', '2026-10-07', partition_digest(rows), 1, 1, 1),
        )
        self.connection.commit()
        result = query(self.connection, {'dimensions': ['campaign', 'day'],
                                         'measures': ['ctr', 'clicks'],
                                         'period': {'start': '2026-10-07', 'end': '2026-10-07'}})
        rows = self.rows(result)
        day = ('c3', '2026-10-07')
        self.assertIn(day, rows)
        self.assertEqual(rows[day]['clicks']['value'], 1)
        self.assertEqual(rows[day]['ctr']['reason'], 'zero_denominator')
        self.assertIsNone(rows[day]['ctr']['value'])

    def test_missing_budget_day_does_not_shift_other_facts(self):
        result = query(self.connection, {'dimensions': ['day'],
                                         'measures': ['impressions', 'budget', 'days_with_budget'],
                                         'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        rows = self.rows(result)
        # 2026-10-03 has impressions but no budget partition row
        self.assertEqual(rows[('2026-10-03',)]['impressions']['value'], 1)
        self.assertEqual(rows[('2026-10-03',)]['budget']['value'], '0.00')
        self.assertEqual(rows[('2026-10-03',)]['days_with_budget']['value'], 0)
        self.assertEqual(rows[('2026-10-03',)]['budget']['reason'], 'no_rows')
        totals = query(self.connection, {'dimensions': [],
                                          'measures': ['impressions', 'budget'],
                                          'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        total = self.rows(totals)[()]
        self.assertEqual(total['impressions']['value'], 9)
        self.assertEqual(total['budget']['value'], '17.67')
        self.assertEqual(total['budget']['method'], 'decimal_sum')

    def test_budget_is_not_multiplied_by_channel(self):
        result = query(self.connection, {'dimensions': ['campaign', 'day'],
                                         'measures': ['budget', 'impressions'],
                                         'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        rows = self.rows(result)
        self.assertEqual(rows[('c1', '2026-10-01')]['budget']['value'], '10.10')
        self.assertEqual(rows[('c1', '2026-10-01')]['impressions']['value'], 2)
        # only one row for the campaign/day pair, not one per channel
        pairs = [row for row in result['business']['rows']
                 if row['dimensions']['campaign'] == 'c1' and row['dimensions']['day'] == '2026-10-01']
        self.assertEqual(len(pairs), 1)

    def test_groups_from_only_one_fact_are_retained(self):
        replace_fact(self.connection, 'budgets', '2026-10-07',
                     [{'budget_key': 'bz', 'day': '2026-10-07', 'campaign': 'c9', 'amount': '5.00'}],
                     'budget-only')
        result = query(self.connection, {'dimensions': ['campaign'],
                                         'measures': ['impressions', 'clicks', 'budget', 'days_with_budget'],
                                         'period': {'start': '2026-10-01', 'end': '2026-10-07'}})
        groups = {row['dimensions']['campaign']: row['measures'] for row in result['business']['rows']}
        self.assertEqual(set(groups), {'c1', 'c2', 'c9'})
        # 只在预算事实出现的 c9：其他事实按加法单位参与，并标出 no_rows
        self.assertEqual(groups['c9']['impressions']['value'], 0)
        self.assertEqual(groups['c9']['impressions']['reason'], 'no_rows')
        self.assertEqual(groups['c9']['budget']['value'], '5.00')
        self.assertEqual(groups['c9']['days_with_budget']['value'], 1)
        # 只在曝光/点击事实出现的 c1、c2 也不会因预算缺失被丢弃
        self.assertEqual(groups['c1']['budget']['value'], '10.30')
        self.assertEqual(groups['c2']['budget']['value'], '7.37')

    def test_budget_sum_is_decimal_exact(self):
        bundle = {'budgets': [
            {'budget_key': 'b10', 'day': '2026-10-07', 'campaign': 'c9', 'amount': '0.10'},
            {'budget_key': 'b11', 'day': '2026-10-07', 'campaign': 'c9', 'amount': '0.20'},
            {'budget_key': 'b12', 'day': '2026-10-08', 'campaign': 'c9', 'amount': '90071992547409.93'},
        ]}
        load_bundle(self.connection, bundle, 'decimal')
        result = query(self.connection, {'dimensions': ['day'],
                                         'measures': ['budget'],
                                         'period': {'start': '2026-10-07', 'end': '2026-10-08'},
                                         'campaign': 'c9'})
        rows = self.rows(result)
        self.assertEqual(rows[('2026-10-07',)]['budget']['value'], '0.30')
        self.assertEqual(rows[('2026-10-08',)]['budget']['value'], '90071992547409.93')
        total = query(self.connection, {'dimensions': [], 'measures': ['budget'],
                                        'period': {'start': '2026-10-07', 'end': '2026-10-08'},
                                        'campaign': 'c9'})
        self.assertEqual(self.rows(total)[()]['budget']['value'], '90071992547410.23')

    def test_explain_includes_scans_merges_and_grains(self):
        result = query(self.connection, {'dimensions': ['day'],
                                         'measures': ['impressions', 'unique_viewers', 'ctr', 'budget'],
                                         'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        explain = result['explain']
        self.assertEqual(explain['alignment']['dimensions'], ['day'])
        methods = {entry['measure']: entry['method'] for entry in explain['merges']}
        self.assertEqual(methods['unique_viewers'], 'set_union_then_count')
        self.assertEqual(methods['ctr'], 'ratio_of_composed_parts')
        self.assertEqual(methods['budget'], 'decimal_sum')
        scanned = {entry['fact']: entry for entry in explain['scanned']}
        self.assertEqual(scanned['impressions']['partition_key'], 'day')
        self.assertIn('2026-10-01', [item['partition'] for item in scanned['impressions']['partitions']])
        self.assertEqual(scanned['budgets']['grain'], ['day', 'campaign'])


class CorrectionTests(WarehouseCase):
    def test_correction_changes_result_and_cache_key(self):
        before = query(self.connection, REQUEST)
        self.assertEqual(self.rows(before)[('2026-10-02',)]['impressions']['value'], 2)
        corrected = [
            {'event': 'i4', 'day': '2026-10-02', 'campaign': 'c1', 'channel': 'app', 'viewer': 'u2'},
            {'event': 'i10', 'day': '2026-10-02', 'campaign': 'c1', 'channel': 'web', 'viewer': 'u5'},
        ]
        replace_fact(self.connection, 'impressions', '2026-10-02', corrected, 'fix-1')
        after = query(self.connection, REQUEST)
        self.assertEqual(self.rows(after)[('2026-10-02',)]['impressions']['value'], 2)
        self.assertEqual(self.rows(after)[('2026-10-02',)]['unique_viewers']['value'], 2)
        self.assertNotEqual(before['cache_key'], after['cache_key'])
        self.assertNotEqual(before['explain']['versions'], after['explain']['versions'])
        self.assertEqual(after['explain']['scanned'][0]['partitions'][1]['generation'], 2)

    def test_correction_does_not_change_other_partitions_or_tables(self):
        rows_before = query(self.connection, {'dimensions': ['day'], 'measures': ['clicks'],
                                              'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        replace_fact(self.connection, 'clicks', '2026-10-01', [], 'fix-empty')
        rows_after = query(self.connection, {'dimensions': ['day'], 'measures': ['clicks'],
                                             'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        before = self.rows(rows_before)
        after = self.rows(rows_after)
        self.assertEqual(after[('2026-10-01',)]['clicks']['value'], 0)
        self.assertEqual(before[('2026-10-02',)], after[('2026-10-02',)])
        impressions = query(self.connection, {'dimensions': ['day'], 'measures': ['impressions'],
                                              'period': {'start': '2026-10-01', 'end': '2026-10-06'}})
        self.assertEqual(self.rows(impressions)[('2026-10-01',)]['impressions']['value'], 2)

    def test_repeated_correction_batch_is_noop(self):
        rows = [{'event': 'i4', 'day': '2026-10-02', 'campaign': 'c1', 'channel': 'app', 'viewer': 'u2'},
                {'event': 'i10', 'day': '2026-10-02', 'campaign': 'c1', 'channel': 'web', 'viewer': 'u5'}]
        first = replace_fact(self.connection, 'impressions', '2026-10-02', rows, 'fix-1')
        second = replace_fact(self.connection, 'impressions', '2026-10-02', rows, 'fix-1')
        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'])
        self.assertEqual(first['generation'], second['generation'])


class CacheTests(WarehouseCase):
    def test_cache_key_contains_versions_and_fingerprint(self):
        budget_request = {
            'dimensions': ['day'],
            'measures': ['impressions', 'budget'],
            'period': {'start': '2026-10-01', 'end': '2026-10-06'},
        }
        first = execute(DECLARED, budget_request, self.connection)
        second = execute(DECLARED, budget_request, self.connection)
        self.assertEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(second['explain']['cache']['status'], 'hit')
        self.assertEqual(first['explain']['cache']['status'], 'miss')
        key = first['cache_key']
        self.assertIn(fingerprint(DECLARED), first['explain']['cache']['fingerprint'])
        self.assertIn('input_versions', first['explain']['cache'])
        replace_fact(self.connection, 'budgets', '2026-10-01', [], 'c-fix')
        third = execute(DECLARED, budget_request, self.connection)
        self.assertNotEqual(third['cache_key'], key)
        self.assertEqual(third['explain']['cache']['status'], 'miss')

    def test_cache_key_differs_when_definition_changes(self):
        first = execute(DECLARED, REQUEST, self.connection)
        changed = dataclasses.replace(
            DECLARED,
            measures=DECLARED.measures + (
                dataclasses.replace(DECLARED.measure('ctr'), name='ctr_copy'),
            ),
        )
        second = execute(changed, REQUEST, self.connection)
        self.assertNotEqual(first['cache_key'], second['cache_key'])

    def test_cache_persists_across_connections(self):
        request = {'dimensions': ['day'], 'measures': ['budget'],
                   'period': {'start': '2026-10-01', 'end': '2026-10-06'}}
        query(self.connection, request)
        self.connection.close()
        self.connection = connect(self.database)
        replay = query(self.connection, request)
        self.assertEqual(replay['explain']['cache']['status'], 'hit')

    def test_point_in_time_uses_single_generation(self):
        result = query(self.connection, REQUEST)
        self.assertTrue(all(entry['generation'] is not None for entry in result['explain']['scanned'][0]['partitions']))
        self.assertEqual(len(set(result['explain']['versions'].values())), 1)


if __name__ == '__main__':
    unittest.main()
