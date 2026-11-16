"""Configuration management."""
import json
import os
import threading

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8082,

    # Browser bridge (extension-facing server).
    "bridge_port": 8083,
    "bridge_worker_ttl": 20,
    "bridge_job_timeout": 600,
    "bridge_poll_wait": 20,

    "api_keys": [],
    "cors_origins": [],

    "default_model": "qwen3.7-plus",
    "strict_models": False,
    "thinking": False,

    # Chay moi job trong temporary chat (?temporary-chat=true).
    "temporary_chat": False,
    # Cho phep extension chon model trong UI theo job.model.
    "bridge_select_model": True,
    # Temporary chat khong co New Chat: reload /?temporary-chat=true truoc
    # job khi trang da co tin nhan de tranh lan context. false = chi canh bao.
    "temporary_chat_reload": True,
    # Khi tab dang hien captcha: cho nguoi dung giai (giay) thay vi reload/gui tiep.
    # 0 = tat. (qwen_fix_patch6)
    "captcha_wait_seconds": 300,
    # Chon che do Thinking/Auto/Fast trong UI theo reasoning_effort (none/low=Fast,
    # medium=Auto, high=Thinking; khong gui = giu nguyen UI). false = khong cham UI.
    "bridge_select_mode": True,
    # THU NGHIEM: extension ghi de `model` trong body completion bang model cua job
    # (khong phu thuoc dropdown/tab nen). Mac dinh tat.
    "bridge_force_request_model": False,
    # Tuy chon (khong dat key = 130000): tran ky tu cua FILE prompt (paste->file).
    # > 131072 ky tu thi extension upload qua input[type=file] thay vi paste.
    # "file_prompt_max_chars": 130000,
    # Text prompt gui kem khi prompt dai duoc paste/attach thanh document trong UI.
    "attachment_prompt": "Please read the text and action",

    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "max_body_bytes": 32 * 1024 * 1024,
    # 0 = KHONG cat prompt (Qwen context lon; gioi han that la 131072 ky tu cua paste->file,
    # xem file_prompt_max_chars). Dat so > 0 neu muon ep tran byte. (qwen_fix_patch7)
    "prompt_max_bytes": 0,

    "tool_retry_on_miss": True,
    "tool_retry_attempts": 1,

    "log_requests": True,
    "log_file": None,
    # --- qwen_findings_patch1 -------------------------------------------
    # finding 4: true = chon model / che do UI that bai -> bao loi + retry,
    # khong chay tiep voi model/mode cu cua UI. false = chi warn (cu).
    "bridge_require_model": False,
    "bridge_require_mode": False,
    # finding 5: false = KHONG cat file prompt (mat evidence o cuoi log).
    # File > tran paste thi extension phai upload qua input[type=file].
    "file_prompt_truncate": False,
    # finding 5: 0 = khong gioi han. > 0 -> vuot thi tra 413, khong cat im lang.
    "prompt_hard_max_chars": 0,
    # finding 6: true = tu choi tool_call co arguments hong (nudge retry),
    # false = hanh vi cu (bien thanh {} van tra call cho client).
    "tool_reject_bad_arguments": True,
    # finding 1: phat hien client ngat ket noi (non-stream) de huy job.
    "bridge_cancel_on_disconnect": True,
    # finding 1: so lan /requeue toi da cho 1 job (chong job zombie).
    "bridge_max_requeues": 8,
    # Bat/tat log debug chi tiet (co the dat "debug": true hoac "debug_log": true trong config.json)
    "debug": False,
}

CONFIG = dict(DEFAULT_CONFIG)
_config_lock = threading.Lock()


def load_config(path=None):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    pkg_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    for p in ("./config.json",
              pkg_cfg,
              os.path.expanduser("~/.config/qwen-web2api/config.json")):
        if os.path.exists(p):
            return p
    return None
