"""Deep Dive interval controls shared by both play entry points."""
import json

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout,
)

from packages.resonance_shared.deep_dive_interval import (
    INTERVAL_OPERATIONS, default_interval_policy, normalize_interval_policy,
)


_LABELS = {'revive': '队员复苏', 'intensify': '推力强化', 'awake': '觉醒升构', 'recruit': '队员招募'}
_SETTINGS_KEY = 'deep_dive/interval_policy_json'


class DeepDiveIntervalSettings(QGroupBox):
    def __init__(self, parent=None, *, settings=None):
        super().__init__('位面间隙设置', parent)
        self.setObjectName('deepDiveIntervalSettings')
        self._settings = settings
        self._operations = {}
        self._order = []
        layout = QVBoxLayout(self)
        title = QLabel('操作优先级', self)
        title.setObjectName('deepDiveIntervalSectionTitle')
        layout.addWidget(title)
        guide = QLabel('按顺位从上到下执行，跳过未勾选的操作。', self)
        guide.setWordWrap(True)
        layout.addWidget(guide)
        self._rows = QVBoxLayout()
        self._rows.setSpacing(8)
        layout.addLayout(self._rows)
        for key in INTERVAL_OPERATIONS:
            self._add_operation(key)

        party_panel = QFrame(self)
        party_panel.setObjectName('deepDiveIntervalParty')
        party_layout = QVBoxLayout(party_panel)
        party_layout.setContentsMargins(10, 6, 10, 6)
        party_title = QLabel('出战队伍设置', party_panel)
        party_title.setObjectName('deepDiveIntervalSectionTitle')
        party_layout.addWidget(party_title)
        party_controls = QHBoxLayout()
        self.fill_party = QCheckBox('候补补员', party_panel)
        self.fill_party.setToolTip('出战位有空缺时，按稀有度从高到低使用本局已有的存活候补。')
        self.fixed_party = QCheckBox('一队打到死', party_panel)
        self.fixed_party.setToolTip('固定原始出战队伍。若无法通过复苏维持原阵容，结算并重开。')
        party_controls.addWidget(self.fill_party)
        party_controls.addWidget(self.fixed_party)
        party_controls.addStretch()
        party_layout.addLayout(party_controls)
        layout.addWidget(party_panel)
        self._load()
        self.fill_party.toggled.connect(self._save)
        self.fixed_party.toggled.connect(self._save)
        for _, controls, _, _, _ in self._operations.values():
            for widget in controls.values():
                if isinstance(widget, QCheckBox):
                    widget.toggled.connect(self._save)
                elif isinstance(widget, QComboBox):
                    widget.currentIndexChanged.connect(self._save)
                else:
                    widget.valueChanged.connect(self._save)

    def _add_operation(self, key):
        row = QFrame(self)
        row.setObjectName('deepDiveIntervalOperation')
        layout = QVBoxLayout(row)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(3)
        heading = QHBoxLayout()
        heading.setContentsMargins(6, 1, 1, 1)
        rank = QLabel(row)
        rank.setObjectName('deepDiveIntervalRank')
        heading.addWidget(rank)
        enabled = QCheckBox(_LABELS[key], row)
        enabled.setObjectName('deepDiveIntervalOperationName')
        heading.addWidget(enabled)
        heading.addStretch()
        up = QPushButton('上移', row)
        down = QPushButton('下移', row)
        up.setToolTip(f'提高{_LABELS[key]}的优先级')
        down.setToolTip(f'降低{_LABELS[key]}的优先级')
        up.clicked.connect(lambda: self._move(key, -1))
        down.clicked.connect(lambda: self._move(key, 1))
        heading.addWidget(up)
        heading.addWidget(down)
        layout.addLayout(heading)

        details = QFrame(row)
        details.setObjectName('deepDiveIntervalOperationOptions')
        form = QFormLayout(details)
        form.setContentsMargins(12, 5, 8, 5)
        limit = QSpinBox(details)
        limit.setRange(0, 2_147_483_647)
        limit.setSpecialValueText('0（不限）')
        form.addRow('本次上限', limit)
        controls = {'enabled': enabled, 'limit': limit}
        if key == 'intensify':
            recommended = QCheckBox('只选择推荐推力', details)
            controls['recommended_only'] = recommended
            form.addRow(recommended)
        if key in {'intensify', 'recruit'}:
            prefix = '价格' if key == 'intensify' else '等级'
            order = QComboBox(details)
            order.addItem(f'{prefix}从低到高', 'low')
            order.addItem(f'{prefix}从高到低', 'high')
            controls['order'] = order
            form.addRow('购买顺序' if key == 'intensify' else '招募顺序', order)
        if key == 'awake':
            mode = QComboBox(details)
            mode.addItem('优先升级', 'focused')
            mode.addItem('平铺升级', 'even')
            max_level = QSpinBox(details)
            max_level.setRange(1, 5)
            controls.update(mode=mode, max_level=max_level)
            form.addRow('升构方式', mode)
            form.addRow('目标觉醒等级', max_level)
        layout.addWidget(details)
        self._operations[key] = row, controls, up, down, rank

    def _show_order(self):
        for index, key in enumerate(self._order):
            row, _, up, down, rank = self._operations[key]
            self._rows.removeWidget(row)
            self._rows.insertWidget(index, row)
            rank.setText(f'第 {index + 1} 顺位')
            up.setEnabled(index > 0)
            down.setEnabled(index < len(self._order) - 1)

    def _move(self, key, direction):
        index = self._order.index(key)
        other = index + direction
        if 0 <= other < len(self._order):
            self._order[index], self._order[other] = self._order[other], self._order[index]
            self._show_order()
            self._save()

    def _load(self):
        policy = default_interval_policy()
        if self._settings is not None:
            try:
                policy = normalize_interval_policy(json.loads(self._settings.value(_SETTINGS_KEY, '{}')))
            except (ValueError, TypeError):
                pass
        self._order = policy['order']
        self._show_order()
        for key, (_, controls, _, _, _) in self._operations.items():
            for field, widget in controls.items():
                value = policy[key][field]
                if isinstance(widget, QCheckBox):
                    widget.setChecked(value)
                elif isinstance(widget, QComboBox):
                    widget.setCurrentIndex(widget.findData(value))
                else:
                    widget.setValue(value)
        self.fill_party.setChecked(policy['fill_party'])
        self.fixed_party.setChecked(policy['fixed_party'])

    def policy(self):
        policy = dict(order=list(self._order), fill_party=self.fill_party.isChecked(),
                      fixed_party=self.fixed_party.isChecked())
        for key, (_, controls, _, _, _) in self._operations.items():
            policy[key] = {
                field: widget.isChecked() if isinstance(widget, QCheckBox)
                else widget.currentData() if isinstance(widget, QComboBox)
                else widget.value()
                for field, widget in controls.items()
            }
        return policy

    def _save(self):
        if self._settings is not None:
            self._settings.set_value(_SETTINGS_KEY, json.dumps(self.policy()))
