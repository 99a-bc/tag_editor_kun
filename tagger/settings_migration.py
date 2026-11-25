"""旧v1.0設定の読み替えと初回保存時の原本保護。"""

import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from .pixai_v1_backend import MODEL_REPO


LEGACY_V1_REPO = "Mexes/pixai-tagger-v1.0-onnx-fp32-fp16-int8"


def _is_legacy_repo(value):
    return isinstance(value, str) and value.strip().casefold() == LEGACY_V1_REPO.casefold()


def migrate_pixai_v1_settings(data):
    """移行不要なら同じオブジェクトを返す。原本への書き込みは行わない。"""
    selected_legacy = _is_legacy_repo(data.get("tagger_model_repo"))
    profiles = data.get("tagger_model_profiles")
    profiles = profiles if isinstance(profiles, dict) else {}
    legacy_keys = [key for key in profiles if _is_legacy_repo(key)]
    if not selected_legacy and not legacy_keys:
        return data
    migrated = data.copy()
    if selected_legacy:
        migrated["tagger_model_repo"] = MODEL_REPO
    migrated_profiles = {key: value for key, value in profiles.items() if key not in legacy_keys}
    if not isinstance(migrated_profiles.get(MODEL_REPO), dict):
        old_profile = next((profiles[key] for key in legacy_keys if isinstance(profiles[key], dict)), {})
        device = data.get("tagger_device", "auto") if selected_legacy else "auto"
        migrated_profiles[MODEL_REPO] = {"device": device, **old_profile}
    migrated["tagger_model_profiles"] = migrated_profiles
    return migrated


def save_migrated_settings(path: Path, text: str) -> bool:
    """旧設定がディスクにある場合だけ、原本をバックアップして置換する。"""
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        return False
    try:
        data = json.loads(original)
    except (UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or migrate_pixai_v1_settings(data) is data:
        return False

    backup = path.with_name(f"{path.name}.pre-pixai-v1.bak")
    try:
        stream = backup.open("xb")
    except FileExistsError:
        if backup.read_bytes() != original:
            raise OSError(f"異なる旧設定のバックアップが既にあります: {backup}")
    else:
        try:
            with stream:
                stream.write(original)
        except OSError:
            backup.unlink(missing_ok=True)
            raise

    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True
