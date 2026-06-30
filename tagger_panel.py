from __future__ import annotations

from pathlib import Path
import time

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tagger.result import TagCandidate, TaggerResult
from tag_color_dictionary import DEFAULT_UNKNOWN_CATEGORY, TagColorDictionary
from dev_config import dev_log_enabled
from logger import get_logger


log = get_logger(__name__)

PIXAI_MODEL_REPO = "pixai-labs/pixai-tagger-v0.9"


TAGGER_PANEL_QSS = """
QFrame#TaggerCard {
    background: #242424;
    border: 1px solid #4b4b4b;
    border-radius: 8px;
}
QLabel#TaggerSectionTitle {
    color: #111111;
    background: #80e6a6;
    border-radius: 7px;
    padding: 4px 12px;
    font-weight: bold;
}
QLabel#TaggerHint {
    color: #cfcfcf;
}
QLabel#TaggerStatus {
    color: #9be9b5;
    font-weight: bold;
}
QLabel#TaggerThresholdLabel:disabled {
    color: #777777;
}
QProgressBar {
    border: 1px solid #555555;
    border-radius: 4px;
    background: #151515;
    text-align: center;
    min-height: 12px;
    max-height: 12px;
}
QProgressBar::chunk {
    background: #de7b35;
    border-radius: 3px;
}
"""


