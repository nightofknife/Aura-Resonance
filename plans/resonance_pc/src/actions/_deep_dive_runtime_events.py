"""Owner-scoped node, battle and reward flows; the caller owns all input."""
from __future__ import annotations

from uuid import uuid4

from packages.resonance_shared.deep_dive_shop import normalize_shop_policy, shop_candidate

from . import _deep_dive_event_flow as healing
from . import _deep_dive_vortex_flow as vortex
from ._deep_dive_choice_target import (
    can_retry, choice_target, matching_card, native_key, new_card_panel, ordered_cards,
    pending_target, preferred_card, result_progress, target_fields,
)
from ._deep_dive_native_choices import is_formation_page
from ._deep_dive_node_rewards import ITEM_REPLACEMENT, RESULT

BOARD = {'board', 'choose_move', 'choose_rotate'}
SELECTION = {'event_card_selection', 'battle_reward_selection'}
REWARD = RESULT | SELECTION
BATTLES = {'battle', 'elite_battle', 'boss_battle'}


def create_context(event_type, owner, now, *, source_action_id=None, resume='scan_turn',
                   observation=None):
    if event_type not in {'empty', 'shop', 'battle', 'elite_battle', 'boss_battle',
                          'healing', 'item', 'vortex', 'passive_reward'}:
        raise ValueError('Unsupported node event: ' + str(event_type))
    context = dict(context_id=uuid4().hex, event_type=event_type, owner=owner,
                   source_action_id=source_action_id, resume=resume, phase='entry_wait',
                   started=now, progress_at=now, trace=[], flow={}, pending=None,
                   reward=None, seen_content=False, victory=False, signature=None, stable=0)
    if event_type in {'healing', 'item', 'vortex'}:
        flow = context['flow']
        flow['context_event_type'] = event_type
        if not observation or not (observation.get('click') or observation.get('native_entry_submitted')):
            raise ValueError('Node flow requires a recognized entry control')
        (vortex if event_type == 'vortex' else healing).start(flow, observation, now)
        context['phase'] = 'node_dispatch'
    elif event_type == 'passive_reward':
        context['phase'] = 'reward_chain'
    return context


def _signature(o):
    shop = o.get('shop', {})
    return repr((o.get('scene'), [(r.get('point'), r.get('selected'), r.get('recommended'), native_key(r.get('native')))
                                  for r in o.get('cards', [])], native_key(o.get('native')),
                 o.get('formation_level_id'), native_key(o.get('battle_retry')),
                 shop.get('subject_id'), shop.get('revision'), shop.get('status')))


def _stable(context, o, now):
    signature = _signature(o)
    if signature == context.get('signature'):
        context['stable'] += 1
    else:
        context.update(signature=signature, stable=1, stable_since=now)
    return context['stable'] >= 2


def _step(status='waiting', *, target=None, action=None, reason=None, evidence=None):
    result = {'status': status}
    if isinstance(target, dict):
        result.update(action=action, native=dict(target))
    elif target is not None:
        result.update(action=action, point=list(map(int, target)))
    if reason:
        result['reason'] = reason
    if evidence:
        result['evidence'] = evidence
    return result


def _issue(context, action, input_target, now, **extra):
    context['pending'] = dict(action=action, at=now, tries=1, **target_fields(input_target), **extra)
    context['progress_at'] = now
    context['trace'].append(dict(action=action, at=now))
    context['signature'] = None
    context['stable'] = 0
    return _step('input_requested', target=input_target, action=action)


def _retry(context, now):
    pending = context['pending']
    if not can_retry(pending) or now - pending['at'] < 2:
        return _step()
    if pending['tries'] >= 3:
        return _step('blocked', reason=pending['action'] + ': confirmation retry limit')
    pending.update(at=now, tries=pending['tries'] + 1)
    context['signature'] = None
    context['stable'] = 0
    return _step('input_requested', target=pending_target(pending), action='retry_' + pending['action'])


