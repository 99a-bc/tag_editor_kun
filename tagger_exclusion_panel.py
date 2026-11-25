from __future__ import annotations

"""Tagger共通除外の編集UI。推論・タグ編集タブの状態は変更しない。"""

from dataclasses import dataclass, replace
from html import escape
from math import ceil
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QEvent, QTimer, QItemSelection, QItemSelectionModel, QMimeData, QObject, QPoint, QRectF, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFrame, QHeaderView, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QStyle, QStyleOptionButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from tag_context_menu import add_tag_lookup_actions, show_tag_lookup_menu
from tag_drag_visual import drag_visual
from shiboken6 import isValid
from tag_color_dictionary import tag_chip_qss
from tag_completion import TagCompletionController
from tag_io import parse_tags
from tag_panel import ADD_TAG_DIALOG_QSS, TAG_CHIP_QSS, PresetTagSearchBar, SearchHighlightButton
from tagger.exclusions import (
    ExclusionRule, ExclusionRuleSet, ExclusionValidationError,
    default_exclusions_path, normalize_tag, save_exclusions, evaluate_exclusion,
)
from theme import APP_BG, BORDER_SOFT, PANEL_BG_ALT, SELECTED_TAG_DASH, TAG_REORDER_MARKER_QSS, TEXT


MODE_LABELS = {"exact": "完全一致", "word": "単語一致"}
SORT_LABELS = {"registered": "登録順", "name": "名前順", "category": "カテゴリー順", "manual": "手動順"}
RULE_MIME = "application/x-tag-editor-exclusion-rule"


