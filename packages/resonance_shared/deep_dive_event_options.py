"""Deep Dive event preferences and the default ranking shared with the editor."""

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any


_FREE_BENEFIT, _FREE_EXIT, _PAID_NONCOMBAT, _BATTLE, _UNCLASSIFIED = range(5)
_ITEM_COSTS = {'LoseEquip': 'dice', 'LoseBuff': 'thrust'}
_BENEFITS = {'Reward', 'Intensify', 'Recover'}

Option = Mapping[str, Any]
Candidate = tuple[int, Option]
Cost = tuple[str, float]


def _costs(effect: Option) -> list[Cost]:
    kind = effect['orderType']
    if kind == 'Recover' and effect['param'] < 0:
        return [('hp', -effect['param'])]
    if kind in _ITEM_COSTS:
        return [(_ITEM_COSTS[kind], effect['param'])]
    if kind == 'Discard':
        return [(f"item:{item['id']}", item['num']) for item in effect['discardList']]
    return []


def _classify(option: Option) -> tuple[int, Cost | None]:
    effects = option.get('effects')
    if effects is None:
        return _UNCLASSIFIED, None
    if not effects:
        return _FREE_EXIT, None
    if any(effect['orderType'] == 'Level' for effect in effects):
        return _BATTLE, None

    costs = []
    followup = False
    for effect in effects:
        kind = effect['orderType']
        if kind == 'Event' and option.get('next_event_id') == effect['eventId']:
            # Only catalogued continuations whose reachable choices avoid battle are eligible here.
            followup = True
        elif kind not in _BENEFITS and kind not in _ITEM_COSTS and kind != 'Discard':
            return _UNCLASSIFIED, None
        costs.extend(_costs(effect))

    if not costs and not followup:
        return _FREE_BENEFIT, None
    resources = {resource for resource, _ in costs}
    if followup or len(resources) != 1:
        return _PAID_NONCOMBAT, None
    return _PAID_NONCOMBAT, (costs[0][0], sum(amount for _, amount in costs))


def choose_event_option(candidates: Sequence[Candidate]) -> Candidate:
    ranked = [(index, option, *_classify(option)) for index, option in candidates]
    best = min(ranked, key=lambda row: (
        row[2], row[1].get('priority', 10) if row[2] in (_FREE_BENEFIT, _UNCLASSIFIED) else 0, row[0],
    ))
    index, option, tier, cost = best
    if cost is None:
        return index, option

    # Compare quantities only within the same resource. UI order resolves incomparable costs.
    comparable = [row for row in ranked if row[2] == tier and row[3] is not None and row[3][0] == cost[0]]
    index, option, _, _ = min(comparable, key=lambda row: (row[3][1], row[0]))
    return index, option


def normalize_event_option_policy(value=None):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError('事件优先级设置必须是列表')
    result = []
    names = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError('每个事件优先级必须是对象')
        name = entry.get('name')
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError('事件名称不能为空或重复')
        names.add(name)
        normalized = {'name': name}
        for key in ('order', 'disabled'):
            ids = entry.get(key, [])
            if (not isinstance(ids, list) or any(type(id_) is not int or not 1 <= id_ <= 2_147_483_647 for id_ in ids)
                    or len(ids) != len(set(ids))):
                raise ValueError(f'事件「{name}」的选项 ID 无效或重复')
            normalized[key] = list(ids)
        if normalized['order'] or normalized['disabled']:
            result.append(normalized)
    return result


def supports_native_event_option(option):
    return not option.get('blocked') and (option.get('supported', True) or option.get('battle_level') is not None)


def load_event_option_groups(plan_root: Path):
    events, options, names = {}, {}, {}
    for relative in ('data/deep_dive_events/healing.json', 'data/deep_dive_vortex/catalog.json'):
        data = json.loads((plan_root / relative).read_text(encoding='utf-8'))
        options.update((row['id'], row) for row in data['options'])
        for event in data['events']:
            events[event['id']] = event
            names.setdefault(event['name'], []).append(event['id'])

    groups = {}
    for name, initial_pages in names.items():
        pages = list(initial_pages)
        visited = set()
        rows = {}
        for event_id in pages:
            if event_id in visited:
                continue
            visited.add(event_id)
            event = events[event_id]
            candidates = [(index, options[id_]) for index, id_ in enumerate(event['options'])]
            ranked = [candidate for candidate in candidates if supports_native_event_option(candidate[1])]
            ordered = []
            while ranked:
                preferred = choose_event_option(ranked)
                ordered.append(preferred)
                ranked.remove(preferred)
            ordered.extend(candidate for candidate in candidates if not supports_native_event_option(candidate[1]))
            for _, option in ordered:
                rows.setdefault(option['id'], dict(option, variant=event['variant'], event_id=event_id))
                followup = option.get('next_event_id')
                if followup in events and followup not in visited:
                    pages.append(followup)
        groups[name] = list(rows.values())
    return groups
