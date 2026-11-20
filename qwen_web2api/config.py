"""Configuration management."""
import json
import os
import threading

# Che do Thinking/Auto/Fast cua Qwen web ghi thang vao body request (messages[].feature_config),
# lay tu 3 request completion THAT. Chi liet ke key khac nhau giua cac che do; output_schema /
# research_mode / auto_search giong nhau nen khong ghi de. Gia tri None (null trong JSON) = XOA key:
# request Fast that khong co thinking_format.
DEFAULT_MODE_FEATURE_CONFIG = {
    "auto": {"thinking_enabled": True, "auto_thinking": True,
             "thinking_mode": "Auto", "thinking_format": "summary"},
    "thinking": {"thinking_enabled": True, "auto_thinking": False,
                 "thinking_mode": "Thinking", "thinking_format": "summary"},
    "fast": {"thinking_enabled": False, "auto_thinking": False,
             "thinking_mode": "Fast", "thinking_format": None},
}

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
    # 0 = tat.
    "captcha_wait_seconds": 300,
    # Chon che do Thinking/Auto/Fast bang DROPDOWN UI theo reasoning_effort. Mac dinh false:
    # che do da duoc ghi thang vao body request (bridge_force_request_mode), dropdown de
    # treo khi tab khong active. true = van bam dropdown (bridge_require_mode chi co nghia o day).
    "bridge_select_mode": False,
    # THU NGHIEM: extension ghi de `model` trong body completion bang model cua job
    # (khong phu thuoc dropdown/tab nen). Mac dinh tat.
    "bridge_force_request_model": False,
    # Che do Thinking/Auto/Fast: reasoning_effort (none/minimal/low=fast, medium=auto,
    # high/xhigh/max=thinking; khong gui = khong ghi de, body mang che do UI hien tai) ->
    # extension ghi de feature_config cua body completion theo bridge_mode_feature_config[mode]
    # -- khong phu thuoc dropdown UI. Mac dinh = DEFAULT_MODE_FEATURE_CONFIG (request that).
    # config.json ghi de CA dict (khong merge theo mode); mode khong khai bao -> khong ghi de
    # (extension warn 1 lan/job). Gia tri null = xoa key.
    "bridge_force_request_mode": True,
    "bridge_mode_feature_config": {m: dict(v) for m, v in DEFAULT_MODE_FEATURE_CONFIG.items()},
    # Tuy chon (khong dat key = 130000): tran ky tu cua FILE prompt (paste->file).
    # > 131072 ky tu van thu paste-file truoc; cac strategy khac chi la fallback.
    # "file_prompt_max_chars": 130000,
    # Text prompt gui kem khi prompt dai duoc paste/attach thanh document trong UI.
    "attachment_prompt": "Please read the text and action",

    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "max_body_bytes": 32 * 1024 * 1024,
    # 0 = KHONG cat prompt (Qwen context lon; gioi han that la 131072 ky tu cua paste->file,
    # xem file_prompt_max_chars). Dat so > 0 neu muon ep tran byte.
    "prompt_max_bytes": 0,
    # Nguong inline -> file prompt (ky tu). Prompt dai hon -> tach history sang file
    # dinh kem (prepare_large_prompt). 0/sai = MAX_PROMPT_CHARS (130000, hanh vi cu).
    # Mac dinh 30000: inline > ~32k khong on dinh (Qwen doi text thanh document, Send treo).
    "inline_prompt_max_chars": 30000,

    "tool_retry_on_miss": True,
    "tool_retry_attempts": 1,

    "log_requests": True,
    "log_file": None,
    # true = chon model / che do UI that bai -> bao loi + retry,
    # khong chay tiep voi model/mode cu cua UI. false = chi warn (cu).
    "bridge_require_model": False,
    "bridge_require_mode": False,
    # true = job co file ma request cua app
    # KHONG mang attachment (va noi dung chua gui du bang text) -> chan + retry,
    # khong gui cau "Please read..." khi Qwen khong nhan duoc tai lieu.
    "bridge_require_attachment": True,
    # Request THAT co attachment nhung metadata lech file proxy da tao -> CHAN (retry) thay vi
    # chi canh bao. Kind: name (khac ten), size (khac byte), status (dang upload/pending),
    # count (khong dung 1 file), content (extension fetch file tu bridge lech bytes/sha256 so
    # voi file Python ghi), stale (job khong co file ma request van mang attachment; mac
    # dinh TAT vi hinh dang field Qwen chua xac nhan). Chi co tac dung khi
    # bridge_require_attachment=true. [] = chi canh bao (hanh vi cu). Neu Qwen doi ten/tinh
    # size khac -> bo kind do ra khoi danh sach.
    "bridge_attachment_block": ["name", "size", "status", "count", "content"],
    # false = KHONG cat file prompt (mat evidence o cuoi log).
    # File > tran paste van thu paste-file truoc; fallback cac strategy khac.
    "file_prompt_truncate": False,
    # 0 = khong gioi han. > 0 -> vuot thi tra 413, khong cat im lang.
    "prompt_hard_max_chars": 0,
    # true = tu choi tool_call co arguments hong (nudge retry),
    # false = hanh vi cu (bien thanh {} van tra call cho client).
    "tool_reject_bad_arguments": True,
    # phat hien client ngat ket noi (non-stream) de huy job.
    "bridge_cancel_on_disconnect": True,
    # so lan /requeue toi da cho 1 job (chong job zombie).
    "bridge_max_requeues": 8,
    # Bat/tat log debug chi tiet (co the dat "debug": true hoac "debug_log": true trong config.json)
    "debug": False,

    # --- Danh sach sua trong config.json (restart server de ap dung) ---
    # Model hien trong /v1/models + id hop le cho extension. null = dung DEFAULT_MODELS
    # trong models.py. Dang chap nhan: ["id", ...] | [{"id","upstream","desc"}] |
    # {"id": {"upstream","desc"}}. Co gia tri hop le -> THAY HAN danh sach mac dinh.
    "models": None,
    # Tool built-in cua Qwen UI (chay trong sandbox cua Qwen): extension de Qwen tu chay,
    # khong tra native call ve client. Them ten moi khi Qwen them built-in. [] = khong co.
    "qwen_builtin_tools": ["code_interpreter"],

    # --- Log theo phien chay ---
    # true = moi lan bat server ghi 1 file <log_dir>/server-<ngay-gio>-<pid>.log
    # (day du, khong bi gioi han 100 dong nhu GET /debug).
    "log_session_file": False,
    "log_dir": "logs",
    # true = luu ban sao file prompt (qwen_prompt_*.txt) vao <log_dir>/prompts-<phien>/
    # de doi chieu voi nhung gi Qwen nhan (file tam bi xoa sau moi luot). Ton dung luong.
    "log_save_prompt_files": False,
    # Job co file: usage.input_tokens / so ky tu file < nguong nay -> warn "Qwen co the
    # chua doc file". 0 = tat warn (van log ty le).
    "attachment_usage_warn_ratio": 0.05,

    # --- Che do session (qwen_web2api/session.py): chat Qwen hien hanh = history cua client ---
    # true = chi gui phan MOI (delta) vao chat dang mo thay vi replay toan bo history moi luot.
    # Can extension moi (nonce/tab/session event). Khop history that bai -> mo chat moi +
    # replay day du (hanh vi cu), nen bat len khong lam hong gi, chi mat 1 lan replay.
    "bridge_session_mode": False,
    # Gioi han 1 session (vuot -> mo chat moi): tong ky tu da gui, so luot, giay khong dung.
    "session_max_chars": 400000,
    "session_max_turns": 60,
    "session_idle_ttl": 1800,
    # Delta (inline) dai hon nguong nay -> gui bang file dinh kem. 0 = luon inline.
    "session_file_threshold": 30000,
    # Cu moi N luot (delta chi la tin user) them 1 dong nhac format. 0 = tat.
    "session_reminder_every": 8,
    # true = so sanh ca NOI DUNG tin assistant (mac dinh chi dem; Cline hay strip whitespace).
    "session_strict_assistant": False,
    # Regex (re.DOTALL) bo khoi noi dung truoc khi hash, de phan dong (vd <environment_details>
    # cua Cline doi moi luot) khong lam lech chuoi history. Sai regex bi bo qua.
    "session_hash_ignore": ["<environment_details>.*?</environment_details>"],
    # Ghi de tung prompt trong session.DEFAULT_PROMPTS (dict ten -> text).
    "session_prompts": {},
    # THU NGHIEM: nhanh (fork) bang cach ghi de parent_id trong body completion: retry cung
    # request / nudge / client undo|edit khong can mo chat moi. Mac dinh tat.
    "session_fork": False,
    # Khi fork: user_action cua request ("edit" = giong sua tin trong UI; "" = giu nguyen).
    "session_fork_user_action": "edit",
    # Nudge sua loi trong session: "append" = gui them 1 tin nhac vao chat; "fork" = gui lai
    # delta o nhanh anh em (can session_fork=true).
    "session_nudge_mode": "append",
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