def _shop_step(context, observation, now):
    scene = observation['scene']
    pending = context.get('pending')
    if context['phase'] == 'shop_return':
        if scene in BOARD and now - context['stable_since'] >= 1:
            return _step('completed', evidence='shop_returned')
        if now - context['progress_at'] >= 30:
            return _step('blocked', reason='shop_return_unverified')
        if scene == 'shop':
            return _retry(context, now)
        return _step()

    if now - context['progress_at'] >= 30:
        return _step('blocked', reason='shop_purchase_unverified' if pending else 'shop_no_progress')
    policy = context.get('shop_policy') or normalize_shop_policy()
    shop = observation.get('shop')
    if not shop:
        if not policy['enabled'] and scene == 'shop' and observation.get('click'):
            context.update(phase='shop_return', seen_content=True)
            return _issue(context, 'shop_back', observation['click'], now)
        return _step()
    if shop['status'] != 'ready':
        return _step()
    subject = shop['subject_id']
    if context.setdefault('shop_subject', subject) != subject:
        return _step('blocked', reason='shop_changed_before_return')
    rows = shop['rows']
    if pending:
        purchase = next((row for row in rows if row['action'] == 'buy'
                         and row['id'] == pending['native']['id']
                         and row['index'] == pending['native']['index']), None)
        if (purchase is None or shop['revision'] <= pending['native']['revision']
                or purchase['purchased'] <= pending['purchased']):
            return _step()
        context['shop_purchases'] = context.get('shop_purchases', 0) + 1
        context.update(pending=None, progress_at=now)
        context['trace'].append(dict(action='shop_purchase_confirmed', at=now, id=purchase['id'],
                                     price=pending['price'], balance=shop['balance']))

    row = None
    if policy['enabled'] and (not policy['limit'] or context.get('shop_purchases', 0) < policy['limit']):
        row = shop_candidate(policy, rows)
    purchase = row is not None and row['price'] <= shop['balance']
    if not purchase:
        row = next((item for item in rows if item['action'] == 'back' and not item['disabled']), None)
        if row is None:
            return _step()
    target = dict(kind='shop', revision=shop['revision'], subject_id=subject, command='confirm',
                  id=row['id'], index=row['index'])
    context.update(phase='shop_purchase' if purchase else 'shop_return', seen_content=True)
    if purchase:
        return _issue(context, 'shop_buy', target, now, purchased=row['purchased'], price=row['price'])
    return _issue(context, 'shop_back', target, now)


def _battle_retry_prompt(context, observation, now):
    if now - context['progress_at'] >= 45:
        return _step('blocked', reason='battle_retry_prompt_timeout')
    if observation['scene'] != 'battle_retry_confirm' or not _stable(context, observation, now):
        return _step()
    native = observation.get('native')
    if not native or native['subject_id'] != context['battle_level']:
        return _step('blocked', reason='battle_retry_level_mismatch')
    context.update(phase='battle_retry_wait', battle_attempt_started=now,
                   battle_retries=context.get('battle_retries', 0) + 1)
    return _issue(context, 'battle_retry_confirm', dict(native, command='confirm'), now)


