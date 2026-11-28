"""Apply the event preferences to one verified page of choices."""

from packages.resonance_shared.deep_dive_event_options import choose_event_option


def choose_available_event_option(state, observation, preferences=None, *, is_supported=None):
    rows = observation['event_options']
    page_name = observation.get('event_option_name')
    group = state.get('event_option_group')
    if page_name is not None and group is None:
        group = state['event_option_group'] = page_name
    policy = (preferences or {}).get(group, {})
    disabled = set(policy.get('disabled', ()))
    order = {id_: rank for rank, id_ in enumerate(policy.get('order', ()))}
    skipped, candidates = [], []
    for index, row in enumerate(rows):
        option_id = row.get('id')
        followup = row.get('supported_followup_options')
        if preferences and page_name is None:
            reason = 'native_identity_unverified'
        elif option_id in disabled:
            reason = 'user_disabled'
        elif row['state'] == 'disabled':
            reason = 'game_disabled'
        elif row['blocked']:
            reason = 'blocked_by_run_rules'
        elif is_supported is not None and not is_supported(row):
            reason = 'unsupported'
        elif followup and disabled.issuperset(followup):
            reason = 'followup_all_disabled'
        else:
            candidates.append((index, row))
            continue
        skipped.append({'id': option_id, 'title': row.get('title'), 'reason': reason})

    chosen = None
    if candidates:
        ranked = [candidate for candidate in candidates if candidate[1].get('id') in order]
        chosen = (min(ranked, key=lambda candidate: order[candidate[1]['id']])
                  if ranked else choose_event_option(candidates))
    state['option_decision'] = dict(
        event=group, page=page_name, event_id=observation.get('event_option_id'),
        selected_id=chosen[1].get('id') if chosen else None, skipped=skipped,
    )
    return chosen
