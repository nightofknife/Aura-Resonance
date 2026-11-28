"""Shared settings and purchase decisions for a Deep Dive plane interval."""

INTERVAL_OPERATIONS = ('revive', 'intensify', 'awake', 'recruit')
INTERVAL_PANELS = {'rest_main', 'rest_feedback', *(f'rest_{key}' for key in INTERVAL_OPERATIONS)}


def default_interval_policy():
    return {
        'order': list(INTERVAL_OPERATIONS),
        'revive': {'enabled': True, 'limit': 0},
        'intensify': {'enabled': True, 'limit': 0, 'recommended_only': True, 'order': 'low'},
        'awake': {'enabled': True, 'limit': 0, 'mode': 'focused', 'max_level': 5},
        'recruit': {'enabled': True, 'limit': 0, 'order': 'low'},
        'fill_party': True,
        'fixed_party': False,
    }


def normalize_interval_policy(value):
    defaults = default_interval_policy()
    if value is None:
        return defaults
    if not isinstance(value, dict):
        raise ValueError('位面间隙设置必须是对象')
    order = value.get('order', defaults['order'])
    if (not isinstance(order, list) or len(order) != len(INTERVAL_OPERATIONS)
            or any(not isinstance(key, str) for key in order) or set(order) != set(INTERVAL_OPERATIONS)):
        raise ValueError('位面间隙优先级必须包含四种不同的操作')
    defaults['order'] = list(order)
    for key in INTERVAL_OPERATIONS:
        supplied = value.get(key, {})
        if not isinstance(supplied, dict):
            raise ValueError('位面间隙各项设置必须是对象')
        for field, default in defaults[key].items():
            setting = supplied.get(field, default)
            if type(default) is bool:
                valid = type(setting) is bool
            elif field == 'limit':
                valid = type(setting) is int and 0 <= setting <= 2_147_483_647
            elif field == 'max_level':
                valid = type(setting) is int and 1 <= setting <= 5
            else:
                valid = setting in (('low', 'high') if field == 'order' else ('focused', 'even'))
            if not valid:
                raise ValueError(f'无效的位面间隙设置：{key}.{field}')
            defaults[key][field] = setting
    for key in ('fill_party', 'fixed_party'):
        setting = value.get(key, defaults[key])
        if type(setting) is not bool:
            raise ValueError(f'无效的位面间隙设置：{key}')
        defaults[key] = setting
    return defaults


def interval_candidate(operation, options, rows, focused=None, initial_party=None):
    candidates = [row for row in rows if row['action'] == 'buy' and not row['disabled']]
    if operation == 'revive' and initial_party is not None:
        candidates = [row for row in candidates if row['id'] in initial_party]
    if operation == 'intensify' and options['recommended_only']:
        candidates = [row for row in candidates if row.get('recommended') is True]
    if operation == 'awake':
        candidates = [row for row in candidates
                      if row['awake'] < min(row['max_awake'], options['max_level'])]
    if not candidates:
        return None
    if operation in {'intensify', 'recruit'}:
        field = 'price' if operation == 'intensify' else 'level'
        pick = min if options['order'] == 'low' else max
        return pick(candidates, key=lambda row: row[field])
    if operation == 'awake':
        if options['mode'] == 'even':
            return min(candidates, key=lambda row: row['awake'])
        return next((row for row in candidates if row['id'] == focused), candidates[0])
    return candidates[0]


def purchase_observed(purchase, rows):
    row = next((row for row in rows if row['action'] == 'buy' and row['id'] == purchase['id']), None)
    return row is None or ('awake' in purchase and row['awake'] > purchase['awake'])
