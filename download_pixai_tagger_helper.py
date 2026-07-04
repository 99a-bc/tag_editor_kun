from __future__ import annotations

import getpass
import sys

from huggingface_hub import HfApi, hf_hub_download, snapshot_download


PIXAI_MODEL_REPO = "pixai-labs/pixai-tagger-v0.9"
PIXAI_MODEL_FILENAMES = (
    "model_v0.9.pth",
    "tags_v0.9_13k.json",
    "char_ip_map.json",
)
PIXAI_BASE_ENCODER_REPO = "SmilingWolf/wd-eva02-large-tagger-v3"
PIXAI_BASE_ENCODER_CONFIG_FILENAME = "config.json"


def _validate_token_and_model_access(token: str) -> None:
    """入力トークンの有効性とPixAIモデルへのアクセス可否を確認する。

    トークンはこの関数呼び出しと後続ダウンロードの引数にだけ渡す。
    login() は使わないため、Hugging Faceのログイン状態やトークン保存ファイルは作らない。
    """
    api = HfApi()
    api.whoami(token=token)
    api.model_info(PIXAI_MODEL_REPO, token=token)


def main() -> int:
    print()
    print("Paste a Hugging Face Access Token, then press Enter.")
    print("The token is not displayed or saved. It is used only for this download.")
    token = getpass.getpass("Access Token (hf_...): ").strip()
    if not token:
        print("ERROR: The token was empty.", file=sys.stderr)
        return 1
    if not token.startswith("hf_"):
        print("ERROR: The token must start with 'hf_'.", file=sys.stderr)
        return 1

    try:
        print("Checking token and PixAI model access...")
        _validate_token_and_model_access(token)

        print("Downloading required files...")
        pixai_path = snapshot_download(
            repo_id=PIXAI_MODEL_REPO,
            allow_patterns=list(PIXAI_MODEL_FILENAMES),
            token=token,
        )
        base_config_path = hf_hub_download(
            repo_id=PIXAI_BASE_ENCODER_REPO,
            filename=PIXAI_BASE_ENCODER_CONFIG_FILENAME,
            token=token,
        )
    except Exception:
        print(
            "ERROR: The token is invalid, PixAI model access is not approved, or the download failed.",
            file=sys.stderr,
        )
        return 1
    finally:
        # Never call huggingface_hub.login(); no credential is written by this helper.
        token = ""

    print("OK: PixAI Tagger files were downloaded to the Hugging Face cache.")
    print(f"PixAI files: {pixai_path}")
    print(f"Base encoder config: {base_config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
