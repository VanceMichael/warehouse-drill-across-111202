from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal


def overview(connection):
    rows = [dict(record) for record in connection.execute("SELECT 'impressions' AS source, COUNT(*) AS rows FROM impressions UNION ALL SELECT 'clicks', COUNT(*) FROM clicks UNION ALL SELECT 'budgets', COUNT(*) FROM budgets UNION ALL SELECT 'unique_viewers', COUNT(DISTINCT viewer) FROM impressions")]
    return rows
