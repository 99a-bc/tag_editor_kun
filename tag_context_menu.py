"""タグ編集とTaggerで共用する、単一タグのコピー・外部検索。"""
from urllib.parse import quote_plus
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMenu


def add_tag_lookup_actions(menu, tag):
    menu.addAction("コピー", lambda: QApplication.clipboard().setText(tag))
    query = quote_plus("_".join(tag.strip().split()))
    destinations = (
        ("Googleで検索", f"https://www.google.com/search?q={quote_plus(tag)}"),
        ("Danbooruで検索", f"https://danbooru.donmai.us/posts?tags={query}"),
        ("Danbooru Wikiを開く", f"https://danbooru.donmai.us/wiki_pages/{query}"),
        ("Gelbooruで検索", f"https://gelbooru.com/index.php?page=post&s=list&tags={query}"),
        ("Gelbooru Wikiで検索", f"https://gelbooru.com/index.php?page=wiki&s=list&search={query}"),
    )
    for label, url in destinations:
        menu.addAction(label, lambda url=url: QDesktopServices.openUrl(QUrl(url)))


def show_tag_lookup_menu(parent, tag, global_pos):
    menu = QMenu(parent)
    add_tag_lookup_actions(menu, tag)
    try:
        menu.exec(global_pos)
    finally:
        menu.deleteLater()
