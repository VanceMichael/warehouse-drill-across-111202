# drill-across

跨事实查询规划层：调用方按注册维度与注册指标取指标，由规划器分别聚合曝光、
点击和预算事实，再在合法共同粒度上对齐。不再由调用方自行拼宽表。

## 数据组织

- `impressions`：`event TEXT PRIMARY KEY, day TEXT NOT NULL, campaign TEXT NOT NULL, channel TEXT NOT NULL, viewer TEXT NOT NULL`
- `clicks`：`event TEXT PRIMARY KEY, day TEXT NOT NULL, campaign TEXT NOT NULL, viewer TEXT NOT NULL`
- `budgets`：`budget_key TEXT PRIMARY KEY, day TEXT NOT NULL, campaign TEXT NOT NULL, amount TEXT NOT NULL`

事实声明在 `warehouse/declaration.py`，是规划器唯一的名称来源：

| 事实 | 声明粒度 | 注册指标 |
| --- | --- | --- |
| `impressions` | 日、活动、渠道 | `impressions`（行数）、`unique_viewers`（viewer 集合） |
| `clicks` | 日、活动 | `clicks`（行数）、`unique_clickers`（viewer 集合） |
| `budgets` | 日、活动 | `budget`（十进制定点和）、`days_with_budget`（有预算的日集合） |

注册维度为 `day`、`week`（由 `day` 派生的 ISO 周）、`campaign`、`channel`。
`ctr` 不在事实中存储，声明为 `clicks / impressions` 的可组合比率，先分别聚合
分子分母，再在对齐后的组上做除法。

预算事实没有 `channel`。请求 `channel` 维度或按渠道筛选并同时要预算时，
`reject` 模式明确拒绝；`annotate` 模式保留其他事实的行，把预算标为
`channel_not_declared` 不可用，绝不把金额复制到各渠道。点击/点击用户同理
缺少渠道，按 `dimension_not_in_fact_grain` 处理。

## 查询请求

请求只接受闭合字段：`dimensions`、`measures`、`period`、`campaign`、`channel`。
维度与指标必须是注册名；字段名先做标识符校验，任何未注册名（包含
`impressions; DROP TABLE clicks` 之类）在接触数据库前就被拒绝，规划器只把
已校验的名称映射到声明中的列，不存在拼接调用方文本的 SQL。

```json
{
  "dimensions": ["week"],
  "measures": ["impressions", "unique_viewers", "clicks", "ctr", "budget"],
  "period": {"start": "2026-10-01", "end": "2026-10-06"}
}
```

结果包含：

- `business`：按共同维度分组的行；某侧事实无数据的组保留，缺失的计数按 0、
  缺失的金额按 `0.00` 参与，`reason` 标为 `no_rows`。零分母的比率返回
  `value: null` 与 `reason: zero_denominator`，并给出合并后的分子分母。
- `explain`：扫描的分区（表、分区键、日、内容摘要、代次）、每个指标的合并方法与
  粒度拒绝原因、对齐规则、缓存键与声明指纹。
- `plan`：规范化后的计划，便于确认规划器理解了什么。

去重用户跨日跨渠道只在集合上合并，绝不把每日 `distinct` 相加；预算金额按
十进制定点（以“分”为单位）累加，避免二进制浮点误差。

## 分区版本、缓存与代次

装载以事实的“表 + 日”分区为发布单位。每次装载或更正都会写入
`fact_versions`（当前版本）与 `fact_version_history`（历史），代次单调递增；
更正某日分区只替换该日行，其他分区和事实不受影响。

查询在同一个读事务内读取版本与实际行内容摘要，二者必须一致，否则拒绝执行，
避免跨发布代次读取。缓存键由 `planner` 版本、声明指纹、请求（模式、维度、
指标、周期、筛选）、实际扫描的分区内容摘要组成；同一逻辑请求在输入版本或
指标定义变化后一定得到不同的键。缓存保存在 SQLite 的 `query_cache` 表中。

## 环境与验证

Python 3.11 以上，仅使用标准库，无第三方安装。`runtime.txt` 记录验证解释器版本。
数据库和对象目录均由入口参数指定。

```bash
python3 -m compileall -q warehouse tests scripts
python3 -m unittest discover -s tests -v
python3 scripts/demo.py
```

`scripts/demo.py` 每次使用全新临时目录，执行真实导入、跨事实查询、粒度拒绝、
annotate 返回、分区更正后重查与导出内容校验；失败返回非零退出码，不回放预制日志。

## 运行

```bash
python3 -m warehouse import --db /tmp/drill-across.sqlite3 --input examples/source.json --batch first
python3 -m warehouse inspect --db /tmp/drill-across.sqlite3
python3 -m warehouse query --db /tmp/drill-across.sqlite3 --request '{"dimensions":["week"],"measures":["impressions","unique_viewers","clicks","ctr","budget"],"period":{"start":"2026-10-01","end":"2026-10-06"}}'
python3 -m warehouse query --db /tmp/drill-across.sqlite3 --mode annotate --request '{"dimensions":["channel"],"measures":["impressions","budget"],"period":{"start":"2026-10-01","end":"2026-10-06"}}'
python3 -m warehouse correct --db /tmp/drill-across.sqlite3 --fact clicks --day 2026-10-03 --input examples/correction.json --batch fix-1
python3 -m warehouse explain --db /tmp/drill-across.sqlite3 --request '{"dimensions":["day"],"measures":["ctr"],"period":{"start":"2026-10-01","end":"2026-10-06"}}'
python3 -m warehouse export --db /tmp/drill-across.sqlite3 --objects /tmp/drill-across-objects
```

`examples/source.json` 为本地合成明细，覆盖同一用户跨日跨渠道、某日有曝光无
点击、预算缺日；`examples/correction.json` 为一次点击分区更正；
`examples/expected.json` 是 `inspect` 的公开样例结果。`schema.json` 描述物理结构。

`inspect`/`export` 保持原有的独立统计口径（各事实行数与曝光用户基数），
跨事实口径统一走 `query` 与 `explain`。
