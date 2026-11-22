"""Offline interaction tests for selecting, limiting and confirming medicine batches."""
from itertools import count
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import _freight_fatigue_medicine as medicine


class MedicineGame:
    def __init__(self, monkeypatch, *, current=805, stock=None, per_batch=None,
                 selected=1, wrong_name=False, unchanged=False):
        self.current = current
        self.stock = {**dict.fromkeys(medicine.FATIGUE_MEDICINES, 12), **(stock or {})}
        self.per_batch = per_batch or {}
        self.default_selected = selected
        self.wrong_name = wrong_name
        self.unchanged = unchanged
        self.page = 'main'
        self.key = None
        self.selected = 0
        self.actions = []
        self.frames = count()
        monkeypatch.setattr(medicine, '_make_reader', lambda *args: self)
        monkeypatch.setattr(medicine, '_open_profile_panel', lambda *args: self.open_profile())
        monkeypatch.setattr(medicine, '_close_profile_panel_to_main', lambda *args: self.close_profile())
        monkeypatch.setattr(medicine, '_read_profile_fatigue', lambda *args: self.read_profile())
        monkeypatch.setattr(medicine, '_click', lambda reader, target, frame: self.click(target))
        monkeypatch.setattr(medicine, '_read_digits', lambda reader, frame, roi, pattern: self.ocr(roi))
        monkeypatch.setattr(medicine, '_sleep_with_cancel', lambda *args: None)
        monkeypatch.setattr(medicine, '_TIMEOUT', .1)

    def open_profile(self):
        self.page = 'profile'

    def close_profile(self):
        assert self.page == 'profile'
        self.page = 'main'

    def read_profile(self):
        assert self.page == 'profile'
        return {'current': self.current}

    def snapshot(self):
        return SimpleNamespace(id=next(self.frames))

    def is_page(self, page, *, frame):
        return self.page == page

    def move(self, source, target, control):
        assert source == self.page == 'profile' and control == 'fatigue_plus'
        self.page = target

    def restore_profile(self):
        assert self.page == 'fatigue_recovery'
        self.open_profile()

    def match(self, target, *, frame):
        actual_name = 'cactus' if self.wrong_name else self.key
        return SimpleNamespace(found=self.page == 'dialog' and target in {'quantity_title', f'name_{actual_name}'})

    def maximum(self):
        return min(self.stock[self.key], self.per_batch.get(self.key, 12))

    def preview(self):
        return max(0, self.current - self.selected * medicine._RECOVERY[self.key])

    def ocr(self, roi):
        if self.page == 'fatigue_recovery':
            return next((self.stock[key],) for key, (x, y, _, _) in medicine._CARDS.items()
                        if (x + 117, y + 130) == roi[:2])
        assert self.page == 'dialog'
        return {medicine._INVENTORY_ROI: (self.stock[self.key],), medicine._CURRENT_ROI: (self.current,),
                medicine._PREVIEW_ROI: (self.preview(),), medicine._SELECTION_ROI: (self.selected, self.maximum())}[roi]

    def click(self, target):
        if target.startswith('card_'):
            assert self.page == 'fatigue_recovery'
            self.key = target.removeprefix('card_')
            assert self.stock[self.key] > 0
            self.selected = min(self.default_selected, self.maximum())
            self.page = 'dialog'
            self.actions.append(('card', self.key))
        else:
            assert self.page == 'dialog'
            if target == 'confirm_button':
                self.actions.append(('replenish', self.key, self.selected, self.preview()))
                if not self.unchanged:
                    self.current = self.preview()
                    self.stock[self.key] -= self.selected
                self.open_profile()
            else:
                self.actions.append((target,))
                self.selected += {'quantity_plus_one': 1, 'quantity_minus_one': -1}[target]
                assert 0 < self.selected <= self.maximum()
        return True

    def run(self, *, stop=100, limits, start=600):
        return medicine.consume_fatigue_medicine(current=self.current, start=start, stop=stop, limits=limits,
                                                 app=None, ocr=None, vision=None)


def test_batches_obey_independent_caps_and_choose_smaller_safe_amount(monkeypatch):
    game = MedicineGame(monkeypatch, per_batch={'lollipop': 2})
    result = game.run(limits={'lollipop': 3, 'gum': 3, 'cactus': 9})
    assert result['success'] and result['final_fatigue'] == 325
    assert result['reason'] == 'fatigue_medicine_stop_floor'
    assert [(row['medicine_id'], row['count'], row['after']) for row in result['items']] == [
        ('lollipop', 2, 685), ('lollipop', 1, 625), ('gum', 3, 325),
    ]
    assert all(row['confirmed'] for row in result['items'])
    assert game.actions.count(('quantity_plus_one',)) == 3


def test_batch_hits_floor_exactly_and_skips_zero_stock(monkeypatch):
    game = MedicineGame(monkeypatch, current=700, stock={'lollipop': 0})
    result = game.run(limits={'lollipop': 12, 'gum': 12})
    assert result['success'] and result['final_fatigue'] == 100
    assert game.actions[0] == ('card', 'gum')
    assert game.actions[-1] == ('replenish', 'gum', 6, 100)
    assert len(result['items']) == 1


def test_existing_quantity_can_be_reduced_before_submitting(monkeypatch):
    game = MedicineGame(monkeypatch, selected=3)
    result = game.run(limits={'gum': 1})
    assert result['success'] and result['final_fatigue'] == 705
    assert game.actions == [('card', 'gum'), ('quantity_minus_one',), ('quantity_minus_one',),
                            ('replenish', 'gum', 1, 705)]


@pytest.mark.parametrize(('current', 'start', 'stop', 'reason'), [
    (600, 600, 100, 'below_start_threshold'), (620, 600, 590, 'fatigue_medicine_stop_floor'),
])
def test_no_submission_when_threshold_or_stop_floor_disallows_use(monkeypatch, current, start, stop, reason):
    game = MedicineGame(monkeypatch, current=current)
    result = game.run(limits={'lollipop': 12, 'gum': 12, 'cactus': 12}, start=start, stop=stop)
    assert result['success'] and result['reason'] == reason
    assert result['final_fatigue'] == current and game.actions == []


def test_cactus_can_reach_a_zero_floor_without_overselecting(monkeypatch):
    game = MedicineGame(monkeypatch, current=650)
    result = game.run(limits={'cactus': 5}, stop=0)
    assert result['success'] and result['final_fatigue'] == 0
    assert game.actions[-1] == ('replenish', 'cactus', 1, 0)


def test_unconfirmed_submission_keeps_the_pending_batch_without_retry(monkeypatch):
    game = MedicineGame(monkeypatch, unchanged=True)
    result = game.run(limits={'gum': 4})
    assert result['reason'] == 'fatigue_medicine_verify_batch_failed' and result['final_fatigue'] is None
    assert len(result['items']) == 1
    assert result['items'][0]['submitted'] and not result['items'][0]['confirmed']
    assert game.actions.count(('replenish', 'gum', 4, 405)) == 1


def test_wrong_quantity_page_medicine_never_submits(monkeypatch):
    game = MedicineGame(monkeypatch, wrong_name=True)
    result = game.run(limits={'gum': 4})
    assert result['reason'] == 'fatigue_medicine_select_card_failed'
    assert result['items'] == [] and game.actions == [('card', 'gum')]