class _PrefixElidedLabel(QLabel):
    """
    表示幅に応じて、prefixを残しながら本文を中央省略するラベル。

    例:
        現在画像: very_long_..._final.png
        Tagger実行中: 42/104: very_long_..._final.png
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._preserved_prefix = ""
        self.setWordWrap(False)

        # 長い元文字列がQLabel本来のsizeHintへ反映されると、親のTaggerパネルが
        # 横方向へ広がってしまう。Ignoredと幅0のsizeHintで、レイアウト上は
        # 「残り領域を受け取るだけ」のラベルとして扱わせる。
        self.setMinimumSize(0, 0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """長い表示文字列を親レイアウトの最小幅へ伝えない。"""
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:  # noqa: N802
        """長い表示文字列を親レイアウトの推奨幅へ伝えない。"""
        return QSize(0, super().sizeHint().height())

    def set_full_text(self, text: str, *, preserved_prefix: str = "") -> None:
        self._full_text = str(text)
        self._preserved_prefix = (
            preserved_prefix
            if preserved_prefix and self._full_text.startswith(preserved_prefix)
            else ""
        )
        self.setToolTip(self._full_text)
        self._refresh_elided_text()

        # set_full_textの時点では、親レイアウトが新しい残り幅を配分する前の場合がある。
        # 次のイベントループでも描き直し、実行ボタン群の左端までの幅へ確実に合わせる。
        QTimer.singleShot(0, self._refresh_elided_text)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refresh_elided_text()

    def _refresh_elided_text(self) -> None:
        if not self._full_text:
            super().setText("")
            return

        available_width = max(0, self.contentsRect().width())
        metrics = self.fontMetrics()

        if self._preserved_prefix:
            prefix_width = metrics.horizontalAdvance(self._preserved_prefix)
            if available_width > prefix_width:
                remaining_text = self._full_text[len(self._preserved_prefix):]
                remaining_width = max(0, available_width - prefix_width)
                display_text = self._preserved_prefix + metrics.elidedText(
                    remaining_text,
                    Qt.TextElideMode.ElideMiddle,
                    remaining_width,
                )
            else:
                display_text = metrics.elidedText(
                    self._full_text,
                    Qt.TextElideMode.ElideRight,
                    available_width,
                )
        else:
            display_text = metrics.elidedText(
                self._full_text,
                Qt.TextElideMode.ElideMiddle,
                available_width,
            )

        super().setText(display_text)


class TaggerPanel(QWidget):
    """WD Tagger用タブのUI。推論処理そのものは持たず、AppWindowへSignalで依頼する。"""

    run_requested = Signal(dict)
    reset_requested = Signal()
    apply_requested = Signal(str)
    cancel_requested = Signal()
    settings_changed = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.items = []
        self.current_index = -1
        self._running = False
        self._cancelling = False
        self._restoring_settings = False
        # 同じ結果を進捗通知のたびに再描画しないための表示キー。
        self._candidate_view_key: tuple[object, ...] | None = None
        self._current_display_result: TaggerResult | None = None
        self._tag_color_dictionary = TagColorDictionary.load()
        self._tag_color_enabled = True

        self.setStyleSheet(TAGGER_PANEL_QSS)

        self.run_button = QPushButton("実行")
        self.reset_button = QPushButton("リセット")
        self.cancel_button = QPushButton("中止")
        self.run_button.setMinimumWidth(72)
        self.reset_button.setMinimumWidth(72)
        self.cancel_button.setMinimumWidth(72)
        self.cancel_button.setToolTip(
            "Tagger用子プロセスを終了して、実行中の推論を中止します。"
        )
        # 中止ボタンは常に場所を確保する。通常時は無効でグレーアウト表示にする。
        self.cancel_button.setEnabled(False)
        self.run_button.clicked.connect(self._emit_run_requested)
        self.reset_button.clicked.connect(self.reset_requested.emit)
        self.cancel_button.clicked.connect(self._emit_cancel_requested)

        action_layout = QHBoxLayout()
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(10)
        action_layout.addWidget(self.run_button)
        action_layout.addWidget(self.reset_button)
        action_layout.addWidget(self.cancel_button)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(False)
        self.model_combo.addItem(
            "WD EVA02 Large Tagger v3",
            "SmilingWolf/wd-eva02-large-tagger-v3",
        )
        self.model_combo.addItem(
            "PixAI Tagger v0.9",
            "pixai-labs/pixai-tagger-v0.9",
        )
        self.model_combo.setCurrentIndex(0)
        self.model_combo.setToolTip(
            "使用するTaggerモデルを選択します。\n"
            "PixAI Tagger v0.9は、事前にHugging Faceのアクセス許可と"
            "モデル取得が必要です。"
        )

        self.device_combo = QComboBox()
        self.device_combo.addItem("Auto", "auto")
        self.device_combo.addItem("CPU", "cpu")
        self.device_combo.addItem("GPU", "gpu")

        left_card = QFrame()
        left_card.setObjectName("TaggerCard")
        left_layout = QGridLayout(left_card)
        left_layout.setContentsMargins(12, 10, 12, 10)
        left_layout.setHorizontalSpacing(10)
        left_layout.setVerticalSpacing(8)
        left_layout.addWidget(QLabel("モデル"), 0, 0)
        left_layout.addWidget(self.model_combo, 0, 1)
        left_layout.addWidget(QLabel("推論実行デバイス"), 1, 0)
        left_layout.addWidget(self.device_combo, 1, 1)
        left_layout.setColumnStretch(1, 1)

        threshold_tooltip = (
            "この値以上の推定スコアのタグを候補に表示します。\n"
            "値を上げると候補は少なくなり、下げると候補は増えます。"
        )

        self.include_general_check = QCheckBox()
        self.include_general_check.setChecked(True)
        self.include_general_check.setToolTip("一般タグを候補に含める")
        self.include_general_check.toggled.connect(self._on_category_included_toggled)

        self.general_threshold_label = QLabel("一般タグ")
        self.general_threshold_label.setObjectName("TaggerThresholdLabel")
        self.general_threshold_label.setToolTip(threshold_tooltip)
        # WD/PixAIで％欄の位置を固定するため、タグ名列を共通幅にする。
        self.general_threshold_label.setFixedWidth(130)
        self.general_threshold_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self.general_threshold_spin = QDoubleSpinBox()
        self.general_threshold_spin.setRange(0.0, 100.0)
        self.general_threshold_spin.setSingleStep(5.0)
        self.general_threshold_spin.setDecimals(0)
        self.general_threshold_spin.setSuffix("%")
        self.general_threshold_spin.setValue(35.0)
        self.general_threshold_spin.setToolTip(threshold_tooltip)
        self.general_threshold_spin.setMaximumWidth(110)
        self.general_threshold_spin.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )

        self.include_character_check = QCheckBox()
        self.include_character_check.setToolTip("キャラクタータグを候補に含める")
        self.include_character_check.toggled.connect(self._on_category_included_toggled)

        self.character_threshold_label = QLabel("キャラクタータグ")
        self.character_threshold_label.setObjectName("TaggerThresholdLabel")
        self.character_threshold_label.setToolTip(threshold_tooltip)
        # 一般タグ行と同じタグ名列幅にして、％欄の開始位置をそろえる。
        self.character_threshold_label.setFixedWidth(130)
        self.character_threshold_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self.character_threshold_spin = QDoubleSpinBox()
        self.character_threshold_spin.setRange(0.0, 100.0)
        self.character_threshold_spin.setSingleStep(5.0)
        self.character_threshold_spin.setDecimals(0)
        self.character_threshold_spin.setSuffix("%")
        self.character_threshold_spin.setValue(85.0)
        self.character_threshold_spin.setToolTip(threshold_tooltip)
        self.character_threshold_spin.setMaximumWidth(110)
        self.character_threshold_spin.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )

        # Windows標準スタイルではdisabledパレットが上下矢印へ反映されない場合がある。
        # 形状を描き直さず、コントロール全体だけを半透明化して確実に視覚的な無効状態を示す。
        self._general_threshold_opacity = QGraphicsOpacityEffect(
            self.general_threshold_spin
        )
        self._general_threshold_opacity.setOpacity(1.0)
        self.general_threshold_spin.setGraphicsEffect(self._general_threshold_opacity)

        self._character_threshold_opacity = QGraphicsOpacityEffect(
            self.character_threshold_spin
        )
        self._character_threshold_opacity.setOpacity(1.0)
        self.character_threshold_spin.setGraphicsEffect(
            self._character_threshold_opacity
        )

        self.include_copyright_check = QCheckBox("作品タグを含める")
        self.include_copyright_check.setToolTip(
            "PixAIのキャラクター候補から逆引きした作品タグを、"
            "候補表示と反映対象に含めます。モデルが直接予測したタグではありません。"
        )
        self.include_copyright_check.setStyleSheet(
            "font-size: 11px; padding-left: 2px;"
        )
        self.include_copyright_check.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )
        self.include_copyright_check.toggled.connect(
            self._on_copyright_included_toggled
        )

        right_card = QFrame()
        right_card.setObjectName("TaggerCard")
        right_layout = QGridLayout(right_card)
        right_layout.setContentsMargins(12, 10, 12, 10)
        right_layout.setHorizontalSpacing(10)
        right_layout.setVerticalSpacing(8)

        threshold_title = QLabel("候補に出す最低スコア")
        threshold_title.setStyleSheet("font-weight: bold; color: #f0f0f0;")
        threshold_title.setToolTip(threshold_tooltip)

        # 各行は独立した横レイアウトにする。
        # PixAIの作品タグチェックはキャラクター行の末尾へ追加するだけなので、
        # WD / PixAIのどちらでも「チェック → タグ名 → %」の位置は共通になる。
        general_row = QWidget()
        general_row_layout = QHBoxLayout(general_row)
        general_row_layout.setContentsMargins(0, 0, 0, 0)
        general_row_layout.setSpacing(10)
        general_row_layout.addWidget(self.include_general_check)
        general_row_layout.addWidget(self.general_threshold_label)
        general_row_layout.addWidget(self.general_threshold_spin)
        general_row_layout.addStretch(1)

        character_row = QWidget()
        character_row_layout = QHBoxLayout(character_row)
        character_row_layout.setContentsMargins(0, 0, 0, 0)
        character_row_layout.setSpacing(10)
        character_row_layout.addWidget(self.include_character_check)
        character_row_layout.addWidget(self.character_threshold_label)
        character_row_layout.addWidget(self.character_threshold_spin)
        character_row_layout.addWidget(self.include_copyright_check)
        character_row_layout.addStretch(1)

        right_layout.addWidget(threshold_title, 0, 0)
        right_layout.addWidget(general_row, 1, 0)
        right_layout.addWidget(character_row, 2, 0)

        self.current_image_label = _PrefixElidedLabel()
        self.current_image_label.setObjectName("TaggerHint")
        self.status_label = _PrefixElidedLabel()
        self.status_label.setObjectName("TaggerStatus")
        self._set_current_image_name(None)
        self.set_status_text("Tagger未実行")

        # 設定カードは同じ上段・同じ高さにそろえる。
        # その下は、状態表示を実行ボタン群の直前まで広げる1行にする。
        # モデル／デバイスカードの左列幅には縛られない。
        status_layout = QVBoxLayout()
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(3)
        status_layout.addWidget(self.current_image_label)
        status_layout.addWidget(self.status_label)

        status_action_layout = QHBoxLayout()
        status_action_layout.setContentsMargins(0, 0, 0, 0)
        status_action_layout.setSpacing(12)
        status_action_layout.addLayout(status_layout, 1)
        status_action_layout.addLayout(
            action_layout,
            0,
        )

        settings_layout = QGridLayout()
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setHorizontalSpacing(10)
        settings_layout.setVerticalSpacing(6)
        # Alignmentを指定しないことで、同じ行の高さまで両カードを自然に広げる。
        settings_layout.addWidget(left_card, 0, 0)
        settings_layout.addWidget(right_card, 0, 1)
        settings_layout.addLayout(status_action_layout, 1, 0, 1, 2)
        settings_layout.setColumnStretch(0, 1)
        settings_layout.setColumnStretch(1, 1)

        candidate_title_row = QHBoxLayout()
        candidate_title_row.setContentsMargins(0, 0, 0, 0)
        candidate_title_row.addStretch(1)
        self.candidate_title = QLabel("表示中画像のタグ候補")
        self.candidate_title.setObjectName("TaggerSectionTitle")
        self.candidate_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        candidate_title_row.addWidget(self.candidate_title)
        candidate_title_row.addStretch(1)

        self.candidate_container = QWidget()
        self.candidate_layout = QVBoxLayout(self.candidate_container)
        self.candidate_layout.setContentsMargins(10, 8, 10, 8)
        self.candidate_layout.setSpacing(2)
        self.candidate_layout.addStretch(1)

        self.candidate_scroll = QScrollArea()
        self.candidate_scroll.setWidgetResizable(True)
        self.candidate_scroll.setWidget(self.candidate_container)
        self.candidate_scroll.setMinimumHeight(220)

        self.apply_mode_combo = QComboBox()
        self.apply_mode_combo.addItem("タグが空の場合のみ（txtファイル無し含む）", "empty_only")
        self.apply_mode_combo.addItem("上書き", "overwrite")
        self.apply_mode_combo.addItem("既存txtの末尾に追加", "append")

        self.apply_button = QPushButton("反映")
        self.apply_button.setMinimumWidth(96)
        self.apply_button.clicked.connect(self._emit_apply_requested)

        apply_layout = QHBoxLayout()
        apply_layout.setContentsMargins(0, 0, 0, 0)
        apply_layout.setSpacing(10)
        apply_layout.addWidget(QLabel("反映方法"))
        apply_layout.addWidget(self.apply_mode_combo, 1)
        apply_layout.addWidget(self.apply_button)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)
        root.addLayout(settings_layout)
        root.addLayout(candidate_title_row)
        root.addWidget(self.candidate_scroll, 1)
        root.addLayout(apply_layout)

        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.device_combo.currentIndexChanged.connect(self._emit_settings_changed)
        self.include_general_check.toggled.connect(self._emit_settings_changed)
        self.general_threshold_spin.valueChanged.connect(self._emit_settings_changed)
        self.include_character_check.toggled.connect(self._emit_settings_changed)
        self.character_threshold_spin.valueChanged.connect(self._emit_settings_changed)
        self.apply_mode_combo.currentIndexChanged.connect(self._emit_settings_changed)

        self._sync_running_controls()
        self._set_empty_candidate_message("Tagger未実行")

    def set_items(self, items) -> None:
        self.items = items
        self.current_index = -1
        self._candidate_view_key = None
        self._current_display_result = None
        self._set_current_image_name(None)
        # フォルダ再読み込み後は前回実行の完了表示を残さない。
        self.set_status_text("Tagger未実行")
        self._set_empty_candidate_message("Tagger未実行")

    def set_current_index(self, index: int) -> None:
        self.current_index = index
        if 0 <= index < len(self.items):
            self._set_current_image_name(Path(self.items[index].image_path).name)
        else:
            self._set_current_image_name(None)

    def settings_dict(self) -> dict[str, object]:
        model_repo = self.model_combo.currentData() or self.model_combo.currentText().strip()
        device = self.device_combo.currentData() or self.device_combo.currentText().lower()
        return {
            "model_repo": str(model_repo).strip(),
            "device": str(device).lower(),
            "include_general": self.include_general_check.isChecked(),
            "general_threshold": float(self.general_threshold_spin.value()) / 100.0,
            "include_character": self.include_character_check.isChecked(),
            "character_threshold": float(self.character_threshold_spin.value()) / 100.0,
            "include_copyright": self.include_copyright_check.isChecked(),
            # 旧設定キー互換用。折りたたみUIを廃止したため常にFalseを保存する。
            "copyright_expanded": False,
            "apply_mode": self.apply_mode_combo.currentData(),
        }

    def restore_persisted_settings(
        self,
        *,
        model_repo: str,
        device: str,
        include_general: bool,
        general_threshold: float,
        include_character: bool,
        character_threshold: float,
        include_copyright: bool,
        copyright_expanded: bool,
        apply_mode: str,
    ) -> None:
        """保存済みのTagger設定を、Signalを出さずにUIへ戻す。"""
        controls = (
            self.model_combo,
            self.device_combo,
            self.include_general_check,
            self.general_threshold_spin,
            self.include_character_check,
            self.character_threshold_spin,
            self.include_copyright_check,
            self.apply_mode_combo,
        )
        self._restoring_settings = True
        for control in controls:
            control.blockSignals(True)
        try:
            model_index = self.model_combo.findData(str(model_repo).strip())
            if model_index >= 0:
                self.model_combo.setCurrentIndex(model_index)

            device_index = self.device_combo.findData(str(device).strip().lower())
            if device_index >= 0:
                self.device_combo.setCurrentIndex(device_index)

            self.include_general_check.setChecked(bool(include_general))
            self.general_threshold_spin.setValue(float(general_threshold) * 100.0)
            self.include_character_check.setChecked(bool(include_character))
            self.character_threshold_spin.setValue(float(character_threshold) * 100.0)
            self.include_copyright_check.setChecked(bool(include_copyright))
            # 旧バージョンの保存値は受け取るが、折りたたみUIを廃止したため使わない。
            _ = copyright_expanded

            apply_index = self.apply_mode_combo.findData(str(apply_mode).strip())
            if apply_index >= 0:
                self.apply_mode_combo.setCurrentIndex(apply_index)
        finally:
            for control in controls:
                control.blockSignals(False)
            self._restoring_settings = False

        self._sync_running_controls()

    def _emit_settings_changed(self, *_args) -> None:
        if not self._restoring_settings:
            self.settings_changed.emit(self.settings_dict())

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        if not self._running:
            self._cancelling = False
        self._sync_running_controls()

    def set_cancelling(self, cancelling: bool) -> None:
        """中止要求済みの間だけ、中止ボタンを多重押下できない表示へ切り替える。"""
        if not self._running:
            return

        self._cancelling = bool(cancelling)
        self._sync_running_controls()

    def _has_enabled_candidate_category(self) -> bool:
        """一般／キャラクターの少なくとも一方が候補対象として有効かを返す。"""
        return (
            self.include_general_check.isChecked()
            or self.include_character_check.isChecked()
            or (
                self._is_pixai_selected()
                and self.include_copyright_check.isChecked()
            )
        )

    def _sync_running_controls(self) -> None:
        running = self._running
        can_run = not running and self._has_enabled_candidate_category()

        # 両カテゴリOFFでは候補を作れないため、実行ボタンも無効にする。
        self.run_button.setEnabled(can_run)
        self.reset_button.setEnabled(not running)
        self.apply_button.setEnabled(not running)
        self.model_combo.setEnabled(not running)
        self.device_combo.setEnabled(not running)
        self.include_general_check.setEnabled(not running)
        self.include_character_check.setEnabled(not running)
        self._sync_pixai_copyright_controls()
        self._sync_category_threshold_enabled()
        self.apply_mode_combo.setEnabled(not running)

        # 中止ボタンは常時表示し、実行中だけ押せるようにする。
        self.cancel_button.setEnabled(running and not self._cancelling)
        self.cancel_button.setText("中止しています..." if self._cancelling else "中止")

    def set_tag_color_enabled(self, enabled: bool) -> None:
        """環境設定のタグ色分けON/OFFを、キャラクター・作品候補文字色へ反映する。"""
        next_enabled = bool(enabled)
        if self._tag_color_enabled == next_enabled:
            return

        self._tag_color_enabled = next_enabled
        if self._current_display_result is not None:
            self._show_candidates(self._current_display_result)

    def _set_current_image_name(self, image_name: str | None) -> None:
        if image_name:
            self.current_image_label.set_full_text(
                f"現在画像: {image_name}",
                preserved_prefix="現在画像: ",
            )
        else:
            self.current_image_label.set_full_text("現在画像: -")

    @staticmethod
    def _status_preserved_prefix(text: str) -> str:
        """実行中ステータスでは進捗表示を残し、末尾のファイル名だけを省略する。"""
        running_prefix = "Tagger実行中... "
        if not text.startswith(running_prefix):
            return ""

        # 実際の進捗文言は「Tagger実行中... 42/104 : ファイル名」。
        # 区切りまでを残せば、長いファイル名だけを中央省略できる。
        filename_separator = text.find(" : ", len(running_prefix))
        if filename_separator >= 0:
            return text[:filename_separator + 3]
        return running_prefix

    def set_status_text(self, text: str) -> None:
        self.status_label.set_full_text(
            text,
            preserved_prefix=self._status_preserved_prefix(text),
        )

    def set_current_result(
        self,
        image_name: str | None,
        result: TaggerResult | None,
        error: str | None = None,
        status: str | None = None,
    ) -> None:
        self._set_current_image_name(image_name)
        self._current_display_result = result

        # 同じ画像・同じ結果・同じ状態なら、重い候補行の再生成を省く。
        # 進捗通知が連続しても候補表示をちらつかせないための保険。
        view_key = (
            image_name,
            id(result) if result is not None else None,
            error,
            status,
        )
        if view_key == self._candidate_view_key:
            return
        self._candidate_view_key = view_key

        if status == "pending":
            self._set_empty_candidate_message("この画像はTagger実行待ちです。")
            return
        if status == "cancelled":
            self._set_empty_candidate_message("この画像のTagger実行は中止されました。")
            return
        if status == "skipped":
            self._set_empty_candidate_message("この画像は今回のTagger実行対象ではありません。")
            return
        if error:
            self._set_empty_candidate_message(f"Taggerエラー: {error}")
            return
        if status == "no_result":
            self._set_empty_candidate_message("しきい値を超える候補タグはありません。")
            return
        if result is None:
            self._set_empty_candidate_message("Tagger未実行")
            return

        self._show_candidates(result)

    def _emit_run_requested(self) -> None:
        if self._running or not self._has_enabled_candidate_category():
            return
        self.run_requested.emit(self.settings_dict())

    def _emit_cancel_requested(self) -> None:
        if not self._running or self._cancelling:
            return
        self.cancel_requested.emit()

    def _emit_apply_requested(self) -> None:
        self.apply_requested.emit(str(self.apply_mode_combo.currentData()))

    def _on_category_included_toggled(self, _checked: bool) -> None:
        self._sync_running_controls()

    def _is_pixai_selected(self) -> bool:
        model_repo = self.model_combo.currentData() or self.model_combo.currentText()
        return str(model_repo).strip().casefold() == PIXAI_MODEL_REPO.casefold()

    def _on_model_changed(self, _index: int) -> None:
        self._sync_pixai_copyright_controls()
        self._emit_settings_changed()

    def _on_copyright_included_toggled(self, _checked: bool) -> None:
        self._sync_running_controls()
        self._emit_settings_changed()

    def _sync_pixai_copyright_controls(self) -> None:
        pixai_selected = self._is_pixai_selected()
        enabled = pixai_selected and not self._running

        self.include_copyright_check.setVisible(pixai_selected)
        self.include_copyright_check.setEnabled(enabled)

    def _sync_category_threshold_enabled(self) -> None:
        # カテゴリのON/OFFは今回の候補に含めるかだけを決める。
        # OFF中も次回ONに戻す時の事前設定として、しきい値は編集可能に保つ。
        self._set_threshold_enabled(
            self.general_threshold_label,
            self.general_threshold_spin,
            self._general_threshold_opacity,
        )
        self._set_threshold_enabled(
            self.character_threshold_label,
            self.character_threshold_spin,
            self._character_threshold_opacity,
        )

    def _set_threshold_enabled(
        self,
        label: QLabel,
        spin: QDoubleSpinBox,
        opacity: QGraphicsOpacityEffect,
    ) -> None:
        enabled = not self._running
        label.setEnabled(enabled)
        spin.setEnabled(enabled)

        # disabledパレットの矢印色がWindows標準スタイルへ反映されない場合があるため、
        # 数値・背景・上下矢印をまとめて半透明化する。矢印の標準形状は維持される。
        opacity.setOpacity(1.0 if enabled else 0.48)

    def _clear_candidate_layout(self) -> None:
        while self.candidate_layout.count():
            item = self.candidate_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

    def _set_empty_candidate_message(self, text: str) -> None:
        self._clear_candidate_layout()
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #9a9a9a; font-weight: bold; padding: 24px;")
        self.candidate_layout.addWidget(label, 1)

    def _candidate_tooltip(self, candidate: TagCandidate) -> str:
        if candidate.category.casefold() != "copyright":
            return candidate.tag

        source_text = (
            "、".join(candidate.source_tags)
            if candidate.source_tags
            else "不明"
        )
        return (
            "作品タグ（キャラクター候補から推定）\n"
            "表示スコアは導出元キャラクター候補の最高スコアです。\n"
            f"推定元: {source_text}"
        )

    def _add_candidate_section_header(self, text: str) -> None:
        """カテゴリ別候補ブロックの見出しを追加する。"""
        label = QLabel(text)
        label.setStyleSheet(
            "color: #cfcfcf; font-weight: bold; padding-top: 7px;"
        )
        self.candidate_layout.addWidget(label)

    def _candidate_tag_text_color(
        self,
        candidate: TagCandidate,
    ) -> str | None:
        """キャラクター・作品候補に、既存タグチップと同じ枠線色を使う。"""
        if not self._tag_color_enabled:
            return None
        if candidate.category.casefold() not in {"character", "copyright"}:
            return None

        # 候補カテゴリではなく、辞書で解決したカテゴリの色を使う。
        # 既存分類を保護しているタグも、通常のタグチップと同じ色になる。
        category_style = self._tag_color_dictionary.style_for_tag(candidate.tag)
        if category_style.category_id == DEFAULT_UNKNOWN_CATEGORY:
            return None
        return category_style.border_color

    def _show_candidates(self, result: TaggerResult) -> None:
        perf_started = time.perf_counter() if dev_log_enabled("TAGGER_PERF") else 0.0

        self._clear_candidate_layout()

        candidates = result.all_candidates
        if not candidates:
            self._set_empty_candidate_message("候補タグなし")
            return

        character_candidates = [
            candidate
            for candidate in candidates
            if candidate.category.casefold() == "character"
        ]
        copyright_candidates = [
            candidate
            for candidate in candidates
            if candidate.category.casefold() == "copyright"
        ]
        general_and_other_candidates = [
            candidate
            for candidate in candidates
            if candidate.category.casefold() not in {"character", "copyright"}
        ]

        # キャラクター → 作品（キャラから推定） → 一般タグの順にまとめる。
        # 各カテゴリ内の順序はbackendが返したスコア順を維持する。
        display_sections: tuple[tuple[str, list[TagCandidate]], ...] = (
            ("キャラクタータグ", character_candidates),
            ("作品タグ（キャラから推定）", copyright_candidates),
            ("一般タグ", general_and_other_candidates),
        )

        for section_title, section_candidates in display_sections:
            if not section_candidates:
                continue

            self._add_candidate_section_header(section_title)

            for candidate in section_candidates:
                row = QWidget()
                row.setFixedHeight(22)
                row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(8)

                # タグ名の表示幅を広げ、候補バーは必要以上に横へ伸びないよう上限を設ける。
                # それでも収まらないタグは末尾を省略し、全文はツールチップで確認できる。
                tag_display_width = 280
                tag_label = QLabel()
                tag_label.setFixedWidth(tag_display_width)
                tag_label.setToolTip(self._candidate_tooltip(candidate))
                tag_label.setWordWrap(False)

                candidate_tag_color = self._candidate_tag_text_color(candidate)
                tag_label.setStyleSheet(
                    "font-weight: bold;"
                    if candidate_tag_color is None
                    else f"font-weight: bold; color: {candidate_tag_color};"
                )
                tag_label.ensurePolished()
                tag_label.setText(
                    tag_label.fontMetrics().elidedText(
                        candidate.tag,
                        Qt.TextElideMode.ElideRight,
                        tag_display_width,
                    )
                )

                bar = QProgressBar()
                bar.setRange(0, 1000)
                bar.setValue(max(0, min(1000, int(candidate.score * 1000))))
                bar.setTextVisible(False)
                bar.setFixedHeight(12)
                bar.setMinimumWidth(180)
                bar.setMaximumWidth(500)
                bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

                score_label = QLabel(f"{candidate.score * 100:.1f}%")
                score_label.setFixedWidth(56)
                score_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

                row_layout.addWidget(tag_label)
                row_layout.addWidget(bar, 1)
                row_layout.addWidget(score_label)

                self.candidate_layout.addWidget(row)

        self.candidate_layout.addStretch(1)

        if perf_started:
            log.info(
                "[TAGGER_PERF] candidate_render image=%s candidates=%d elapsed=%.2fms",
                result.image_path.name,
                len(candidates),
                (time.perf_counter() - perf_started) * 1000,
            )

    def show_not_implemented_apply_message(self) -> None:
        QMessageBox.information(
            self,
            "Tagger反映",
            "Stage2aではまだ反映処理は未接続です。\\n"
            "候補の確認までを先に実装しています。",
        )
