"""Settings and purchase order for the Deep Dive board shop."""


def normalize_shop_policy(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('商店购买设置必须是对象')
    enabled = value.get('enabled', True)
    recommended_only = value.get('recommended_only', True)
    order = value.get('order', 'low')
    limit = value.get('limit', 0)
    if (type(enabled) is not bool or type(recommended_only) is not bool or order not in ('low', 'high')
            or type(limit) is not int or not 0 <= limit <= 2_147_483_647):
        raise ValueError('无效的识海商店购买设置')
    return dict(enabled=enabled, recommended_only=recommended_only, order=order, limit=limit)


def shop_candidate(policy, rows):
    candidates = (row for row in rows if row['action'] == 'buy' and not row['disabled']
                  and (not policy['recommended_only'] or row.get('recommended') is True))
    pick = min if policy['order'] == 'low' else max
    return pick(candidates, key=lambda row: row['price'], default=None)