class ExclusionDeleteDialog(QDialog):
    """除外リストで選択済みのルールを確認する。"""

    def __init__(self, rules, parent=None):
        super().__init__(parent)
        count = len(rules)
        self.setWindowTitle("恒久除外ルールの削除")
        self.setStyleSheet(ADD_TAG_DIALOG_QSS + f"""
            QTreeWidget {{
                background: {APP_BG};
                border: 1px solid {BORDER_SOFT};
                color: {TEXT};
            }}
            QHeaderView::section {{
                background: {PANEL_BG_ALT};
                border: 0;
                border-bottom: 1px solid {BORDER_SOFT};
                color: {TEXT};
                padding: 4px;
            }}
        """)
        self.resize(580, 440)
        layout = QVBoxLayout(self)
        self.message = QLabel(f"選択した{count}件の除外ルールを削除しますか？")
        self.message.setObjectName("DialogTitle")
        warning = QLabel("単語一致などのルールは、ほかのタグにも影響する場合があります。")
        warning.setObjectName("DialogHint")
        for label in (self.message, warning):
            label.setWordWrap(True)
            layout.addWidget(label)

        self.listing = QTreeWidget()
        self.listing.setHeaderLabels(["タグ名", "一致方式"])
        self.listing.setRootIsDecorated(False)
        self.listing.setUniformRowHeights(True)
        self.listing.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.listing.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.listing.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        header = self.listing.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for rule in rules:
            mode = MODE_LABELS[rule.match_mode]
            item = QTreeWidgetItem(self.listing, [rule.tag, mode])
            item.setData(0, Qt.ItemDataRole.UserRole, rule.rule_id)
            tooltip = f"<qt>{escape(rule.tag)}<br>{mode}<br>ID: {escape(rule.rule_id)}</qt>"
            item.setToolTip(0, tooltip)
            item.setToolTip(1, tooltip)
        layout.addWidget(self.listing, 1)

        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        self.delete_button = buttons.addButton(f"{count}件を削除", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.cancel_button.setAutoDefault(False)
        self.delete_button.setAutoDefault(False)
        self.cancel_button.setDefault(True)
        buttons.rejected.connect(self.reject)
        self.delete_button.clicked.connect(self.accept)
        layout.addWidget(buttons)

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parentWidget()
        screen = parent.window().screen() if parent is not None else self.screen()
        available = screen.availableGeometry().adjusted(8, 8, -8, -8)
        frame = self.frameGeometry().size() - self.size()
        self.setMaximumSize(available.size() - frame)
        self.resize(self.size().boundedTo(self.maximumSize()))
        frame_size = self.frameGeometry().size()
        self.move(available.center() - QPoint(frame_size.width() // 2, frame_size.height() // 2))
        self.cancel_button.setFocus()


class ExclusionController(QObject):
    changed = Signal()
    rules_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rules = ExclusionRuleSet()
        self.path = default_exclusions_path()
        self.issues: tuple[str, ...] = ()

    def bind(self, rules, issues=(), path=None):
        self.rules = rules
        self.issues = tuple(issues)
        self.path = Path(path) if path is not None else default_exclusions_path()
        self.changed.emit()

    def commit(self, rules, parent=None):
        """保存に成功した集合だけを公開。問題のある元ファイルは変更しない。"""
        if self.issues:
            QMessageBox.warning(parent, "除外設定", "読込エラーがあるため保存できません。\n" + "\n".join(self.issues))
            return False
        try:
            checked = ExclusionRuleSet(rules)
            save_exclusions(checked, self.path)
        except (OSError, ExclusionValidationError) as error:
            QMessageBox.warning(parent, "除外設定を保存できません", str(error))
            return False
        self.rules = checked
        self.rules_changed.emit(checked)
        self.changed.emit()
        return True

    def decision(self, tag):
        return evaluate_exclusion(tag, self.rules)

    def edited_rules(self, selected_ids, drafts):
        old = {r.rule_id: r for r in self.rules.rules}
        rules = [r for r in self.rules.rules if r.rule_id not in selected_ids]
        registered = max((r.registered_order for r in old.values()), default=-1) + 1
        manual = max((r.manual_order for r in old.values()), default=-1) + 1
        for draft in drafts:
            if draft.key in selected_ids and draft.key in old:
                rules.append(replace(old[draft.key], tag=draft.tag, match_mode=draft.mode))
            else:
                rules.append(ExclusionRule(uuid4().hex, draft.tag, draft.mode, registered, manual))
                registered += 1
                manual += 1
        return ExclusionRuleSet(rules).rules

    def add_rule(self, tag, mode, parent=None):
        rules = ExclusionRuleSet(self.rules.rules)
        try:
            rules.add(tag, mode)
        except ExclusionValidationError as error:
            QMessageBox.warning(parent, "除外設定", str(error))
            return False
        return self.commit(rules.rules, parent)

    def remove_rules(self, ids, parent=None, *, from_candidates=False):
        selected = [r for r in self.rules.rules if r.rule_id in ids]
        if not selected:
            return False
        if from_candidates:
            names = "\n".join(f"・{r.tag} / {MODE_LABELS[r.match_mode]} / ID: {r.rule_id}" for r in selected)
            confirmed = QMessageBox.question(
                parent, "恒久除外ルールの削除",
                "次の恒久ルールを削除しますか？\n単語一致など、このルールに一致する他のタグにも影響します。\n\n" + names,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes
        else:
            dialog = ExclusionDeleteDialog(selected, parent)
            try:
                confirmed = dialog.exec() == QDialog.DialogCode.Accepted
            finally:
                dialog.deleteLater()
        if not confirmed:
            return False
        return self.commit([r for r in self.rules.rules if r.rule_id not in ids], parent)

    def reorder(self, ids, parent=None):
        old = {r.rule_id: r for r in self.rules.rules}
        if len(ids) != len(old) or set(ids) != set(old):
            return False
        return self.commit([replace(old[key], manual_order=i) for i, key in enumerate(ids)], parent)

    def change_mode(self, rule_id, mode, parent=None):
        if not any(r.rule_id == rule_id for r in self.rules.rules):
            return False
        # 重複検証・保存・公開は既存のトランザクションを通す。
        try:
            rules = [replace(r, match_mode=mode) if r.rule_id == rule_id else r for r in self.rules.rules]
        except ExclusionValidationError as error:
            QMessageBox.warning(parent, "除外設定", str(error))
            return False
        return self.commit(rules, parent)

    def matching_rules_for_tags(self, tags):
        ids = {r.rule_id for tag in tags for r in self.decision(tag).matching_rules}
        return [r for r in self.rules.rules if r.rule_id in ids]

    def candidate_menu(self, tags, parent=None, *, clicked_tag=None):
        tags = [tags] if isinstance(tags, str) else list(tags)
        menu = QMenu(parent)
        add = menu.addAction("恒久除外リストに追加...", lambda: parent.exclusion_panel.open_dialog(tags=tags))
        remove = menu.addAction("恒久除外ルールを削除...", lambda: self.choose_rules_to_remove(tags, parent))
        add.setEnabled(not self.issues)
        remove.setEnabled(not self.issues and bool(self.matching_rules_for_tags(tags)))
        menu.addSeparator()
        add_tag_lookup_actions(menu, clicked_tag if clicked_tag is not None else tags[0])
        return menu

    def choose_rules_to_remove(self, tags, parent=None):
        rules = self.matching_rules_for_tags(tags)
        if not rules or self.issues:
            return
        dialog = QDialog(parent)
        dialog.setWindowTitle("恒久除外ルールを削除")
        dialog.resize(620, 400)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("削除するルールを選択してください。他のタグにも影響します。"))
        listing = QListWidget()
        for rule in rules:
            item = QListWidgetItem(f"{rule.tag} / {MODE_LABELS[rule.match_mode]} / ID: {rule.rule_id}", listing)
            item.setData(Qt.ItemDataRole.UserRole, rule.rule_id)
            item.setCheckState(Qt.CheckState.Unchecked)
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                ids = {listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count())
                       if listing.item(i).checkState() == Qt.CheckState.Checked}
                self.remove_rules(ids, parent, from_candidates=True)
        finally:
            dialog.deleteLater()


class ElidedChip(SearchHighlightButton):
    """既存の検索ハイライトと色を使い、行の残り幅に合わせて省略するチップ。"""

    selection_requested = Signal(object)

    def __init__(self, tag):
        self.full_tag = tag
        super().__init__(tag)
        self.setToolTip(tag)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        self.ensurePolished()
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self.full_tag
        metrics = self.fontMetrics()
        # elidedText compares fractional glyph advances even for an integer contents rect.
        text_width = ceil(QFontMetricsF(metrics).horizontalAdvance(self.full_tag))
        return self.style().sizeFromContents(QStyle.ContentsType.CT_PushButton, option,
            QSize(text_width, metrics.height()), self)

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_display_text()

    def update_display_text(self):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        self.set_display_text(self.fontMetrics().elidedText(
            self.full_tag, Qt.TextElideMode.ElideRight, max(0, rect.width())))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selection_requested.emit(event.modifiers())
        super().mousePressEvent(event)


class DraggableRuleChip(ElidedChip):
    def __init__(self, tag, key, rule_list):
        super().__init__(tag)
        self.key, self.rule_list = key, rule_list
        self.start = None
        self.press_modifiers = Qt.KeyboardModifier.NoModifier
        self.drag_enabled = True
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_lookup_menu)

    def show_lookup_menu(self, point):
        show_tag_lookup_menu(self, self.full_tag, self.mapToGlobal(point))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = event.position().toPoint()
            self.press_modifiers = event.modifiers()
            self.setDown(True)
            event.accept()
        else:
            event.accept()

    def mouseMoveEvent(self, event):
        if (self.start is not None and self.drag_enabled
                and self.press_modifiers == Qt.KeyboardModifier.NoModifier
                and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self.start).manhattanLength() >= QApplication.startDragDistance()):
            origin = self.mapToGlobal(self.start)
            self.start = None
            self.setDown(False)
            self.rule_list.start_rule_drag(self.key, origin)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            start, self.start = self.start, None
            self.setDown(False)
            if start is not None and self.rect().contains(event.position().toPoint()):
                self.selection_requested.emit(self.press_modifiers)
            event.accept()
        else:
            event.accept()


