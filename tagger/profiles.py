"""モデル別設定の移行・検証。推論ランタイムは読み込まない。"""
from copy import deepcopy
import math
from .pixai_v1_backend import MODEL_REPO, default_categories

WD_REPO = "SmilingWolf/wd-eva02-large-tagger-v3"
OLD_PIXAI_REPO = "pixai-labs/pixai-tagger-v0.9"


def normalize_profiles(value, legacy=None):
    legacy = legacy or {}
    old = dict(device=legacy.get("tagger_device", "auto"), include_general=legacy.get("tagger_include_general", True),
               general_threshold=legacy.get("tagger_general_threshold", .35), include_character=legacy.get("tagger_include_character", False),
               character_threshold=legacy.get("tagger_character_threshold", .85), include_copyright=legacy.get("tagger_include_copyright", False))
    defaults = {WD_REPO: deepcopy(old), OLD_PIXAI_REPO: deepcopy(old),
                MODEL_REPO: dict(device="auto", categories={k: dict(enabled=v.enabled, threshold=v.threshold) for k,v in default_categories().items()})}
    value = value if isinstance(value, dict) else {}
    def probability(raw, default):
        return float(raw) if type(raw) in (float,int) and math.isfinite(raw) and 0 <= raw <= 1 else default
    for repo, base in defaults.items():
        raw = value.get(repo, {})
        raw = raw if isinstance(raw,dict) else {}
        device = raw.get("device", base["device"])
        base["device"] = device if device in ("auto","cpu","gpu") else "auto"
        if repo == MODEL_REPO:
            cats = raw.get("categories", {})
            cats = cats if isinstance(cats,dict) else {}
            for name, setting in base["categories"].items():
                cat = cats.get(name, {})
                cat = cat if isinstance(cat,dict) else {}
                setting["enabled"] = cat.get("enabled") if type(cat.get("enabled")) is bool else setting["enabled"]
                setting["threshold"] = probability(cat.get("threshold"), setting["threshold"])
        else:
            for key in ("include_general","include_character","include_copyright"):
                base[key] = raw.get(key,base[key]) if type(raw.get(key,base[key])) is bool else (key == "include_general")
            for key, default in (("general_threshold",.35),("character_threshold",.85)):
                base[key] = probability(raw.get(key,base[key]),default)
    return defaults
