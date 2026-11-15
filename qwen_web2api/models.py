"""Model ids for Qwen Chat -- chi nhung model CO THAT trong dropdown cua UI.

Popup chinh : Qwen3.7-Plus, Qwen4-Max-Preview, Qwen3.8-Max
"Expand more models": Qwen3.8-Omni-Flash, Qwen3.7-Max, Qwen3.5-Omni-Plus
(id cua 3 model sau suy ra tu nhan UI, chua doi chieu /api/v2/models.)

Khong con alias / hau to -thinking / model legacy. Id la khong biet van di qua
nguyen ban (neu strict_models=false). Che do Thinking/Auto/Fast cua UI chon qua
reasoning_effort (xem ui_mode_for_effort), khong qua ten model.
"""

MODELS = {
    "qwen3.7-plus": {
        "upstream": "qwen3.7-plus",
        "desc": "Qwen3.7-Plus (web default, multimodal)",
    },
    "qwen4.0-max-preview": {
        "upstream": "qwen4.0-max-preview",
        "desc": "Qwen4-Max-Preview (preview of the flagship Qwen4)",
    },
    "qwen3.8-max": {
        "upstream": "qwen3.8-max",
        "desc": "Qwen3.8-Max (flagship Qwen3.8)",
    },
    "qwen3.8-omni-flash": {
        "upstream": "qwen3.8-omni-flash",
        "desc": "Qwen3.8-Omni-Flash (native multimodal: text, image, audio, video)",
    },
    "qwen3.7-max": {
        "upstream": "qwen3.7-max",
        "desc": "Qwen3.7-Max (flagship Qwen3.7, text-only)",
    },
    "qwen3.5-omni-plus": {
        "upstream": "qwen3.5-omni-plus",
        "desc": "Qwen3.5-Omni-Plus (native multimodal)",
    },
}


def ui_mode_for_effort(effort):
    """reasoning_effort -> che do UI: none/minimal/low -> fast, medium -> auto,
    high/xhigh/max -> thinking. Khong gui / khong nhan ra -> None (giu nguyen UI)."""
    if not isinstance(effort, str):
        return None
    e = effort.strip().lower()
    if e in ("none", "minimal", "low"):
        return "fast"
    if e == "medium":
        return "auto"
    if e in ("high", "xhigh", "max"):
        return "thinking"
    return None


def resolve_model(model_name, default="qwen3.7-plus", thinking_default=False):
    """Resolve a requested model id -> (name, upstream_id, think, error).

    Unknown ids pass through. "@think=1"/"@think=0" forces thinking on/off.
    """
    think_override = None
    if not isinstance(model_name, str) or not model_name:
        model_name = default
    if "@think=" in model_name:
        model_name, s = model_name.rsplit("@think=", 1)
        s = s.strip().lower()
        if s in ("1", "true", "on", "yes"):
            think_override = True
        elif s in ("0", "false", "off", "no"):
            think_override = False
        else:
            return None, None, None, f"Invalid think level: {s}"
    cfg = MODELS.get(model_name)
    if cfg is None:
        think = think_override if think_override is not None else thinking_default
        return model_name, model_name, think, None
    think = think_override if think_override is not None else cfg.get("think", thinking_default)
    return model_name, cfg.get("upstream", model_name), think, None
