from __future__ import annotations

from pathlib import Path

from .result import TaggerResult


class TaggerSession:
    """Tagger実行結果をImageItemとは分離して保持する一時セッション。"""

    def __init__(self) -> None:
        self.target_paths: set[Path] = set()
        self.no_result_paths: set[Path] = set()
        self.result_by_path: dict[Path, TaggerResult] = {}
        self.error_by_path: dict[Path, str] = {}
        self.cancelled_paths: set[Path] = set()

    @property
    def active(self) -> bool:
        return bool(
            self.target_paths
            or self.no_result_paths
            or self.result_by_path
            or self.error_by_path
            or self.cancelled_paths
        )

    def clear(self) -> None:
        self.target_paths.clear()
        self.no_result_paths.clear()
        self.result_by_path.clear()
        self.error_by_path.clear()
        self.cancelled_paths.clear()

    def start(self, target_paths: list[Path]) -> None:
        self.clear()
        self.target_paths = set(target_paths)

    def set_result(self, path: Path, result: TaggerResult) -> None:
        self.target_paths.add(path)
        self.no_result_paths.discard(path)
        self.result_by_path[path] = result
        self.error_by_path.pop(path, None)
        self.cancelled_paths.discard(path)

    def set_no_result(self, path: Path) -> None:
        self.target_paths.add(path)
        self.no_result_paths.add(path)
        self.result_by_path.pop(path, None)
        self.error_by_path.pop(path, None)
        self.cancelled_paths.discard(path)

    def set_error(self, path: Path, message: str) -> None:
        self.target_paths.add(path)
        self.no_result_paths.discard(path)
        self.result_by_path.pop(path, None)
        self.error_by_path[path] = message
        self.cancelled_paths.discard(path)

    def set_cancelled(self, path: Path) -> None:
        """強制中止により未処理のまま終わった画像を記録する。"""
        self.target_paths.add(path)
        self.no_result_paths.discard(path)
        self.result_by_path.pop(path, None)
        self.error_by_path.pop(path, None)
        self.cancelled_paths.add(path)

    def result_for_path(self, path: Path) -> TaggerResult | None:
        return self.result_by_path.get(path)

    def error_for_path(self, path: Path) -> str | None:
        return self.error_by_path.get(path)

    def pending_paths(self) -> set[Path]:
        """今回の対象のうち、まだ結果・候補なし・エラーへ確定していない画像を返す。"""
        completed_paths = (
            set(self.result_by_path)
            | set(self.no_result_paths)
            | set(self.error_by_path)
            | set(self.cancelled_paths)
        )
        return set(self.target_paths) - completed_paths

    def status_for_path(self, path: Path) -> str | None:
        if not self.active:
            return None
        if path in self.result_by_path:
            return "generated"
        if path in self.error_by_path:
            return "error"
        if path in self.no_result_paths:
            return "no_result"
        if path in self.cancelled_paths:
            return "cancelled"
        if path in self.target_paths:
            return "pending"
        return "skipped"