def _reward_step(context, o, now, resume_scenes):
    """Drain one chain, retaining the exact owner until its resume page is stable."""
    reward = context.get('reward')
    if reward is None:
        reward = dict(started=now, progress_at=now, pending=None, signature=None,
                      stable=0, seen=False, between=False, trace=[])
        context['reward'] = reward
    scene = o.get('scene')
    if now - reward['progress_at'] > 30:
        return _step('blocked', reason='reward_chain_no_progress')
    if 'battle_formation' in resume_scenes and is_formation_page(o):
        # A recognized team page can close front rewards before its native
        # controls are ready. The caller still receives no input here.
        scene = 'battle_formation'
    if scene == 'native_choice_wait':
        reward.update(signature=None, stable=0)
        return _step()
    if scene not in REWARD:
        reward['between'] = True
        if not _stable(reward, o, now):
            return _step()
        board_delay = 5 if scene in BOARD else 1
        if (reward['seen'] and scene in resume_scenes
                and now - reward.get('stable_since', now) >= board_delay):
            context['reward'] = None
            context['progress_at'] = now
            return _step('completed', evidence='reward_chain_returned_to_owner')
        return _step()
    reward['seen'] = True
    pending = reward.get('pending')
    if reward.pop('between', False) and not (pending or {}).get('native'):
        reward['pending'] = None
        pending = None
        reward['progress_at'] = now
    cards = o.get('cards', [])
    has_control = o.get('click') or o.get('native') or any('native' in card for card in cards)
    if not _stable(reward, o, now) or not has_control:
        return _step()
    confirmed_card = None
    if pending:
        kind = pending['action']
        target = matching_card(cards, pending)
        if kind == 'select_reward' and scene in SELECTION and 'native' in pending and (
                target is None or any(card['selected'] and card is not target for card in cards)):
            return _step('blocked', reason='native_reward_selection_changed')
        accepted = ((kind == 'select_reward' and scene in SELECTION and target and target['selected'])
                    or (kind == 'confirm_reward' and
                        (scene in RESULT or scene in SELECTION and new_card_panel(cards, pending)))
                    or (kind in {'dismiss_reward', 'discard_new_item'} and
                        (scene in SELECTION or result_progress(pending, o))))
        if accepted:
            if kind == 'select_reward':
                confirmed_card = target
            reward['pending'] = None
            reward['progress_at'] = now
        elif 'native' not in pending or scene in SELECTION and target is not None:
            return _retry(reward, now)
        else:
            return _step()
    if scene in SELECTION:
        cards = ordered_cards(cards)
        if len(cards) not in (1, 2, 3):
            return _step('blocked', reason='unsupported_reward_card_count')
        target = confirmed_card or preferred_card(cards)
        if not target.get('recommended') and any(r['selected'] and r is not target for r in cards):
            return _step('blocked', reason='non_target_reward_selected')
        kind = 'confirm_reward' if target['selected'] else 'select_reward'
        point = o.get('click') if target['selected'] else target.get('point')
        input_target = choice_target(target, 'confirm' if target['selected'] else 'select', point)
        return _issue(reward, kind, input_target, now, target=target.get('point'))
    kind = 'discard_new_item' if scene == ITEM_REPLACEMENT else 'dismiss_reward'
    target = choice_target(o, 'discard' if scene == ITEM_REPLACEMENT else 'confirm', o.get('click'))
    return _issue(reward, kind, target, now, visual=o.get('result_visual'), result_scene=scene)