@dataclass
class DraftRule:
    key: str
    tag: str
    mode: str = "exact"


class RuleRow(QWidget):
    remove_requested = Signal(str)
    selected = Signal(str, object)

    def __init__(self, key, tag, mode, dictionary, color_enabled, *, rule_list=None):
        super().__init__()
        self.key, self.tag = key, tag
        self.is_selected = False
        self.setMinimumWidth(0)
        self.setFixedHeight(40)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 3, 4, 3)
        row.setSpacing(5)
        self.chip = DraggableRuleChip(tag, key, rule_list) if rule_list is not None else ElidedChip(tag)
        self.chip.setStyleSheet(
            (tag_chip_qss(dictionary.style_for_tag(tag)) if color_enabled else None) or TAG_CHIP_QSS)
        self.chip.ensurePolished()
        self.chip.selection_requested.connect(lambda modifiers: self.selected.emit(key, modifiers))
        self.chip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        row.addWidget(self.chip)
        row.addStretch(1)
        self.mode = QComboBox()
        for value, label in MODE_LABELS.items():
            self.mode.addItem(label, value)
        self.mode.setCurrentIndex(self.mode.findData(mode))
        self.mode.setFixedWidth(108)
        self.mode.setToolTip("除外ルールの一致方式")
        row.addWidget(self.mode)
        if rule_list is None:
            self.remove = QPushButton("×")
            self.remove.setFixedSize(24, 26)
            self.remove.setToolTip(f"{tag} / {MODE_LABELS[mode]} を削除")
            self.remove.setStyleSheet("QPushButton { padding: 0px; }")
            self.remove.clicked.connect(lambda: self.remove_requested.emit(key))
            row.addWidget(self.remove)

        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self.fit_chip)
        self.chip.installEventFilter(self)
        self._fit_timer.start(0)

    def eventFilter(self, watched, event):
        if watched is self.chip and event.type() in (
            QEvent.Type.FontChange, QEvent.Type.StyleChange, QEvent.Type.PolishRequest,
            QEvent.Type.DevicePixelRatioChange):
            # QSS・継承フォントが確定した後に計測する。行の破棄でタイマーも破棄する。
            self._fit_timer.start(0)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_chip()

    def fit_chip(self):
        layout = self.layout()
        margins = layout.contentsMargins()
        controls = [self.mode] + ([self.remove] if hasattr(self, "remove") else [])
        available = self.width() - margins.left() - margins.right()
        available -= sum(w.width() for w in controls) + layout.spacing() * len(controls)
        width = max(0, min(self.chip.sizeHint().width(), available))
        self.chip.setMaximumWidth(width)
        layout.activate()
        self.chip.update_display_text()

    def set_selected(self, selected):
        if self.is_selected != selected:
            self.is_selected = selected
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.is_selected:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(SELECTED_TAG_DASH), 1.2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 5, 5)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.key, event.modifiers())
            event.accept()
        else:
            super().mousePressEvent(event)


