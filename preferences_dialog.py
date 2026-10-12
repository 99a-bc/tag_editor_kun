from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from settings import (
    AppSettings,
    DEFAULT_TAG_COMPLETION_LIMIT,
    DEFAULT_THUMBNAIL_WHEEL_SCROLL_PX,
    MAX_TAG_COMPLETION_LIMIT,
    MAX_THUMBNAIL_WHEEL_SCROLL_PX,
    MIN_TAG_COMPLETION_LIMIT,
    MIN_THUMBNAIL_WHEEL_SCROLL_PX,
    THUMBNAIL_WHEEL_SCROLL_STEP,
    normalize_recursive_load_depth,
    normalize_tag_completion_limit,
    normalize_thumbnail_wheel_scroll_px,
)
from theme import ADD_TAG_DIALOG_QSS


class PreferencesDialog(QDialog):
    """アプリ環境設定ダイアログ。"""

    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("環境設定")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(ADD_TAG_DIALOG_QSS)
        # ダイアログにないTagger設定などを、OK時にも失わないための基準値。
        self._base_settings = AppSettings.from_dict(settings.to_dict())
        self._recursive_load_enabled = settings.recursive_load_enabled
        self._recursive_load_depth = normalize_recursive_load_depth(settings.recursive_load_depth)

        card = QFrame()
        card.setObjectName("DialogCard")

        title_label = QLabel("環境設定")
        title_label.setObjectName("DialogTitle")

        hint_label = QLabel(
            "サムネイル欄の操作感、フォルダ読み込み設定、タグ表示・候補表示設定を変更できます。"
        )
        hint_label.setObjectName("DialogHint")
        hint_label.setWordWrap(True)

        self.thumbnail_wheel_spin = QSpinBox()
        self.thumbnail_wheel_spin.setRange(
            MIN_THUMBNAIL_WHEEL_SCROLL_PX,
            MAX_THUMBNAIL_WHEEL_SCROLL_PX,
        )
        self.thumbnail_wheel_spin.setSingleStep(THUMBNAIL_WHEEL_SCROLL_STEP)
        self.thumbnail_wheel_spin.setSuffix(" px / 1ノッチ")
        self.thumbnail_wheel_spin.setValue(
            normalize_thumbnail_wheel_scroll_px(settings.thumbnail_wheel_scroll_px)
        )
        self.thumbnail_wheel_spin.setToolTip(
            "サムネイル欄でマウスホイールを1ノッチ動かした時のスクロール量です。"
        )

        default_label = QLabel(f"既定値: {DEFAULT_THUMBNAIL_WHEEL_SCROLL_PX} px")
        default_label.setObjectName("DialogHint")

        self.remember_recursive_check = QCheckBox("サブフォルダ読み込み設定を記憶する")
        self.remember_recursive_check.setChecked(settings.remember_recursive_load)
        self.remember_recursive_check.setToolTip(
            "ONにすると、サムネイル欄フッターのサブフォルダ読み込みON/OFFと深さを"
            "次回起動時も引き継ぎます。OFFの場合、起動時は常にOFFになります。"
        )

        remember_recursive_hint = QLabel(
            "ONにした場合、現在のサブフォルダ読み込みON/OFFと深さ設定を保存します。"
            "深さ0は全階層を意味します。"
        )
        remember_recursive_hint.setObjectName("DialogHint")
        remember_recursive_hint.setWordWrap(True)

        self.tag_color_check = QCheckBox("タグカテゴリ色分けを有効にする")
        self.tag_color_check.setChecked(settings.tag_color_enabled)
        self.tag_color_check.setToolTip(
            "ONにすると、タグ色分け辞書のカテゴリに応じてタグチップの色を変えます。"
        )

        tag_color_hint = QLabel(
            "辞書が見つからない場合や壊れている場合は、通常色で表示します。"
        )
        tag_color_hint.setObjectName("DialogHint")
        tag_color_hint.setWordWrap(True)

        self.tag_preset_compact_check = QCheckBox("プリセットタグを詰めて表示する")
        self.tag_preset_compact_check.setChecked(settings.tag_preset_compact_enabled)
        self.tag_preset_compact_check.setToolTip(
            "ONにすると、タグプリセット画面のチェック付きタグを横方向に詰めて折り返して表示します。"
        )

        tag_preset_compact_hint = QLabel(
            "OFFの場合は1タグ1行で表示します。設定変更後はプリセット画面へ即時反映されます。"
        )
        tag_preset_compact_hint.setObjectName("DialogHint")
        tag_preset_compact_hint.setWordWrap(True)

        self.tag_completion_check = QCheckBox("Danbooruタグ候補を表示する")
        self.tag_completion_check.setChecked(settings.tag_completion_enabled)
        self.tag_completion_check.setToolTip(
            "タグ追加・置換ダイアログで、入力中のタグ候補を表示します。"
        )

        self.tag_completion_limit_label = QLabel("候補の表示数")
        self.tag_completion_limit_label.setObjectName("DialogHint")
        self.tag_completion_limit_spin = QSpinBox()
        self.tag_completion_limit_spin.setRange(
            MIN_TAG_COMPLETION_LIMIT,
            MAX_TAG_COMPLETION_LIMIT,
        )
        self.tag_completion_limit_spin.setSingleStep(1)
        self.tag_completion_limit_spin.setSuffix(" 件")
        self.tag_completion_limit_spin.setValue(
            normalize_tag_completion_limit(settings.tag_completion_limit)
        )
        self.tag_completion_limit_spin.setToolTip(
            "入力中に表示するDanbooruタグ候補の最大件数です。"
        )

        tag_completion_default_label = QLabel(
            f"既定値: {DEFAULT_TAG_COMPLETION_LIMIT} 件"
        )
        tag_completion_default_label.setObjectName("DialogHint")

        form_layout = QGridLayout()
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setHorizontalSpacing(12)
        form_layout.setVerticalSpacing(8)

        wheel_label = QLabel("サムネイル欄のホイールスクロール量")
        wheel_label.setObjectName("DialogHint")
        form_layout.addWidget(wheel_label, 0, 0)
        form_layout.addWidget(self.thumbnail_wheel_spin, 0, 1)
        form_layout.addWidget(default_label, 1, 1)
        form_layout.addWidget(self.remember_recursive_check, 2, 0, 1, 2)
        form_layout.addWidget(remember_recursive_hint, 3, 0, 1, 2)
        form_layout.addWidget(self.tag_color_check, 4, 0, 1, 2)
        form_layout.addWidget(tag_color_hint, 5, 0, 1, 2)
        form_layout.addWidget(self.tag_preset_compact_check, 6, 0, 1, 2)
        form_layout.addWidget(tag_preset_compact_hint, 7, 0, 1, 2)
        form_layout.addWidget(self.tag_completion_check, 8, 0, 1, 2)
        form_layout.addWidget(self.tag_completion_limit_label, 9, 0)
        form_layout.addWidget(self.tag_completion_limit_spin, 9, 1)
        form_layout.addWidget(tag_completion_default_label, 10, 1)

        self.tag_completion_check.toggled.connect(self._update_tag_completion_controls)
        self._update_tag_completion_controls(self.tag_completion_check.isChecked())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)
        card_layout.addWidget(title_label)
        card_layout.addWidget(hint_label)
        card_layout.addLayout(form_layout)
        card_layout.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(card)

    def _update_tag_completion_controls(self, enabled: bool) -> None:
        self.tag_completion_limit_label.setEnabled(enabled)
        self.tag_completion_limit_spin.setEnabled(enabled)

    def settings(self) -> AppSettings:
        # 環境設定の対象外であるTagger設定などは、開いた時点の値を維持する。
        next_settings = AppSettings.from_dict(self._base_settings.to_dict())
        next_settings.thumbnail_wheel_scroll_px = normalize_thumbnail_wheel_scroll_px(
            self.thumbnail_wheel_spin.value()
        )
        next_settings.remember_recursive_load = self.remember_recursive_check.isChecked()
        next_settings.recursive_load_enabled = self._recursive_load_enabled
        next_settings.recursive_load_depth = normalize_recursive_load_depth(
            self._recursive_load_depth
        )
        next_settings.tag_color_enabled = self.tag_color_check.isChecked()
        next_settings.tag_preset_compact_enabled = self.tag_preset_compact_check.isChecked()
        next_settings.tag_completion_enabled = self.tag_completion_check.isChecked()
        next_settings.tag_completion_limit = normalize_tag_completion_limit(
            self.tag_completion_limit_spin.value()
        )
        return next_settings