def advance_context(context, observation, now, *, event_option_policy=None):
    """Return one guarded input or an owner completion; never mutate game quotas."""
    boss_boundary = (context.get('last_player_action') and context['owner'] == 'player_move_node'
                     and observation.get('scene') == 'event_entry'
                     and observation.get('event_type') == 'boss_battle')
    enemy_board = ((observation.get('scene') == 'enemy_turn' and observation.get('enemy_turn') is True)
                   or boss_boundary)
    native_passive_reward = (context['event_type'] == 'passive_reward'
                             and observation.get('source') == 'bridge')
    o = (dict(observation, scene='board', event_board=True)
         if enemy_board and not native_passive_reward else observation)
    scene = o.get('scene', 'unknown')
    kind = context['event_type']
    if not o.get('valid'):
        context['signature'] = None
        context['stable'] = 0
        if kind in {'healing', 'item', 'vortex'}:
            (vortex if kind == 'vortex' else healing).reset_observation(context['flow'])
        return _step()
    if context['phase'] == 'battle_retry_prompt':
        return _battle_retry_prompt(context, o, now)
    if context['phase'] == 'battle_retry_wait':
        if now - context['progress_at'] >= 600:
            return _step('blocked', reason='battle_retry_result_timeout')
        if scene not in {'battle_victory', 'battle_defeat'}:
            return _step()
        native = o.get('native')
        if not native or native['subject_id'] != context['battle_level']:
            return _step('blocked', reason='battle_retry_level_mismatch')
        if native['revision'] <= context['pending']['native']['revision']:
            return _step()
        context.update(phase='battle_wait', pending=None)
    if scene == 'battle_defeat':
        if kind not in BATTLES or not o.get('native'):
            return _step('blocked', reason='battle_defeat_without_owner')
        if context['phase'] == 'defeat_return':
            if now - context['progress_at'] >= 45:
                return _step('blocked', reason='battle_defeat_settlement_timeout')
            return _step()
        if not _stable(context, o, now):
            return _step()
        retry = o.get('battle_retry')
        limit = context.get('battle_retry_limit', 0)
        if retry and context.get('battle_retries', 0) < limit:
            level = retry['subject_id']
            if (level != o['native']['subject_id']
                    or (context.get('battle_level') or level) != level):
                return _step('blocked', reason='battle_retry_level_mismatch')
            context.update(phase='battle_retry_prompt', battle_level=level, victory=False)
            return _issue(context, 'battle_retry_open', dict(retry, command='confirm'), now)
        context.update(phase='defeat_return', victory=False)
        return _issue(context, 'battle_defeat_next', choice_target(o, 'confirm', None), now)
    total_limit = 900 if kind in BATTLES else 180
    if now - context.get('battle_attempt_started', context['started']) >= total_limit:
        return _step('blocked', reason='event_total_timeout')
    if kind == 'passive_reward':
        resume_scenes = BOARD | {'rest_area', 'event_options', 'event_ending', 'battle_formation', 'shop'}
        if context['resume'] in {'enemy_wait', 'operation_outcome', 'verify_move'}:
            resume_scenes.update({'enemy_turn', 'event_entry'})
        return _reward_step(context, o, now, resume_scenes)
    # Rewards inserted before a battle/shop or healing/vortex content retain
    # their parent. Returning to the board here does not finish that node.
    prefix = 'vortex' if kind == 'vortex' else 'event'
    awaiting_content = (context['phase'] == 'entry_wait' or
                        kind in {'healing', 'vortex'} and
                        context['flow'].get(prefix + '_pending', {}).get('kind') == 'entry')
    front_reward = awaiting_content and scene in REWARD and kind != 'item'
    if front_reward or context.get('reward') and awaiting_content:
        resume = ({'battle_formation'} if kind in {'battle', 'elite_battle', 'boss_battle'}
                  else {'shop'} if kind == 'shop'
                  else {'event_options', 'event_ending'} if kind in {'healing', 'vortex'}
                  else BOARD)
        result = _reward_step(context, o, now, resume)
        if result['status'] != 'completed':
            return result
        return _step()
    if (kind == 'shop' and context['phase'] in {'shop_purchase', 'shop_return'}
            and (scene in REWARD or context.get('reward'))):
        returning = context['phase'] == 'shop_return'
        result = _reward_step(context, o, now, BOARD if returning else {'shop'})
        if result['status'] == 'completed':
            if returning:
                return _step('completed', evidence='shop_tail_rewards_returned')
            context['progress_at'] = now
            return _step()
        return result
    if kind in {'healing', 'item', 'vortex'}:
        flow = vortex if kind == 'vortex' else healing
        action, target = flow.step(context['flow'], o, now, event_option_policy=event_option_policy)
        if action in {'event_returned', 'vortex_returned'}:
            return _step('completed', evidence='node_flow_returned')
        if action in {'event_blocked', 'vortex_blocked'}:
            return _step('blocked', reason=context['flow'].get(prefix + '_error', action))
        if target is not None:
            return _step('input_requested', target=target, action=action)
        return _step()
    if not _stable(context, o, now):
        return _step()
    if kind == 'shop':
        return _shop_step(context, o, now)
    pending = context.get('pending')
    if pending:
        action = pending['action']
        if action == 'battle_start' and 'native' in pending and scene in {
                'battle_formation', 'native_choice_wait', 'unknown'}:
            # Active combat can remain unclassified until the result panel appears.
            if now - pending['at'] >= 180:
                return _step('blocked', reason='battle_start_transition_timeout')
            return _step()
        if action == 'battle_next':
            if scene in {'native_choice_wait', 'unknown'} or scene == 'battle_victory' and 'native' in pending:
                if now - context['progress_at'] >= 45:
                    return _step('blocked', reason='battle_victory_return_timeout')
                return _step()
            if scene == 'battle_victory' and o.get('native'):
                return _issue(context, action, choice_target(o, 'confirm', None), now)
        accepted = ((action == 'battle_start' and scene != 'battle_formation')
                    or (action == 'battle_next' and scene != 'battle_victory')
                    or (action == 'empty_enter_retry' and scene != 'event_entry'))
        if accepted:
            context['pending'] = None
            context['progress_at'] = now
        elif ((action == 'battle_start' and scene == 'battle_formation')
              or (action == 'battle_next' and scene == 'battle_victory')):
            return _retry(context, now)
        elif action == 'empty_enter_retry' and scene == 'event_entry':
            if now - pending['at'] < 2:
                return _step()
            if context.get('entry_clicks', 2) >= 3:
                return _step('blocked', reason='empty_entry_confirmation_limit')
            result = _retry(context, now)
            if result['status'] == 'input_requested':
                context['entry_clicks'] = context.get('entry_clicks', 2) + 1
            return result
    if kind == 'empty':
        if scene in BOARD and (o.get('player_turn') or enemy_board):
            if now - context['started'] >= 5 and context['stable'] >= 3:
                return _step('completed', evidence='empty_node_stable_board')
        elif scene == 'event_entry' and o.get('event_type') == 'empty' and not context.get('native_entry_submitted'):
            # The first entry click belongs to the parent move transaction.
            if now - context['progress_at'] >= 2:
                context['entry_clicks'] = 2
                return _issue(context, 'empty_enter_retry', o['click'], now)
        if now - context['started'] >= 30:
            return _step('blocked', reason='empty_node_return_timeout')
        return _step()
    if context['phase'] == 'entry_wait' and is_formation_page(o):
        if not context.get('formation_seen'):
            context.update(formation_seen=True, progress_at=now)
        if o.get('native_wait_reason'):
            context['formation_wait_reason'] = o['native_wait_reason']
        native = o.get('native') or {}
        expected_level = context.get('battle_level')
        level_matches = expected_level is None or expected_level == native.get('subject_id')
        if scene == 'battle_formation' and level_matches:
            native_kind = native.get('kind')
            if native_kind == 'formation_leave':
                return _step()
            if native_kind == 'formation_fill':
                if now - context['progress_at'] >= 45:
                    return _step('blocked', reason='formation_fill_no_progress')
                context['progress_at'] = now
                context['trace'].append(dict(action='formation_fill', at=now))
                return _step('input_requested', target=choice_target(o, 'confirm', None), action='formation_fill')
            target = choice_target(o, 'confirm', o.get('click'))
            if target is not None:
                context.update(phase='battle_wait', seen_content=True, progress_at=now)
                return _issue(context, 'battle_start', target, now)
    if context['phase'] in {'entry_wait', 'battle_wait'} and scene == 'battle_victory':
        context.update(victory=True, phase='battle_rewards', progress_at=now)
        return _issue(context, 'battle_next', choice_target(o, 'confirm', o.get('click')), now)
    if context['phase'] == 'battle_rewards':
        resume = BOARD | ({'event_ending'} if context['owner'] == 'vortex_sub_battle' else {'rest_area'})
        if scene in REWARD or context.get('reward'):
            return _reward_step(context, o, now, resume)
        if context['victory'] and scene in resume and now - context['stable_since'] >= 1:
            return _step('completed', evidence='battle_victory_and_return')
    phase_limit = 600 if context['phase'] == 'battle_wait' else 45
    if now - context['progress_at'] >= phase_limit:
        if context['phase'] == 'entry_wait' and context.get('formation_seen'):
            reason = context.get('formation_wait_reason')
            return _step('blocked', reason='formation_not_ready' + (':' + reason if reason else ''))
        return _step('blocked', reason='battle_no_progress:' + context['phase'])
    return _step()
