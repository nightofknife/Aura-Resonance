"""Editor for priorities shared by an event's variants and follow-up pages."""
import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from packages.resonance_shared.deep_dive_event_options import (
    load_event_option_groups, normalize_event_option_policy, supports_native_event_option,
)

from ..paths import resolve_application_root


_SETTINGS_KEY = 'deep_dive/event_option_policy_json'
_DATA = Qt.ItemDataRole.UserRole
_PRIORITY_COLUMN = 0
_ALLOWED_COLUMN = 1


class DeepDiveEventSettings(QGroupBox):
    def __init__(self, parent=None, *, settings=None):
        super().__init__('事件选项优先级', parent)
        self._settings = settings
        self._policy = []
        if settings is not None:
            try:
                self._policy = normalize_event_option_policy(json.loads(settings.value(_SETTINGS_KEY, '[]')))
            except (TypeError, ValueError):
                pass
        layout = QHBoxLayout(self)
        edit = QPushButton('编辑事件选项…', self)
        edit.clicked.connect(self._edit)
        layout.addWidget(edit)
        self._summary = QLabel(self)
        layout.addWidget(self._summary, 1)
        self._refresh_summary()

    def policy(self):
        return normalize_event_option_policy(self._policy)

    def _refresh_summary(self):
        self._summary.setText(f'已自定义 {len(self._policy)} 个事件' if self._policy else '使用默认顺序')

    def _edit(self):
        try:
            groups = load_event_option_groups(resolve_application_root() / 'plans/resonance_pc')
        except (OSError, KeyError, TypeError, ValueError):
            QMessageBox.warning(self, '事件选项优先级', '无法读取识海事件目录。')
            return
        dialog = EventOptionDialog(groups, self.policy(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._policy = dialog.policy()
            if self._settings is not None:
                self._settings.set_value(_SETTINGS_KEY, json.dumps(self._policy, ensure_ascii=False))
            self._refresh_summary()
        dialog.deleteLater()


class EventOptionDialog(QDialog):
    def __init__(self, groups, policy, parent=None):
        super().__init__(parent)
        self.setWindowTitle('识海事件选项优先级')
        self.resize(940, 560)
        self._groups = groups
        self._policy = {entry['name']: entry for entry in policy}
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        heading.addWidget(QLabel('事件', self))
        self.event = QComboBox(self)
        self.event.addItems(groups)
        heading.addWidget(self.event, 1)
        self.mode = QLabel(self)
        heading.addWidget(self.mode)
        layout.addLayout(heading)
        guide = QLabel('数字越小越优先。每页按顺序选择第一个允许且可用的选项。', self)
        guide.setWordWrap(True)
        layout.addWidget(guide)

        headers = ['优先级', '允许', '选项', '效果和代价', '出现页面']
        self.options = QTableWidget(0, len(headers), self)
        self.options.setHorizontalHeaderLabels(headers)
        self.options.setAlternatingRowColors(True)
        self.options.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.options.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.options.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.options.verticalHeader().hide()
        header = self.options.horizontalHeader()
        header.setSectionResizeMode(_PRIORITY_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_ALLOWED_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.options.setColumnWidth(2, 205)
        self.options.setColumnWidth(4, 220)
        layout.addWidget(self.options, 1)

        actions = QHBoxLayout()
        self.up = QPushButton('提高优先级', self)
        self.down = QPushButton('降低优先级', self)
        self.reset = QPushButton('恢复此事件默认', self)
        self.up.clicked.connect(lambda: self._move(-1))
        self.down.clicked.connect(lambda: self._move(1))
        self.reset.clicked.connect(self._reset)
        for widget in (self.up, self.down, self.reset):
            actions.addWidget(widget)
        actions.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('保存')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)
        layout.addLayout(actions)
        self.event.currentIndexChanged.connect(self._populate)
        self.options.itemSelectionChanged.connect(self._sync_buttons)
        self.options.itemChanged.connect(self._set_allowed)
        self._populate()

    def policy(self):
        return normalize_event_option_policy(list(self._policy.values()))

    def _entry(self):
        name = self.event.currentText()
        return self._policy.setdefault(name, {'name': name, 'order': [], 'disabled': []})

    def _populate(self, *_):
        name = self.event.currentText()
        options = {option['id']: option for option in self._groups.get(name, [])}
        policy = self._policy.get(name, {})
        ranks = {id_: index for index, id_ in enumerate(policy.get('order', []))}
        rows = sorted(options.values(), key=lambda option: ranks.get(option['id'], len(ranks)))
        self.mode.setText('自定义顺序' if ranks else '默认顺序')
        disabled = set(policy.get('disabled', []))
        self.options.blockSignals(True)
        self.options.setRowCount(len(rows))
        for index, option in enumerate(rows):
            priority = QTableWidgetItem(str(index + 1))
            priority.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            priority.setBackground(QColor('#e0e8da'))
            self.options.setItem(index, _PRIORITY_COLUMN, priority)
            supported = supports_native_event_option(option)
            allowed = QTableWidgetItem()
            allowed.setData(_DATA, option['id'])
            allowed.setFlags(Qt.ItemFlag.ItemIsSelectable | (
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable if supported else Qt.ItemFlag.NoItemFlags))
            allowed.setCheckState(Qt.CheckState.Checked if supported and option['id'] not in disabled
                                  else Qt.CheckState.Unchecked)
            if not supported:
                reason = '行动规则限制' if option.get('blocked') else '、'.join(option.get('unsupported_reasons', ()))
                allowed.setToolTip(reason or '尚未适配')
            self.options.setItem(index, _ALLOWED_COLUMN, allowed)
            values = (option['title'], option.get('description') or '—', option['variant'])
            for column, value in enumerate(values, _ALLOWED_COLUMN + 1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.options.setItem(index, column, item)
        self.options.resizeRowsToContents()
        self.options.clearSelection()
        self.options.setCurrentItem(None)
        self.options.blockSignals(False)
        self._sync_buttons()

    def _set_allowed(self, item):
        if item.column() != _ALLOWED_COLUMN or not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        entry = self._entry()
        disabled = set(entry['disabled'])
        id_ = item.data(_DATA)
        if item.checkState() == Qt.CheckState.Checked:
            disabled.discard(id_)
        else:
            disabled.add(id_)
        entry['disabled'] = sorted(disabled)
        self._sync_buttons()

    def _move(self, offset):
        current = self.options.currentRow()
        other = current + offset
        if not 0 <= current < self.options.rowCount() or not 0 <= other < self.options.rowCount():
            return
        ids = [self.options.item(row, _ALLOWED_COLUMN).data(_DATA) for row in range(self.options.rowCount())]
        ids[current], ids[other] = ids[other], ids[current]
        self._entry()['order'] = ids
        self._populate()
        self.options.selectRow(other)

    def _reset(self):
        self._policy.pop(self.event.currentText(), None)
        self._populate()

    def _sync_buttons(self):
        row = self.options.currentRow()
        self.up.setEnabled(row > 0)
        self.down.setEnabled(0 <= row < self.options.rowCount() - 1)
        entry = self._policy.get(self.event.currentText(), {})
        self.reset.setEnabled(bool(entry.get('order') or entry.get('disabled')))
