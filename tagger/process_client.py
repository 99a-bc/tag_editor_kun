from __future__ import annotations

import json
from pathlib import Path
import sys

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from logger import get_logger


log = get_logger(__name__)

_STARTUP_TIMEOUT_MS = 10_000
_FORCE_STOP_KILL_TIMEOUT_MS = 1_500


class TaggerProcessClient(QObject):
    """Tagger専用の常駐Python子プロセスを管理する親側クライアント。

    Phase 2cでは、常駐子プロセスへ複数画像の逐次推論を依頼する。
    実行中の中止は子プロセス自体を停止し、次回実行で新しい子プロセスを起動する。
    """

    ready = Signal(dict)
    message_received = Signal(dict)
    stderr_received = Signal(str)
    failed = Signal(str)
    exited = Signal(int, int)

    def __init__(self, project_root: Path, parent=None) -> None:
        super().__init__(parent)
        self._project_root = Path(project_root)
        self._process: QProcess | None = None
        self._ready = False
        self._stopping = False
        self._failure_reported = False
        self._request_sequence = 0
        self._stdout_buffer = bytearray()
        self._stderr_buffer = bytearray()

        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.setInterval(_STARTUP_TIMEOUT_MS)
        self._startup_timer.timeout.connect(self._on_startup_timeout)

        self._force_stop_kill_timer = QTimer(self)
        self._force_stop_kill_timer.setSingleShot(True)
        self._force_stop_kill_timer.setInterval(_FORCE_STOP_KILL_TIMEOUT_MS)
        self._force_stop_kill_timer.timeout.connect(self._on_force_stop_kill_timeout)

    @property
    def is_ready(self) -> bool:
        return self._ready and self._process_is_running()

    @property
    def is_active(self) -> bool:
        return self._process_is_running()

    def _process_is_running(self) -> bool:
        return (
            self._process is not None
            and self._process.state() != QProcess.ProcessState.NotRunning
        )

    def ensure_started(self) -> bool:
        """子プロセスを起動する。ready通知は非同期で受け取る。"""
        if self.is_ready:
            return True

        if self.is_active:
            self._report_failed(
                "Tagger子プロセスの起動確認が完了していません。少し待ってから再実行してください。"
            )
            return False

        if not self._project_root.is_dir():
            self._report_failed(
                f"Tagger子プロセスの起動フォルダが見つかりません。\n{self._project_root}"
            )
            return False

        self._ready = False
        self._stopping = False
        self._failure_reported = False
        self._force_stop_kill_timer.stop()
        self._stdout_buffer.clear()
        self._stderr_buffer.clear()

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.setWorkingDirectory(str(self._project_root))
        process.setProgram(sys.executable)
        process.setArguments(["-u", "-m", "tagger.worker_process"])

        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUTF8", "1")
        environment.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(environment)

        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.errorOccurred.connect(self._on_process_error)
        process.finished.connect(self._on_process_finished)

        self._process = process
        log.info(
            "Tagger subprocess starting program=%s args=%s cwd=%s",
            sys.executable,
            ["-u", "-m", "tagger.worker_process"],
            self._project_root,
        )
        process.start()
        self._startup_timer.start()
        return True

    def send_ping(self) -> int | None:
        return self._send({"type": "ping"})

    def send_predict_batch(
        self,
        image_paths: list[Path],
        settings: dict[str, object],
    ) -> int | None:
        """常駐子プロセスへ、緑チェック中画像の逐次バッチ推論を依頼する。"""
        return self._send(
            {
                "type": "predict_batch",
                "paths": [str(Path(path)) for path in image_paths],
                "settings": dict(settings),
            }
        )

    def abort_current_run(self) -> bool:
        """実行中の子プロセスを停止し、必要なら短時間後にkillする。

        バッチ推論中の子プロセスは標準入力を読み戻らないため、cancelメッセージは送らない。
        停止完了は finished シグナルで親側へ通知され、次回実行時に新しい子プロセスを起動する。
        """
        process = self._process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            return False

        self._startup_timer.stop()
        self._ready = False
        self._stopping = True
        self._force_stop_kill_timer.start()
        log.info(
            "Tagger subprocess force-stop requested state=%s; terminate then kill after %dms",
            process.state(),
            _FORCE_STOP_KILL_TIMEOUT_MS,
        )
        process.terminate()
        return True

    def _on_force_stop_kill_timeout(self) -> None:
        process = self._process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            return

        log.warning("Tagger subprocess did not stop after terminate; killing")
        process.kill()

    def request_shutdown(self) -> None:
        """通常終了を依頼する。終了待ちは shutdown_and_wait() が担当する。"""
        if not self._process_is_running():
            return

        self._startup_timer.stop()
        self._force_stop_kill_timer.stop()
        self._stopping = True
        self._send({"type": "shutdown"})

    def shutdown_and_wait(self, timeout_ms: int = 1500) -> None:
        """アプリ終了時に子プロセスを終了し、必要なら強制終了する。"""
        process = self._process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            return

        self.request_shutdown()
        if process.waitForFinished(timeout_ms):
            return

        log.warning("Tagger subprocess did not stop within %dms; terminating", timeout_ms)
        process.terminate()
        if process.waitForFinished(800):
            return

        log.warning("Tagger subprocess did not terminate; killing")
        process.kill()
        process.waitForFinished(800)

    def _on_startup_timeout(self) -> None:
        """readyが返らない子プロセスを終了し、親側の実行ロックを解除できるようにする。"""
        if self.is_ready or not self._process_is_running():
            return

        self._report_failed(
            f"Tagger子プロセスの起動確認が {_STARTUP_TIMEOUT_MS // 1000} 秒以内に完了しませんでした。"
        )

        process = self._process
        if process is None:
            return

        self._ready = False
        self._stopping = True
        log.warning("Tagger subprocess startup timed out; killing")
        process.kill()

    def _send(self, payload: dict[str, object]) -> int | None:
        process = self._process
        if process is None or process.state() != QProcess.ProcessState.Running:
            self._report_failed("Tagger子プロセスと通信できません。")
            return None

        message = dict(payload)
        request_id = message.get("request_id")
        if not isinstance(request_id, int):
            self._request_sequence += 1
            request_id = self._request_sequence
            message["request_id"] = request_id

        encoded = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if process.write(encoded) < 0:
            self._report_failed("Tagger子プロセスへ要求を書き込めませんでした。")
            return None

        return request_id

    def _read_stdout(self) -> None:
        process = self._process
        if process is None:
            return

        self._stdout_buffer.extend(bytes(process.readAllStandardOutput()))
        while True:
            try:
                newline_index = self._stdout_buffer.index(0x0A)
            except ValueError:
                break

            raw_line = bytes(self._stdout_buffer[:newline_index])
            del self._stdout_buffer[: newline_index + 1]
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._report_failed(
                    "Tagger子プロセスから不正な応答を受け取りました。"
                )
                log.warning("Tagger subprocess invalid stdout: %s", line)
                continue

            if not isinstance(message, dict):
                self._report_failed(
                    "Tagger子プロセスから不正な応答を受け取りました。"
                )
                log.warning("Tagger subprocess non-object stdout: %r", message)
                continue

            message_type = str(message.get("type", ""))
            if message_type == "ready":
                self._startup_timer.stop()
                self._ready = True
                log.info(
                    "Tagger subprocess ready pid=%s protocol=%s",
                    message.get("pid", "?"),
                    message.get("protocol", "?"),
                )
                self.ready.emit(message)

            self.message_received.emit(message)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None:
            return

        self._stderr_buffer.extend(bytes(process.readAllStandardError()))
        while True:
            try:
                newline_index = self._stderr_buffer.index(0x0A)
            except ValueError:
                break

            raw_line = bytes(self._stderr_buffer[:newline_index])
            del self._stderr_buffer[: newline_index + 1]
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            log.warning("Tagger subprocess stderr: %s", line)
            self.stderr_received.emit(line)

    def _on_process_error(self, _error) -> None:
        self._startup_timer.stop()
        if self._stopping:
            return

        process = self._process
        detail = process.errorString() if process is not None else "不明なQProcessエラー"
        self._report_failed(f"Tagger子プロセスの起動または通信に失敗しました。\n{detail}")

    def _on_process_finished(self, exit_code: int, exit_status) -> None:
        self._startup_timer.stop()
        self._force_stop_kill_timer.stop()
        process = self._process
        was_stopping = self._stopping
        was_ready = self._ready

        self._ready = False
        self._stopping = False
        self._process = None
        if process is not None:
            process.deleteLater()

        log.info(
            "Tagger subprocess finished exit_code=%d exit_status=%s stopping=%s ready=%s",
            exit_code,
            exit_status,
            was_stopping,
            was_ready,
        )
        # PySide6 6.11 では QProcess.ExitStatus が Enum として渡り、
        # int(exit_status) が TypeError になる。終了検知そのものを例外で止めない。
        raw_exit_status = getattr(exit_status, "value", exit_status)
        try:
            exit_status_value = int(raw_exit_status)
        except (TypeError, ValueError):
            log.warning(
                "Tagger subprocess returned an unknown exit_status=%r",
                exit_status,
            )
            exit_status_value = -1

        # 異常終了は先に通知する。親側はこの通知で未処理画像をエラー化し、
        # Tagger操作ロックを解除する。
        if not was_stopping:
            self._report_failed(
                f"Tagger子プロセスが終了しました。\n終了コード: {exit_code}"
            )

        self.exited.emit(int(exit_code), exit_status_value)

    def _report_failed(self, message: str) -> None:
        if self._failure_reported:
            return
        self._failure_reported = True
        log.warning("%s", message.replace("\n", " | "))
        self.failed.emit(message)
