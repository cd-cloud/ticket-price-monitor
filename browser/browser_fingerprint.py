"""Fixed JS property overrides, not a browser-wide identity guarantee."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path


SCRIPT = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")
SCRIPT_DIGEST = hashlib.sha256(SCRIPT.encode()).hexdigest()[:16]


def default_fingerprint():
    """Fresh, stable Air China defaults. Never fabricate a default GPU identity."""
    return {"mode": "fixed-v1", "hardware_concurrency": 8, "device_memory": 8}


def validate_fingerprint(raw):
    """Finite public fields only; no arbitrary script path/code or randomness."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {
        "mode", "hardware_concurrency", "device_memory", "webgl"
    } or raw.get("mode") != "fixed-v1":
        raise ValueError("固定指纹配置无效。")
    if len(raw) < 2:
        raise ValueError("至少配置一个固定指纹字段。")
    if "hardware_concurrency" in raw:
        value = raw["hardware_concurrency"]
        if type(value) is not int or not 1 <= value <= 64:
            raise ValueError("固定核心数必须是 1–64 的整数。")
    if "device_memory" in raw:
        value = raw["device_memory"]
        if type(value) not in (int, float) or value not in (0.25, 0.5, 1, 2, 4, 8):
            raise ValueError("固定内存参数无效。")
    if "webgl" in raw:
        value = raw["webgl"]
        if not isinstance(value, dict) or set(value) != {"vendor", "renderer"}:
            raise ValueError("WebGL 需要同时设置 vendor 和 renderer。")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 256 or
               any(ord(c) < 32 or ord(c) == 127 for c in v) for v in value.values()):
            raise ValueError("WebGL 固定字段无效。")
    return deepcopy(raw)


def fingerprint_script(raw):
    config = validate_fingerprint(raw)
    if config is None:
        return None
    # json.dumps, never interpolation into quotes or arbitrary code evaluation.
    return "(" + SCRIPT + ")(" + json.dumps(config, ensure_ascii=True) + ");"


def fingerprint_summary(raw):
    if not raw:
        return {"enabled": False}
    return {
        "enabled": True, "mode": raw["mode"], "script_revision": SCRIPT_DIGEST,
        "hardware_concurrency": raw.get("hardware_concurrency"),
        "device_memory": raw.get("device_memory"), "webgl_override": "webgl" in raw,
        "coverage": "new document windows/frames only; workers not patched",
    }
