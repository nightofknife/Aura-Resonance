"""Purchase settings shared by both Deep Dive entry points."""
import json

from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QGroupBox, QSpinBox

from packages.resonance_shared.deep_dive_shop import normalize_shop_policy


_SETTINGS_KEY = 'deep_dive/shop_policy_json'


class DeepDiveShopSettings(QGroupBox):
    def __init__(self, parent=None, *, settings=None):
        super().__init__('商店设置', parent)
        self._settings = settings
        policy = normalize_shop_policy()
        if settings is not None:
            try:
                policy = normalize_shop_policy(json.loads(settings.value(_SETTINGS_KEY, '{}')))
            except (TypeError, ValueError):
                pass

        form = QFormLayout(self)
        self.enabled = QCheckBox('允许商店购买', self)
        self.enabled.setChecked(policy['enabled'])
        form.addRow(self.enabled)
        self.recommended_only = QCheckBox('只买推荐商品', self)
        self.recommended_only.setChecked(policy['recommended_only'])
        form.addRow(self.recommended_only)
        self.order = QComboBox(self)
        self.order.addItem('价格从低到高', 'low')
        self.order.addItem('价格从高到低', 'high')
        self.order.setCurrentIndex(self.order.findData(policy['order']))
        form.addRow('购买顺序', self.order)
        self.limit = QSpinBox(self)
        self.limit.setRange(0, 2_147_483_647)
        self.limit.setSpecialValueText('0（不限）')
        self.limit.setValue(policy['limit'])
        form.addRow('单次进店购买上限', self.limit)

        self._enable_options(policy['enabled'])
        self.enabled.toggled.connect(self._enable_options)
        self.enabled.toggled.connect(self._save)
        self.recommended_only.toggled.connect(self._save)
        self.order.currentIndexChanged.connect(self._save)
        self.limit.valueChanged.connect(self._save)

    def _enable_options(self, enabled):
        for widget in (self.recommended_only, self.order, self.limit):
            widget.setEnabled(enabled)

    def policy(self):
        return dict(enabled=self.enabled.isChecked(), recommended_only=self.recommended_only.isChecked(),
                    order=self.order.currentData(), limit=self.limit.value())

    def _save(self):
        if self._settings is not None:
            self._settings.set_value(_SETTINGS_KEY, json.dumps(self.policy()))