class ExclusionEditDialog(QDialog):
    """追加と登録済み編集で同じレイアウト。ドラフトはルールIDで管理する。"""

    def __init__(self, parent=None, *, rules=(), dictionary, tag_dictionary=None,
                 color_enabled=True, completion_enabled=True, completion_limit=12, validator=None):
        super().__init__(parent)
        self.setWindowTitle("除外リスト：タグ編集" if rules else "除外リスト：タグ追加")
        self.setStyleSheet(ADD_TAG_DIALOG_QSS)
        self.resize(600, 480)
        self.setMinimumSize(430, 340)
        self.dictionary, self.color_enabled, self.validator = dictionary, color_enabled, validator
        self.drafts = [DraftRule(r.rule_id, r.tag, r.match_mode) for r in rules]
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self.windowTitle()))
        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText("例: shirt, white shirt, socks")
        layout.addWidget(self.input_line)
        layout.addWidget(QLabel("複数タグはカンマ区切りで入力できます。"))
        self.completion = TagCompletionController(
            self.input_line, tag_dictionary if completion_enabled else None,
            tag_color_dictionary=dictionary, tag_color_enabled=color_enabled, limit=completion_limit)
        if completion_enabled and not self.completion.is_available():
            layout.addWidget(QLabel("入力補完辞書が利用できません。手入力は可能です。"))
        layout.addWidget(QLabel("登録予定タグ（一致方式はタグごとに指定）"))
        self.preview = QListWidget()
        self.preview.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.preview, 1)
        bulk = QHBoxLayout()
        self.all_exact = QPushButton("すべて完全一致")
        self.all_word = QPushButton("すべて単語一致")
        self.all_exact.clicked.connect(lambda: self.set_all_modes("exact"))
        self.all_word.clicked.connect(lambda: self.set_all_modes("word"))
        bulk.addWidget(self.all_exact)
        bulk.addWidget(self.all_word)
        bulk.addStretch()
        layout.addLayout(bulk)
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #ffab91;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox()
        self.accept_button = buttons.addButton("更新" if rules else "追加", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.input_line.setText(", ".join(d.tag for d in self.drafts))
        self.input_line.textChanged.connect(self.sync_input)
        self.input_line.returnPressed.connect(self.accept)
        self.rebuild()

    def sync_input(self):
        tags = parse_tags(self.input_line.text())
        old = list(self.drafts)
        unused = list(old)
        normalized = [normalize_tag(tag) for tag in tags]
        drafts = []
        for index, tag in enumerate(tags):
            match = next((d for d in unused if normalize_tag(d.tag) == normalized[index]), None)
            if match is None and index < len(old):
                candidate = old[index]
                if candidate in unused and normalize_tag(candidate.tag) not in normalized:
                    match = candidate
            if match is not None:
                unused.remove(match)
                drafts.append(DraftRule(match.key, tag, match.mode))
            else:
                drafts.append(DraftRule(uuid4().hex, tag))
        # 同じ方式の重複入力は先頭を残す。同名・異方式の編集行は維持する。
        seen = set()
        self.drafts = []
        for draft in drafts:
            key = (normalize_tag(draft.tag), draft.mode)
            if key not in seen:
                self.drafts.append(draft)
                seen.add(key)
        self.rebuild()

    def rebuild(self):
        self.preview.clear()
        self.rows = {}
        for draft in self.drafts:
            item = QListWidgetItem(self.preview)
            item.setSizeHint(QSize(0, 40))
            row = RuleRow(draft.key, draft.tag, draft.mode, self.dictionary, self.color_enabled)
            row.mode.currentIndexChanged.connect(lambda _index, d=draft, combo=row.mode: setattr(d, "mode", combo.currentData()))
            row.remove_requested.connect(self.remove_draft)
            self.preview.setItemWidget(item, row)
            self.rows[draft.key] = row
        self.error_label.clear()
        self.accept_button.setEnabled(bool(self.drafts))

    def remove_draft(self, key):
        self.drafts = [d for d in self.drafts if d.key != key]
        self.input_line.blockSignals(True)
        self.input_line.setText(", ".join(d.tag for d in self.drafts))
        self.input_line.blockSignals(False)
        self.completion.hide_popup()
        self.rebuild()

    def set_all_modes(self, mode):
        for draft in self.drafts:
            draft.mode = mode
        self.rebuild()

    def accept(self):
        try:
            if not self.drafts:
                raise ExclusionValidationError("タグを入力してください。")
            if self.validator is not None:
                self.validator(self.drafts)
            else:
                ExclusionRuleSet(ExclusionRule(d.key, d.tag, d.mode, i, i) for i, d in enumerate(self.drafts))
        except ExclusionValidationError as error:
            self.error_label.setText(str(error))
            return
        super().accept()

    def done(self, result):
        self.completion.dispose()
        super().done(result)




class RuleList(QListWidget):
    moved = Signal(str, int)

    def __init__(self):
        super().__init__()
        self.token = uuid4().hex
        self.anchor_id = None
        self.drag_active = False
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.insertion_marker = QFrame(self.viewport())
        self.insertion_marker.setObjectName("TagReorderMarker")
        self.insertion_marker.setStyleSheet(TAG_REORDER_MARKER_QSS)
        self.insertion_marker.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.insertion_marker.hide()
        self._drop_point = None
        self.verticalScrollBar().valueChanged.connect(self.refresh_insertion_marker)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(0)
        self.setStyleSheet("QListWidget { outline: 0; } QListWidget::item:selected { background: transparent; }")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # indexWidgetの古い幅が項目の推奨幅へ逆流しないよう、行幅を更新する。
        for i in range(self.count()):
            row = self.itemWidget(self.item(i))
            if row is not None:
                row.resize(self.viewport().width(), row.height())
        self.doItemsLayout()
        self.refresh_insertion_marker()

    def select_rule(self, key, modifiers=Qt.KeyboardModifier.NoModifier):
        items = {self.item(i).data(Qt.ItemDataRole.UserRole): self.item(i) for i in range(self.count())}
        if self.drag_active or key not in items:
            return
        model = self.selectionModel()
        index = self.indexFromItem(items[key])
        flags = QItemSelectionModel.SelectionFlag
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            anchor = items.get(self.anchor_id, items[key])
            first, last = sorted((self.row(anchor), index.row()))
            selection = QItemSelection(self.model().index(first, 0), self.model().index(last, 0))
            model.select(selection, (flags.Select if modifiers & Qt.KeyboardModifier.ControlModifier else flags.ClearAndSelect) | flags.Rows)
            if self.anchor_id not in items:
                self.anchor_id = key
        else:
            model.select(index, flags.Toggle | flags.Rows)
            self.anchor_id = key
        model.setCurrentIndex(index, flags.NoUpdate)

    def mousePressEvent(self, event):
        if self.drag_active:
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                self.select_rule(item.data(Qt.ItemDataRole.UserRole), event.modifiers())
            elif event.modifiers() == Qt.KeyboardModifier.NoModifier:
                self.clearSelection()
                self.anchor_id = None
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # 選択は明示的なクリックだけで更新する。Qt標準のドラッグ範囲選択を止める。
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def rule_mime(self, key):
        mime = QMimeData()
        mime.setData(RULE_MIME, f"{self.token}:{key}".encode("utf-8"))
        return mime

    def accepts_mime(self, mime, source):
        if source is not self or not mime.hasFormat(RULE_MIME):
            return False
        value = bytes(mime.data(RULE_MIME)).decode("utf-8", errors="replace")
        return any(value == f"{self.token}:{self.item(i).data(Qt.ItemDataRole.UserRole)}" for i in range(self.count()))

    def start_rule_drag(self, key, origin=None):
        if self.drag_active:
            return
        self.drag_active = True
        item = next((self.item(i) for i in range(self.count()) if self.item(i).data(Qt.ItemDataRole.UserRole) == key), None)
        if item is None:
            self.drag_active = False
            return
        row = self.itemWidget(item)
        chip = row.chip
        hotspot = chip.mapFromGlobal(origin) if origin is not None else QPoint(0, 0)
        drag = QDrag(self)
        drag.setMimeData(self.rule_mime(key))
        try:
            with drag_visual(drag, chip, hotspot):
                drag.exec(Qt.DropAction.MoveAction)
        finally:
            self.drag_active = False
            self.hide_insertion_marker()
            if isValid(drag):
                drag.deleteLater()

    def insertion_index(self, point):
        item = self.itemAt(point)
        index = self.row(item) if item else self.count()
        if item and point.y() > self.visualItemRect(item).center().y():
            index += 1
        return index

    def hide_insertion_marker(self):
        self._drop_point = None
        self.insertion_marker.hide()

    def refresh_insertion_marker(self, *_args):
        if self._drop_point is None:
            return
        index = self.insertion_index(self._drop_point)
        y = (self.visualItemRect(self.item(index)).top() if index < self.count()
             else self.visualItemRect(self.item(self.count() - 1)).bottom() + 1 if self.count() else 0)
        self.insertion_marker.setGeometry(2, max(0, min(y - 2, self.viewport().height() - 4)),
                                          max(0, self.viewport().width() - 4), 4)
        self.insertion_marker.raise_()
        self.insertion_marker.show()

    def dragEnterEvent(self, event):
        if self.accepts_mime(event.mimeData(), event.source()):
            self._drop_point = event.position().toPoint()
            self.refresh_insertion_marker()
            event.acceptProposedAction()
        else:
            self.hide_insertion_marker()
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self.hide_insertion_marker()
        event.accept()

    def dropEvent(self, event):
        self.hide_insertion_marker()
        if not self.accepts_mime(event.mimeData(), event.source()):
            event.ignore()
            return
        key = bytes(event.mimeData().data(RULE_MIME)).decode("utf-8").split(":", 1)[1]
        self.moved.emit(key, self.insertion_index(event.position().toPoint()))
        event.acceptProposedAction()


class ExclusionPanel(QFrame):
    close_requested = Signal()
    sort_changed = Signal(str)

    def __init__(self, controller, dictionary, parent=None):
        super().__init__(parent)
        self.controller, self.dictionary = controller, dictionary
        self.color_enabled = True
        self.tag_dictionary = None
        self.completion_enabled, self.completion_limit = True, 12
        self.sort_mode = "registered"
        self.current_match = None
        self.setObjectName("TaggerCard")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        title_row = QHBoxLayout()
        self.title = QLabel("除外リスト")
        title_row.addWidget(self.title, 1)
        close = QPushButton("×")
        close.setFixedSize(24, 24)
        close.setToolTip("除外リストを閉じる")
        close.setStyleSheet("QPushButton { padding: 0px; }")
        close.clicked.connect(self.close_requested)
        title_row.addWidget(close)
        layout.addLayout(title_row)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(3)
        self.sort_button = QPushButton("並べ替え")
        sort_menu = QMenu(self)
        self.sort_menu = sort_menu
        for mode, label in SORT_LABELS.items():
            sort_menu.addAction(label, lambda mode=mode: self.set_sort(mode))
        self.sort_button.setMenu(sort_menu)
        self.edit_button = QPushButton("編集")
        self.add_button = QPushButton("追加")
        self.delete_button = QPushButton("削除")
        for button in (self.sort_button, self.edit_button, self.add_button, self.delete_button):
            button.setMinimumWidth(0)
            button.setStyleSheet("QPushButton { padding: 4px 5px; }")
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.search = PresetTagSearchBar()
        self.search.setMinimumWidth(0)
        self.search.line.setMinimumWidth(0)
        self.search.line.setPlaceholderText("タグ検索（ハイライト）")
        layout.addWidget(self.search)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ffab91;")
        self.error_label.setMinimumWidth(0)
        layout.addWidget(self.error_label)
        self.list = RuleList()
        layout.addWidget(self.list, 1)

        self.add_button.clicked.connect(lambda: self.open_dialog(False))
        self.edit_button.clicked.connect(lambda: self.open_dialog(True))
        self.delete_button.clicked.connect(lambda: controller.remove_rules(self.selected_ids(), self))
        self.list.itemSelectionChanged.connect(self.sync_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self.open_dialog(True))
        self.list.moved.connect(self.move_rule)
        self.search.line.textChanged.connect(lambda: self.refresh_search(True))
        self.search.exact_check.toggled.connect(lambda: self.refresh_search(True))
        self.search.prev_button.clicked.connect(lambda: self.jump(-1))
        self.search.next_button.clicked.connect(lambda: self.jump(1))
        self.search.line.returnPressed.connect(lambda: self.jump(1))
        self.search.clear_button.clicked.connect(self.search.line.clear)
        controller.changed.connect(self.refresh)
        self.refresh()

    def selected_ids(self):
        return {item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()}

    def ordered_rules(self):
        keys = {
            "registered": lambda r: r.registered_order,
            "manual": lambda r: r.manual_order,
            "name": lambda r: (normalize_tag(r.tag), r.registered_order),
            "category": lambda r: (*self.dictionary.sort_key_for_tag(r.tag), r.registered_order),
        }
        return sorted(self.controller.rules.rules, key=keys[self.sort_mode])

    def set_sort(self, mode):
        if mode not in SORT_LABELS:
            mode = "registered"
        self.sort_mode = mode
        self.refresh()
        self.sort_changed.emit(mode)

    def refresh(self):
        selected = self.selected_ids()
        current = self.list.currentItem()
        current_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        scroll_position = self.list.verticalScrollBar().value()
        blocker = QSignalBlocker(self.list)
        self.list.clear()
        self.rows, self.items_by_id = {}, {}
        for rule in self.ordered_rules():
            item = QListWidgetItem(self.list)
            item.setData(Qt.ItemDataRole.UserRole, rule.rule_id)
            item.setSizeHint(QSize(0, 40))
            item.setToolTip(f"{rule.tag} / {MODE_LABELS[rule.match_mode]}\nID: {rule.rule_id}")
            row = RuleRow(rule.rule_id, rule.tag, rule.match_mode, self.dictionary,
                          self.color_enabled, rule_list=self.list)
            row.selected.connect(self.select_rule)
            row.mode.currentIndexChanged.connect(lambda _index, key=rule.rule_id, combo=row.mode: self.change_mode(key, combo.currentData()))
            row.chip.drag_enabled = not self.controller.issues
            row.mode.setEnabled(not self.controller.issues)
            self.list.setItemWidget(item, row)
            item.setSelected(rule.rule_id in selected)
            self.rows[rule.rule_id], self.items_by_id[rule.rule_id] = row, item
        if current_id in self.items_by_id:
            self.list.selectionModel().setCurrentIndex(self.list.indexFromItem(self.items_by_id[current_id]), QItemSelectionModel.SelectionFlag.NoUpdate)
        if self.list.anchor_id not in self.items_by_id:
            self.list.anchor_id = None
        blocker.unblock()
        self.title.setText(f"除外リスト　{len(self.rows)}件")
        self.sort_button.setToolTip(f"現在: {SORT_LABELS[self.sort_mode]}")
        self.error_label.setText("読込エラー：元ファイルを保護するため編集・保存できません。" if self.controller.issues else "")
        self.error_label.setToolTip(str(self.controller.path) + "\n" + "\n".join(self.controller.issues))
        self.error_label.setVisible(bool(self.controller.issues))
        self.refresh_search()
        if self.current_match is None:
            self.list.verticalScrollBar().setValue(scroll_position)
        self.sync_buttons()

    def sync_buttons(self):
        enabled = bool(self.selected_ids()) and not self.controller.issues
        self.edit_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)
        self.add_button.setEnabled(not self.controller.issues)
        selected = self.selected_ids()
        for key, row in getattr(self, "rows", {}).items():
            row.set_selected(key in selected)

    def select_rule(self, key, modifiers=None):
        self.list.select_rule(key, QApplication.keyboardModifiers() if modifiers is None else modifiers)

    def change_mode(self, key, mode):
        original = next((r for r in self.controller.rules.rules if r.rule_id == key), None)
        if original is None or original.match_mode == mode:
            return
        # 先に表示を元に戻す。保存エラー通知中も未確定の値を表示しない。
        combo = self.rows[key].mode
        with QSignalBlocker(combo):
            combo.setCurrentIndex(combo.findData(original.match_mode))
        self.controller.change_mode(key, mode, self)

    def refresh_search(self, reset=False):
        query = normalize_tag(self.search.line.text())
        exact = self.search.exact_check.isChecked()
        self.matches = [r.rule_id for r in self.ordered_rules() if query and (
            normalize_tag(r.tag) == query if exact else query in normalize_tag(r.tag))]
        if reset or self.current_match not in self.matches:
            self.current_match = self.matches[0] if self.matches else None
        for key, row in self.rows.items():
            row.chip.set_search_marker(self.search.line.text() if key in self.matches else "",
                                       current=key == self.current_match, exact_match=exact and key in self.matches)
        index = self.matches.index(self.current_match) if self.current_match in self.matches else -1
        self.search.set_match_state(len(self.matches), index)
        if self.current_match is not None:
            self.list.scrollToItem(self.items_by_id[self.current_match])

    def jump(self, direction):
        if self.matches:
            self.current_match = self.matches[(self.matches.index(self.current_match) + direction) % len(self.matches)]
            self.refresh_search()

    def move_rule(self, key, insertion):
        ids = [r.rule_id for r in self.ordered_rules()]
        if key not in ids:
            return
        old_index = ids.index(key)
        ids.remove(key)
        ids.insert(max(0, min(len(ids), insertion - (old_index < insertion))), key)
        if self.controller.reorder(ids, self):
            self.set_sort("manual")

    def open_dialog(self, edit=False, tags=None):
        if self.controller.issues:
            return
        selected = self.selected_ids() if edit else set()
        if edit and not selected:
            return
        dialog = ExclusionEditDialog(
            self, rules=[r for r in self.ordered_rules() if r.rule_id in selected],
            dictionary=self.dictionary, tag_dictionary=self.tag_dictionary,
            color_enabled=self.color_enabled, completion_enabled=self.completion_enabled,
            completion_limit=self.completion_limit,
            validator=lambda drafts: self.controller.edited_rules(selected, drafts),
        )
        if tags is not None:
            dialog.input_line.setText(", ".join(dict((normalize_tag(t), t) for t in tags).values()))
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.controller.commit(self.controller.edited_rules(selected, dialog.drafts), self)
        finally:
            dialog.completion.dispose()
            dialog.deleteLater()
