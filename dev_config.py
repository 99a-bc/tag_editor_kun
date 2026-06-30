from __future__ import annotations

"""
開発・検証用ログの切替設定。

通常運用ではすべて False のままで使用する。
調べたい挙動がある時だけ、必要なカテゴリを True にして起動する。

例:
    フィルタ速度を調べたい:
        DEV_LOGS["PERF_FILTER"] = True

    操作対象プレビューの発動条件を調べたい:
        DEV_LOGS["HOVER_PREVIEW"] = True

    一時的に全部確認したい:
        ENABLE_ALL_DEV_LOGS = True
"""

# True にすると、下のカテゴリ設定に関係なく全開発ログを出す。
ENABLE_ALL_DEV_LOGS = False

DEV_LOGS: dict[str, bool] = {
    # タグフィルタ処理、右側一覧キャッシュ、FlowLayout の処理時間。
    "PERF_FILTER": False,

    # フォルダ初回読込、タグtxt読込、サムネイル生成、初回UI構築の処理時間。
    "PERF_FOLDER_LOAD": False,

    # 左右タグ欄・フィルタ条件チップの折り返しレイアウト再構築詳細。
    "TAG_UI_LAYOUT": False,

    # 左側タグ欄ホバーによる操作対象プレビュー発動／解除の確認。
    "HOVER_PREVIEW": False,

    # 読み込んだ個別画像パスや、フォルダ切替時のFlowLayoutクリア詳細。
    "FOLDER_LOAD_DETAIL": False,

    # フォルダ単位チェックダイアログの継承判定などの詳細。
    "FOLDER_CHECK_DETAIL": False,

    # 青枠画像の選択移動など、細かなUI操作。
    "UI_INTERACTION_DETAIL": False,

    # フィルタで自動解除された黄色チェックの画像index詳細。
    "FILTER_DETAIL": False,

    # WD Taggerのモデル読込・画像ごとの前処理/推論/候補抽出・候補表示更新の処理時間。
    "TAGGER_PERF": False,
}


def dev_log_enabled(category: str) -> bool:
    """指定カテゴリの開発ログを出力するか返す。"""
    return ENABLE_ALL_DEV_LOGS or bool(DEV_LOGS.get(category, False))


def enabled_dev_log_categories() -> list[str]:
    """起動ログへ表示する、有効な開発ログカテゴリ一覧を返す。"""
    if ENABLE_ALL_DEV_LOGS:
        return ["ALL"]
    return [category for category, enabled in DEV_LOGS.items() if enabled]
