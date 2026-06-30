from __future__ import annotations

"""Tagger常駐子プロセスのPhase 2c用エントリポイント。

起動直後はモデルをロードせず、最初の ``predict_batch`` 要求でだけ
WDTaggerBackendを作成する。同じモデル・実効デバイスの次回要求では、
子プロセス内に保持した同一backendを再利用する。

このプロセスはCUDAとモデルを所有する。親側のPySideプロセスは、
JSON Linesで依頼と結果の受け渡しだけを行う。
"""

import gc
import json
import os
from pathlib import Path
import sys
import traceback


PROTOCOL_VERSION = 1
DEFAULT_MODEL_REPO = "SmilingWolf/wd-eva02-large-tagger-v3"
PIXAI_MODEL_REPO = "pixai-labs/pixai-tagger-v0.9"


def emit(message: dict[str, object]) -> None:
    sys.stdout.write(
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


def emit_protocol_error(request_id: object, message: str) -> None:
    payload: dict[str, object] = {"type": "error", "message": message}
    if isinstance(request_id, int):
        payload["request_id"] = request_id
    emit(payload)


def emit_batch_failed(request_id: object, message: str) -> None:
    payload: dict[str, object] = {"type": "batch_failed", "message": message}
    if isinstance(request_id, int):
        payload["request_id"] = request_id
    emit(payload)


def write_stderr(message: str) -> None:
    sys.stderr.write(f"[tagger.worker_process] {message}\n")
    sys.stderr.flush()


def normalize_model_repo(value: object) -> str:
    return str(value or "").strip() or DEFAULT_MODEL_REPO


def resolve_backend_kind(model_repo: str) -> str:
    # モデルIDからbackend種別を決める。未知のIDは従来どおりWDとして扱う。
    if model_repo.casefold() == PIXAI_MODEL_REPO.casefold():
        return "pixai"
    return "wd"


def resolve_effective_device_name(requested_device: object) -> str:
    """UI指定を実効デバイス名へ正規化する。モデルはまだロードしない。"""
    requested = str(requested_device or "auto").strip().lower()
    import torch

    if requested == "gpu":
        if not torch.cuda.is_available():
            raise RuntimeError(
                "GPU が利用できません。CPU を選ぶか、CUDA対応PyTorchを導入してください。"
            )
        return "cuda"
    if requested == "cpu":
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def serialize_candidates(candidates) -> list[dict[str, object]]:
    return [
        {
            "tag": candidate.tag,
            "score": float(candidate.score),
            "category": candidate.category,
            "source_tags": list(candidate.source_tags),
        }
        for candidate in candidates
    ]


def serialize_result(result) -> dict[str, object]:
    return {
        "general_candidates": serialize_candidates(result.general_candidates),
        "character_candidates": serialize_candidates(result.character_candidates),
        "copyright_candidates": serialize_candidates(result.copyright_candidates),
    }


def release_backend(backend) -> None:
    """モデル切替・通常終了時に、現在backendが持つ参照を明示的に外す。"""
    if backend is None:
        return

    device_name = getattr(backend, "device_name", "")
    try:
        backend.model = None
        backend.transform = None
        backend.tag_rows = []
        if hasattr(backend, "_character_ip_map"):
            backend._character_ip_map = {}
        backend.device = None
        backend.device_name = ""
        gc.collect()
        if device_name == "cuda":
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    except Exception:
        write_stderr("backend release failed:\n" + traceback.format_exc())


def parse_batch_paths(value: object) -> list[Path]:
    if not isinstance(value, list):
        raise ValueError("paths はJSON配列である必要があります。")

    paths: list[Path] = []
    seen: set[Path] = set()
    for raw_path in value:
        path_text = str(raw_path or "").strip()
        if not path_text:
            raise ValueError("paths に空の画像パスがあります。")
        path = Path(path_text)
        if path in seen:
            continue
        paths.append(path)
        seen.add(path)

    if not paths:
        raise ValueError("Tagger実行対象がありません。")

    return paths


def ensure_backend(
    backend,
    runtime_key: tuple[str, str, str] | None,
    settings: dict[str, object],
):
    """指定設定に合うbackendを返す。必要な時だけ旧モデルを破棄して載せ替える。"""
    model_repo = normalize_model_repo(settings.get("model_repo"))
    backend_kind = resolve_backend_kind(model_repo)
    requested_device = str(settings.get("device", "auto"))
    effective_device = resolve_effective_device_name(requested_device)
    requested_key = (backend_kind, model_repo, effective_device)

    backend_reused = backend is not None and runtime_key == requested_key
    if backend_reused:
        return backend, runtime_key, True

    release_backend(backend)
    backend = None
    runtime_key = None

    try:
        if backend_kind == "pixai":
            from tagger.pixai_backend import PixAITaggerBackend

            backend = PixAITaggerBackend(model_repo, requested_device)
        else:
            from tagger.wd_backend import WDTaggerBackend

            backend = WDTaggerBackend(model_repo, requested_device)

        backend.load()
        runtime_key = (backend_kind, backend.model_repo, backend.device_name)
    except Exception:
        release_backend(backend)
        raise

    return backend, runtime_key, False


def run_predict_batch(
    *,
    request_id: object,
    paths: list[Path],
    settings: dict[str, object],
    backend,
    runtime_key: tuple[str, str, str] | None,
):
    """1つのバッチ要求を順番に処理し、画像ごとの結果と進捗を返す。"""
    backend, runtime_key, backend_reused = ensure_backend(
        backend,
        runtime_key,
        settings,
    )

    total = len(paths)
    emit(
        {
            "type": "batch_started",
            "request_id": request_id,
            "total": total,
            "device": backend.device_name,
            "backend_reused": backend_reused,
        }
    )

    generated_count = 0
    no_result_count = 0
    error_count = 0

    include_general = bool(settings.get("include_general", True))
    general_threshold = float(settings.get("general_threshold", 0.35))
    include_character = bool(settings.get("include_character", False))
    character_threshold = float(settings.get("character_threshold", 0.85))
    include_copyright = bool(settings.get("include_copyright", False))

    for completed, image_path in enumerate(paths, start=1):
        try:
            if not image_path.is_file():
                raise RuntimeError(f"画像ファイルが見つかりません。\n{image_path}")

            result = backend.predict_image(
                image_path,
                include_general=include_general,
                general_threshold=general_threshold,
                include_character=include_character,
                character_threshold=character_threshold,
                include_copyright=include_copyright,
            )
            candidate_count = len(result.all_candidates)
            if candidate_count:
                generated_count += 1
            else:
                no_result_count += 1

            emit(
                {
                    "type": "batch_item_result",
                    "request_id": request_id,
                    "path": str(image_path),
                    "result": serialize_result(result),
                }
            )
        except Exception as exc:
            error_count += 1
            message = str(exc) or exc.__class__.__name__
            write_stderr(
                "batch item failed "
                f"path={image_path}:\n{traceback.format_exc()}"
            )
            emit(
                {
                    "type": "batch_item_error",
                    "request_id": request_id,
                    "path": str(image_path),
                    "message": message,
                }
            )

        emit(
            {
                "type": "batch_progress",
                "request_id": request_id,
                "completed": completed,
                "total": total,
                "image_name": image_path.name,
            }
        )

    emit(
        {
            "type": "batch_finished",
            "request_id": request_id,
            "device": backend.device_name,
            "backend_reused": backend_reused,
            "summary": {
                "total": total,
                "generated": generated_count,
                "no_result": no_result_count,
                "error": error_count,
            },
        }
    )
    return backend, runtime_key


def main() -> int:
    backend = None
    runtime_key: tuple[str, str, str] | None = None

    emit(
        {
            "type": "ready",
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
        }
    )

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            emit_protocol_error(None, "JSON形式の要求ではありません。")
            continue

        if not isinstance(request, dict):
            emit_protocol_error(None, "要求はJSONオブジェクトである必要があります。")
            continue

        request_id = request.get("request_id")
        request_type = str(request.get("type", ""))

        if request_type == "ping":
            emit(
                {
                    "type": "pong",
                    "request_id": request_id,
                    "pid": os.getpid(),
                }
            )
            continue

        if request_type == "shutdown":
            release_backend(backend)
            emit(
                {
                    "type": "shutdown_complete",
                    "request_id": request_id,
                }
            )
            return 0

        if request_type != "predict_batch":
            emit_protocol_error(
                request_id,
                f"未対応の要求です: {request_type or '(empty)'}",
            )
            continue

        settings = request.get("settings")
        if not isinstance(settings, dict):
            emit_batch_failed(
                request_id,
                "settings はJSONオブジェクトである必要があります。",
            )
            continue

        try:
            paths = parse_batch_paths(request.get("paths"))
            backend, runtime_key = run_predict_batch(
                request_id=request_id,
                paths=paths,
                settings=settings,
                backend=backend,
                runtime_key=runtime_key,
            )
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            write_stderr("predict_batch failed:\n" + traceback.format_exc())
            release_backend(backend)
            backend = None
            runtime_key = None
            emit_batch_failed(request_id, message)

    release_backend(backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
