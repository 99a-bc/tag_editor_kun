from __future__ import annotations

from pathlib import Path
from copy import deepcopy
from tagger.profiles import normalize_profiles
from tagger.pixai_v1_backend import MODEL_REPO as PIXAI_V1_REPO, DEFAULT_THRESHOLDS
import time

from PySide6.QtCore import QEvent, QRectF, QPoint, Qt, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QShortcut, QKeySequence
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
    QSplitter,
    QStyleFactory,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tagger.pixai_backend import PIXAI_MODEL_REPO, pixai_model_is_cached
from tagger.result import TagCandidate, TaggerResult
from tagger_exclusion_panel import ExclusionController, ExclusionPanel, MODE_LABELS
from tag_color_dictionary import NO_COLOR_CATEGORIES, TagColorDictionary
from theme import SELECTED_TAG_DASH
from dev_config import dev_log_enabled
from logger import get_logger


log = get_logger(__name__)


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
        QTimer.singleShot(0, self, self._refresh_elided_text)

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


class _CandidateTagLabel(_PrefixElidedLabel):
    clicked = Signal(object)
    menu_requested = Signal(QPoint)

    def __init__(self, color=None):
        super().__init__()
        self.setObjectName("TaggerCandidateTag")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self._pressed = False
        self.is_selected = False
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._text_color = QColor(color or "#e8e8e8")
        self.set_excluded(False)

    def set_excluded(self, excluded):
        color = self._text_color
        if excluded:
            background = QColor("#1b1b1b")
            color = QColor(*(round(.33 * component + .67 * base) for component, base in zip(
                (color.red(), color.green(), color.blue()),
                (background.red(), background.green(), background.blue()))))
        self.setStyleSheet(
            f"QLabel#TaggerCandidateTag {{ font-weight: bold; color: {color.name()}; "
            "background: transparent; border: 1px solid transparent; border-radius: 3px; padding: 1px 3px; } "
            "QLabel#TaggerCandidateTag:hover { background: #2b2f33; border-color: #505961; }")

    def mousePressEvent(self, event):
        self._pressed = event.button() == Qt.MouseButton.LeftButton
        event.accept()

    def mouseReleaseEvent(self, event):
        pressed = self._pressed
        self._pressed = False
        if pressed and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(event.modifiers())
        elif event.button() == Qt.MouseButton.RightButton and self.rect().contains(event.position().toPoint()):
            self.menu_requested.emit(self.mapToGlobal(QPoint(0, self.height())))
        event.accept()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.is_selected:
            painter = QPainter(self)
            painter.setPen(QPen(QColor(SELECTED_TAG_DASH), 1.2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 3, 3)


class _CandidateRow(QWidget):
    def __init__(self, candidate, panel):
        super().__init__()
        self.candidate, self.panel = candidate, panel
        self.setFixedHeight(24)
        self.setMinimumWidth(0)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.tag_label = _CandidateTagLabel(panel._candidate_tag_text_color(candidate))
        self.tag_label.set_full_text(candidate.tag)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(max(0, min(1000, int(candidate.score * 1000))))
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(12)
        self.bar.setMinimumWidth(24)
        self.bar.setMaximumWidth(500)
        self.score_label = QLabel(f"{candidate.score * 100:.1f}%")
        self.score_label.setFixedWidth(max(52, self.score_label.fontMetrics().horizontalAdvance("100.0%") + 6))
        self.score_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.tag_label, 5)
        layout.addWidget(self.bar, 4)
        layout.addWidget(self.score_label)
        # ホバー背景・枠線は暗転させず、文字・バー・スコアだけを暗くする。
        self.opacity = QGraphicsOpacityEffect(self.bar)
        self.bar.setGraphicsEffect(self.opacity)
        self.score_opacity = QGraphicsOpacityEffect(self.score_label)
        self.score_label.setGraphicsEffect(self.score_opacity)
        self.tag_label.clicked.connect(lambda modifiers: panel.select_candidate(self, modifiers))
        self.tag_label.menu_requested.connect(lambda point: self.show_menu(point))
        for widget in (self, self.tag_label, self.bar, self.score_label):
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)

    def menu_position(self, menu, point):
        screen = self.tag_label.screen().availableGeometry()
        size = menu.sizeHint()
        return QPoint(
            max(screen.left(), min(point.x(), screen.right() + 1 - size.width())),
            max(screen.top(), min(point.y(), screen.bottom() + 1 - size.height())),
        )

    def show_menu(self, point):
        tags = self.panel.candidate_menu_targets(self)
        menu = self.panel.exclusion_controller.candidate_menu(tags, self.panel, clicked_tag=self.candidate.tag)
        try:
            menu.exec(self.menu_position(menu, point))
        finally:
            menu.deleteLater()
            self.panel.clear_candidate_selection()

    def refresh_exclusion(self):
        decision = self.panel.exclusion_controller.decision(self.candidate.tag)
        self.excluded = decision.excluded
        self.opacity.setOpacity(0.33 if decision.excluded else 1.0)
        self.score_opacity.setOpacity(0.33 if decision.excluded else 1.0)
        self.tag_label.set_excluded(decision.excluded)
        reasons = [self.panel._candidate_tooltip(self.candidate)]
        reasons.extend(f"恒久: {r.tag} / {MODE_LABELS[r.match_mode]} / ID: {r.rule_id}" for r in decision.matching_rules)
        tooltip = "\n".join(reasons)
        for widget in (self, self.tag_label, self.bar, self.score_label):
            widget.setToolTip(tooltip)
        self.tag_label.setToolTip("左クリックで選択、右クリックで除外操作を開く\n" + tooltip)


