from __future__ import annotations

import getpass
import sys

from huggingface_hub import login


def main() -> int:
    print()
    print("Paste your Hugging Face Access Token, then press Enter.")
    print("The token will not be displayed. A character count will be shown afterwards.")
    token = getpass.getpass("Access Token (hf_...): ").strip()

    print(f"Received token length: {len(token)} characters.")
    if not token:
        print("ERROR: The token was empty.", file=sys.stderr)
        return 1
    if not token.startswith("hf_"):
        print("WARNING: The token does not start with 'hf_'. Continuing anyway.")

    try:
        login(token=token, add_to_git_credential=False)
    except Exception as exc:
        print(f"ERROR: Hugging Face login failed: {exc}", file=sys.stderr)
        return 1

    print("OK: Hugging Face login completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