class TaggerPanel(QWidget):
    """WD Tagger用タブのUI。推論処理そのものは持たず、AppWindowへSignalで依頼する。"""

    run_requested = Signal(dict)
    reset_requested = Signal()
    apply_requested = Signal(str)
    cancel_requested = Signal()
    settings_changed = Signal(dict)
    exclusion_rules_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.items = []
        self.current_index = -1
        self._model_profiles = normalize_profiles({})
        self._profile_model = "SmilingWolf/wd-eva02-large-tagger-v3"
        self._running = False
        self._cancelling = False
        self._restoring_settings = False
        # 同じ結果を進捗通知のたびに再描画しないための表示キー。
        self._candidate_view_key: tuple[object, ...] | None = None
        self._current_display_result: TaggerResult | None = None
        self._tag_color_dictionary = TagColorDictionary.load()
        self._tag_color_enabled = True
        self._candidate_rows = []
        self._selected_candidates = set()
        self._candidate_anchor = None
        self._exclusion_width = 350
        self.exclusion_controller = ExclusionController(self)
        self.exclusion_controller.rules_changed.connect(self.exclusion_rules_changed.emit)

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
        self.reset_button.clicked.connect(self.clear_candidate_selection)
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
        # PixAIは必須ファイルがHugging Faceキャッシュに揃っている時だけ選択肢へ出す。
        # 判定はローカルキャッシュだけを参照し、ネットワーク接続やダウンロードは行わない。
        self.model_combo.addItem("PixAI Tagger v0.9", PIXAI_MODEL_REPO)
        self.model_combo.addItem("PixAI Tagger v1.0", PIXAI_V1_REPO)
        self.model_combo.setCurrentIndex(0)
        self.model_combo.setToolTip("使用するTaggerモデルを選択します。")

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
        self._device_setting_label = QLabel("推論実行デバイス")
        left_layout.addWidget(self._device_setting_label, 1, 0)
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
        self._legacy_category_rows = (general_row, character_row)
        self.v1_category_card = QWidget()
        grid = QGridLayout(self.v1_category_card)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(0)
        # 3列は必要幅のまま左詰めにし、余剰幅は末尾へ渡す。
        grid.setColumnStretch(3, 1)
        self._v1_spin_style = QStyleFactory.create("Fusion")
        self.v1_categories = {}
        for i, (name, label) in enumerate((("general","一般"),("character","キャラ"),("copyright","作品"),("style","画風"),("meta","メタ"),("rating","評価"))):
            cell = QWidget()
            layout = QHBoxLayout(cell)
            layout.setContentsMargins(0,0,0,0)
            layout.setSpacing(2)
            check = QCheckBox(label)
            check.setChecked(name == "general")
            spin = QDoubleSpinBox()
            spin.setRange(0,100)
            spin.setSingleStep(5.0)
            spin.setDecimals(0)
            spin.setSuffix("%")
            spin.setValue(DEFAULT_THRESHOLDS[name]*100)
            spin.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            layout.addWidget(check)
            layout.addWidget(spin)
            grid.addWidget(cell, i//3, i%3)
            self.v1_categories[name] = (check, spin)
            check.toggled.connect(self._on_category_included_toggled)
            check.toggled.connect(self._emit_settings_changed)
            spin.valueChanged.connect(self._emit_settings_changed)
        right_layout.addWidget(self.v1_category_card, 1, 0, 2, 1)
        self.v1_category_card.hide()


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
        self._settings_layout = settings_layout
        self._settings_cards = (left_card, right_card)
        for card in self._settings_cards:
            card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        candidate_title_row = QHBoxLayout()
        candidate_title_row.setContentsMargins(0, 0, 0, 0)
        candidate_title_row.addStretch(1)
        self.exclusion_toggle = QPushButton("除外リスト")
        self.exclusion_toggle.setCheckable(True)
        self.exclusion_toggle.setChecked(True)
        self.exclusion_toggle.setToolTip("恒久除外ルールを管理")
        self.candidate_title = QLabel("表示中画像のタグ候補")
        self.candidate_title.setObjectName("TaggerSectionTitle")
        self.candidate_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        candidate_title_row.addWidget(self.candidate_title)
        candidate_title_row.addWidget(self.exclusion_toggle)
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
        self.candidate_container.installEventFilter(self)
        self.candidate_scroll.viewport().installEventFilter(self)
        self.selection_count = QLabel("選択: 0件")
        candidate_title_row.insertWidget(1, self.selection_count)
        self._escape_selection = QShortcut(QKeySequence("Escape"), self.candidate_scroll)
        self._escape_selection.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._escape_selection.activated.connect(self.clear_candidate_selection)
        self.candidate_scroll.setMinimumWidth(0)
        self.candidate_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.exclusion_panel = ExclusionPanel(self.exclusion_controller, self._tag_color_dictionary)
        self.candidate_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.candidate_splitter.setChildrenCollapsible(False)
        self.candidate_splitter.addWidget(self.candidate_scroll)
        self.candidate_splitter.addWidget(self.exclusion_panel)
        self.candidate_splitter.setStretchFactor(0, 1)
        self.candidate_splitter.setStretchFactor(1, 0)
        self.candidate_splitter.setSizes([500, self._exclusion_width])
        self.exclusion_toggle.toggled.connect(self._toggle_exclusion_panel)
        self.exclusion_panel.close_requested.connect(lambda: self.exclusion_toggle.setChecked(False))
        self.candidate_splitter.splitterMoved.connect(self._remember_exclusion_width)
        self.exclusion_panel.sort_changed.connect(lambda _mode: self._emit_settings_changed())
        self.exclusion_controller.changed.connect(self._refresh_candidate_exclusions)

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
        root.addWidget(self.candidate_splitter, 1)
        root.addLayout(apply_layout)

        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.device_combo.currentIndexChanged.connect(self._emit_settings_changed)
        self.include_general_check.toggled.connect(self._emit_settings_changed)
        self.general_threshold_spin.valueChanged.connect(self._emit_settings_changed)
        self.include_character_check.toggled.connect(self._emit_settings_changed)
        self.character_threshold_spin.valueChanged.connect(self._emit_settings_changed)
        self.apply_mode_combo.currentIndexChanged.connect(self._emit_settings_changed)

        self._load_profile()
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
        if index != self.current_index:
            self.clear_candidate_selection()
        self.current_index = index
        if 0 <= index < len(self.items):
            self._set_current_image_name(Path(self.items[index].image_path).name)
        else:
            self._set_current_image_name(None)

    def settings_dict(self) -> dict[str, object]:
        self._save_profile()
        model_repo = self.model_combo.currentData() or self.model_combo.currentText().strip()
        device = self.device_combo.currentData() or self.device_combo.currentText().lower()
        return {
            "model_repo": str(model_repo).strip(),
            "model_profiles": deepcopy(self._model_profiles),
            "categories": {name: {"enabled":check.isChecked(), "threshold":spin.value()/100} for name,(check,spin) in self.v1_categories.items()},
            "device": str(device).lower(),
            "include_general": self.include_general_check.isChecked(),
            "general_threshold": float(self.general_threshold_spin.value()) / 100.0,
            "include_character": self.include_character_check.isChecked(),
            "character_threshold": float(self.character_threshold_spin.value()) / 100.0,
            "include_copyright": self.include_copyright_check.isChecked(),
            # 旧設定キー互換用。折りたたみUIを廃止したため常にFalseを保存する。
            "copyright_expanded": False,
            "apply_mode": self.apply_mode_combo.currentData(),
            "exclusion_panel_open": self.exclusion_toggle.isChecked(),
            "exclusion_panel_width": self._exclusion_width,
            "exclusion_sort_mode": self.exclusion_panel.sort_mode,
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
        exclusion_panel_open: bool = True,
        exclusion_panel_width: int = 350,
        exclusion_sort_mode: str = "registered",
        model_profiles=None,
    ) -> None:
        """保存済みのTagger設定を、Signalを出さずにUIへ戻す。"""
        controls = (
            self.model_combo,
            self.exclusion_toggle,
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
            self._model_profiles = normalize_profiles(model_profiles, dict(tagger_device=device, tagger_include_general=include_general, tagger_general_threshold=general_threshold, tagger_include_character=include_character, tagger_character_threshold=character_threshold, tagger_include_copyright=include_copyright))
            self._profile_model = str(self.model_combo.currentData())
            self._load_profile()
            # 旧バージョンの保存値は受け取るが、折りたたみUIを廃止したため使わない。
            _ = copyright_expanded

            apply_index = self.apply_mode_combo.findData(str(apply_mode).strip())
            if apply_index >= 0:
                self.apply_mode_combo.setCurrentIndex(apply_index)
            self._exclusion_width = exclusion_panel_width
            self.exclusion_panel.set_sort(exclusion_sort_mode)
            self.exclusion_toggle.setChecked(exclusion_panel_open)
            self.exclusion_panel.setVisible(exclusion_panel_open)
            QTimer.singleShot(0, self, self._restore_exclusion_width)
        finally:
            for control in controls:
                control.blockSignals(False)
            self._restoring_settings = False

        self._sync_running_controls()

    def _emit_settings_changed(self, *_args) -> None:
        if not self._restoring_settings:
            self.settings_changed.emit(self.settings_dict())

    def set_running(self, running: bool) -> None:
        if running and not self._running:
            self.clear_candidate_selection()
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
        if self._is_v1_selected():
            return any(check.isChecked() for check, _ in self.v1_categories.values())
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
        for check, spin in self.v1_categories.values():
            check.setEnabled(not running)
            spin.setEnabled(not running)
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
        """環境設定のタグ色分けON/OFFを全カテゴリーの候補文字色へ反映する。"""
        next_enabled = bool(enabled)
        if self._tag_color_enabled == next_enabled:
            return

        self._tag_color_enabled = next_enabled
        self.exclusion_panel.color_enabled = next_enabled
        self.exclusion_panel.refresh()
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
        self.clear_candidate_selection()
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

    def _is_v1_selected(self):
        return self.model_combo.currentData() == PIXAI_V1_REPO

    def _save_profile(self):
        if self._restoring_settings:
            return
        device = self.device_combo.currentData()
        if self._profile_model == PIXAI_V1_REPO:
            self._model_profiles[self._profile_model] = dict(device=device,
                categories={name: dict(enabled=check.isChecked(), threshold=spin.value()/100) for name,(check,spin) in self.v1_categories.items()})
        else:
            self._model_profiles[self._profile_model] = dict(device=device,
                include_general=self.include_general_check.isChecked(), general_threshold=self.general_threshold_spin.value()/100,
                include_character=self.include_character_check.isChecked(), character_threshold=self.character_threshold_spin.value()/100,
                include_copyright=self.include_copyright_check.isChecked())

    def _load_profile(self):
        from PySide6.QtCore import QSignalBlocker
        profile = self._model_profiles[self._profile_model]
        controls = [self.device_combo, self.include_general_check, self.include_character_check,
                    self.include_copyright_check, self.general_threshold_spin, self.character_threshold_spin]
        controls += [widget for pair in self.v1_categories.values() for widget in pair]
        blockers = [QSignalBlocker(widget) for widget in controls]
        self.device_combo.setCurrentIndex(self.device_combo.findData(profile["device"]))
        if self._is_v1_selected():
            for name,(check,spin) in self.v1_categories.items():
                check.setChecked(profile["categories"][name]["enabled"])
                spin.setValue(profile["categories"][name]["threshold"]*100)
        else:
            self.include_general_check.setChecked(profile["include_general"])
            self.general_threshold_spin.setValue(profile["general_threshold"]*100)
            self.include_character_check.setChecked(profile["include_character"])
            self.character_threshold_spin.setValue(profile["character_threshold"]*100)
            self.include_copyright_check.setChecked(profile["include_copyright"])
        del blockers
        # カードの境界はモデルによらず1:1。内容だけを切り替える。
        v1 = self._is_v1_selected()
        self.v1_category_card.parentWidget().layout().setVerticalSpacing(0 if v1 else 8)
        for check, spin in self.v1_categories.values():
            spin.setStyle(self._v1_spin_style)
            spin.ensurePolished()
            # 縦矢印16pxと枠・エディター余白8pxを含む。
            spin.setFixedWidth(spin.fontMetrics().horizontalAdvance("100%") + 24)
            spin.setFixedHeight(self.general_threshold_spin.sizeHint().height())
        # どのモデルでも全内容を収める共通の最小幅を使う。
        left, right = self._settings_cards
        legacy_width = sum(min(w.maximumWidth(), max(w.minimumWidth(), w.sizeHint().width())) for w in (
            self.include_character_check, self.character_threshold_label,
            self.character_threshold_spin, self.include_copyright_check)) + 30
        margins = right.layout().contentsMargins()
        cells = [check.sizeHint().width() + 2 + spin.width() for check,spin in self.v1_categories.values()]
        v1_width = sum(max(cells[i], cells[i+3]) for i in range(3)) + 8
        needed = max(legacy_width, v1_width)
        common_minimum = max(left.minimumSizeHint().width(), needed + margins.left() + margins.right() + 2)
        for card in self._settings_cards:
            card.setMinimumWidth(common_minimum)
        # 表示前にサイズを確定し、切替途中のsizeHintでウィンドウが広がるのを防ぐ。
        for row in self._legacy_category_rows:
            row.setVisible(not v1)
        self.v1_category_card.setVisible(v1)

    def _on_model_changed(self, _index: int) -> None:
        self._save_profile()
        self._profile_model = str(self.model_combo.currentData())
        self._load_profile()
        self._sync_running_controls()
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

    def select_candidate(self, row, modifiers):
        if modifiers & Qt.KeyboardModifier.ShiftModifier and self._candidate_anchor in self._candidate_rows:
            start, end = sorted((self._candidate_rows.index(self._candidate_anchor), self._candidate_rows.index(row)))
            self._selected_candidates.update(self._candidate_rows[start:end + 1])
        else:
            if row in self._selected_candidates:
                self._selected_candidates.remove(row)
            else:
                self._selected_candidates.add(row)
            self._candidate_anchor = row
        self._refresh_candidate_selection()

    def _refresh_candidate_selection(self):
        for row in self._candidate_rows:
            row.tag_label.is_selected = row in self._selected_candidates
            row.tag_label.update()
        self.selection_count.setText(f"選択: {len(self._selected_candidates)}件")

    def clear_candidate_selection(self):
        self._selected_candidates.clear()
        self._candidate_anchor = None
        self._refresh_candidate_selection()

    def candidate_menu_targets(self, row):
        rows = [r for r in self._candidate_rows if r in self._selected_candidates] if row in self._selected_candidates else [row]
        return [r.candidate.tag for r in rows]

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            point = self.candidate_container.mapFrom(watched, event.position().toPoint())
            # 行間の数pxを空白と誤認しない。下部のstretch等は解除対象。
            spacing = max(0, self.candidate_layout.spacing())
            in_rows = any(row.geometry().adjusted(0, -spacing, 0, spacing).contains(point)
                          for row in self._candidate_rows)
            if not in_rows:
                self.clear_candidate_selection()
        return super().eventFilter(watched, event)

    def _clear_candidate_layout(self) -> None:
        self.clear_candidate_selection()
        self._candidate_rows = []
        while self.candidate_layout.count():
            item = self.candidate_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def _set_empty_candidate_message(self, text: str) -> None:
        self._clear_candidate_layout()
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #9a9a9a; font-weight: bold; padding: 24px;")
        self.candidate_layout.addWidget(label, 1)

    def _candidate_tooltip(self, candidate: TagCandidate) -> str:
        if candidate.category.casefold() != "copyright" or not candidate.source_tags:
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

    def set_exclusion_context(self, rules, issues=(), path=None):
        self.exclusion_controller.bind(rules, issues, path)
        if issues:
            self.exclusion_toggle.setToolTip("除外設定の読込エラーがあります。除外リストで確認してください。")
            QTimer.singleShot(0, lambda: QMessageBox.warning(
                self, "除外設定の読込エラー",
                f"正常なルールのみ読み込みました。元ファイルは上書きしません。\n設定ファイル: {self.exclusion_controller.path}\n" + "\n".join(issues)))

    def set_tag_completion_settings(self, *, tag_dictionary, enabled, limit):
        self.exclusion_panel.tag_dictionary = tag_dictionary
        self.exclusion_panel.completion_enabled = enabled
        self.exclusion_panel.completion_limit = limit

    def _refresh_candidate_exclusions(self):
        for row in self._candidate_rows:
            row.refresh_exclusion()

    def _toggle_exclusion_panel(self, visible):
        if not visible and self.exclusion_panel.isVisible() and not self._restoring_settings:
            self._exclusion_width = max(240, self.candidate_splitter.sizes()[1])
        self.exclusion_panel.setVisible(visible)
        if visible:
            self._restore_exclusion_width()
        self._emit_settings_changed()

    def _restore_exclusion_width(self):
        if self.exclusion_toggle.isChecked():
            available = max(1, self.candidate_splitter.width() - self.candidate_splitter.handleWidth())
            right = min(self._exclusion_width, max(240, available - 180))
            self.candidate_splitter.setSizes([max(1, available - right), right])

    def showEvent(self, event):
        super().showEvent(event)
        # 起動時は非表示タブなので、実際の幅が決まってから保存幅を復元する。
        QTimer.singleShot(0, self, self._restore_exclusion_width)

    def _remember_exclusion_width(self, _position, _index):
        if self.exclusion_toggle.isChecked() and not self._restoring_settings:
            self._exclusion_width = max(240, self.candidate_splitter.sizes()[1])
            self._emit_settings_changed()

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
        """推論カテゴリーとは独立に共通辞書のアクセント色を使う。"""
        if not self._tag_color_enabled:
            return None

        # 候補カテゴリではなく、辞書で解決したカテゴリの色を使う。
        # 既存分類を保護しているタグも、通常のタグチップと同じ色になる。
        category_style = self._tag_color_dictionary.style_for_tag(candidate.tag)
        if category_style.category_id in NO_COLOR_CATEGORIES:
            return None
        return category_style.border_color

    def _show_candidates(self, result: TaggerResult) -> None:
        perf_started = time.perf_counter() if dev_log_enabled("TAGGER_PERF") else 0.0

        self._clear_candidate_layout()

        candidates = result.all_candidates
        if not candidates:
            self._set_empty_candidate_message("候補タグなし")
            return

        labels = {"character":"キャラクタータグ", "copyright":"作品タグ", "general":"一般タグ", "style":"画風タグ", "meta":"メタタグ", "rating":"評価タグ"}
        display_sections = []
        # 表示だけを並べ替え、結果データと反映順序は変更しない。
        for name in ("character", "copyright", "style", "meta", "rating", "general"):
            group = [candidate for candidate in candidates if candidate.category.casefold() == name]
            title = labels[name]
            if name == "copyright" and any(c.source_tags for c in group):
                title += "（キャラから推定）"
            display_sections.append((title, group))

        for section_title, section_candidates in display_sections:
            if not section_candidates:
                continue

            self._add_candidate_section_header(section_title)

            for candidate in section_candidates:
                row = _CandidateRow(candidate, self)
                self._candidate_rows.append(row)
                self.candidate_layout.addWidget(row)

        self.candidate_layout.addStretch(1)
        self._refresh_candidate_exclusions()

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
