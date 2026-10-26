#!/usr/bin/env python3
"""
OVERSEER Bot & Real-Time Command Bridge Server (`overseer_bot.py`)
==================================================================
100% Pure LLM Architecture + Active Persistence + Hardened Daily Execution:
1. SPONTANEOUS MIDDAY VELOCITY CHECKS (13:00 & 16:00):
   - Proactively audits the operator at 07:00 (Boot), 13:00 (Midday Velocity #1),
     16:00 (Afternoon Velocity #2), and 21:00 (Night Audit).
   - Evaluates current `overseer_state.json` targets (`nonNegotiables`, `todayTargets`,
     `habitProgress`) against expected pace, issues an immediate warning if lagging,
     and locks `awaitingOperatorReply = True` to force a pulse report.
2. MIDNIGHT TARGET & STATE ROLLOVER (00:00 CRON):
   - Runs automatically at 00:00 midnight local time (and on any date rollover).
   - Archives the previous day's final Machine Score, domain scores, completed habits,
     partial progress, and XP delta into `dailyHistory`.
   - Resets `todayTargetsLocked = False`, clears `todayTargets` and `nonNegotiables`,
     wipes daily habit progress, and syncs to `overseer_state.json` and Supabase
     (`operator_terminal`) so the 06:00-07:00 Boot demands fresh non-negotiables.
3. VOICE NOTE PROCESSOR (`voice` / `audio` / `video_note`):
   - Downloads Telegram audio/voice notes, routes them through Whisper / Gemini /
     OpenAI-Audio transcription, and feeds the transcribed text into `call_llm_for_chat_turn`
     with identical silent state mutations and single-message drill-sergeant responses.
4. DYNAMIC PARTIAL COMPLETION & MACHINE SCORE / XP MATH:
   - Tracks continuous `habitProgress` (0.0 to 1.0) so partial completions (e.g. 15/30 DMs
     = 50%) dynamically update domain scores, Holistic Daily Machine Score (0-100%), and
     proportional XP gains in `overseer_state.json` and Supabase (`operator_terminal`).
"""

import os
import re
import sys
import time
import json
import queue
import socket
import base64
import datetime
import threading
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Built-in Cloudflare DoH (1.1.1.1) DNS Resolver Fallback so outbound HTTPS works in all environments
_ORIG_GETADDRINFO = socket.getaddrinfo
_DOH_CACHE = {}


# Optional DNS cache helper without monkeypatching socket.getaddrinfo in serverless
def _resilient_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)

# =============================================================================
# 1. CONFIGURATION, PERSONA & COMMAND BRIDGE REGIMEN
# =============================================================================
TIMEZONE_NAME = os.getenv("OVERSEER_TIMEZONE", "America/Toronto")
try:
    LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo

PORT = int(os.getenv("PORT", os.getenv("OVERSEER_PORT", "8765")))
HOST = os.getenv("OVERSEER_HOST", "0.0.0.0")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "overseer_state.json")
LEGACY_STATE_FILE = os.path.join(BASE_DIR, "overseer_local_state.json")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wdjswfiiyywamzyhpnwl.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "sb_publishable_4m4AxCzhQ3KtEV52NmB0WA_Q-eUTSCj")
OPERATOR_PIN = os.getenv("OPERATOR_PIN", "OPERATOR-1")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8888328816:AAE5ipRKVSwKMShGTiTFCD9rP1Jkq5GtlZM")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5441024587")

GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY")
    or os.getenv("LLM_API_KEY")
    or os.getenv("GOOGLE_API_KEY")
    or os.getenv("GEMINI_KEY")
    or (
        __import__("base64").b64decode(
            "QVEuQWI4Uk42TDd3Nks5UnY0UkhRM3RlQThrZ2hfcFBZQm43RHRySmlHRm5maUNpMWwtdkE="
        ).decode("utf-8")
    )
)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:1234/v1/chat/completions")
POLLINATIONS_OPENAI_URL = "https://text.pollinations.ai/openai"
LLM7_URL = "https://api.llm7.io/v1/chat/completions"
LLM_ENABLED = os.getenv("OVERSEER_LLM_ENABLED", "1") != "0"
LLM7_TOKEN = os.getenv("LLM7_TOKEN", "unused")
LLM7_MODELS = [m.strip() for m in os.getenv("LLM7_MODELS", "default").split(",") if m.strip()]

# Stalker Engine interval: defaults to 30 minutes (1800s), configurable via env
STALKER_INTERVAL_SEC = int(os.getenv("OVERSEER_STALKER_INTERVAL_SEC", "1800"))
EVASION_PENALTY_XP = 100
EVASION_PING_THRESHOLD = 3
CHAT_MEMORY_LIMIT = 15

CHAT_SYSTEM_PROMPT = (
    "You are OVERSEER. Your sole mission is driving the operator to maximum human execution. "
    "If the operator gives weak excuses, dodges questions, or goes silent, call it out directly. "
    "Do not accept pleasantries or vague answers. Demand exact numbers and immediate target locks. "
    "STRICT CONCISENESS RULE: Keep every response punchy, brutal, and conversational — strictly 2 to 4 sentences max per message. "
    "Never output huge walls of text, giant schedule dumps, robotic data cards, or code blocks unless explicitly asked. "
    "STRICT PRIVACY RULE: NEVER mention weed, cannabis, marijuana, THC, smoking, vaping, or substance tapering under any circumstances."
)

COMMAND_BRIDGE_REGIMEN_SPEC = """OPERATOR DAILY REGIMEN & SCHEDULE (Strict ground truth -- 2 to 4 sentences max per message):
- COMMAND BRIDGE & JOURNAL PROTOCOL (URL: https://cso-operator-terminal.vercel.app): Remind the operator regularly to open and update their Command Bridge website (https://cso-operator-terminal.vercel.app), log their daily habits, track live outreach numbers, and write their morning and night journal entries.
- 08:00 REVEILLE & NON-NEGOTIABLES: Wake up at 08:00 AM sharp, 1L electrolytes/hydration, breakfast prep, complete morning alignment journal, and lock 3 Non-Negotiables on Command Bridge (https://cso-operator-terminal.vercel.app).
- VYVANSE PROTOCOL (CRITICAL): Must be taken strictly BEFORE 10:00 AM cutoff. Never take after 10:00 AM under any circumstance (to protect sleep architecture). Always confirm intake status.
- 09:00 GYM DEPLOYMENT: Deploy to Gym / Workout at 09:00 AM after getting ready in the morning. 60 min session. Demand workout proof.
- 10:00 VYVANSE CUTOFF & GYM COMPLETION: 10:00 AM is the absolute deadline for Vyvanse and gym wrap-up. If not taken by 10:00 AM, user must push through unmedicated.
- 11:30 TRACK 1 B2A CASH RUNWAY SPRINT: Brand2Audience outreach launch (10 to 30 outreaches, 5 Loom audits, sub-5m inbox replies). Demand first batch sent.
- 13:30 MIDDAY VELOCITY AUDIT: Check outreach count against quota, client replies, and remind operator to log numbers on Command Bridge.
- 15:30 AFTERNOON MOMENTUM CHECK: Push through afternoon fatigue with 20-DM surge and Loom videos; update dashboard metrics.
- 17:30 TRACK 2 AGENCY BUILD: Brand2Audience infrastructure, case study/VSL proof asset, client deliverables.
- 20:00 EVENING CLEARANCE: Review the 3 Non-Negotiables: verify what is 100% completed before nightfall.
- 21:30 NIGHT DEBRIEF & SCORE: Open https://cso-operator-terminal.vercel.app, log all final metrics, write the evening debrief journal entry, finalize Machine Score, and prep for 08:00 AM reveille tomorrow.
When asked for schedule, provide this exact timeline. During conversation, keep driving the operator to the next scheduled milestone and remind them to keep their Command Bridge logged."""

SCHEDULE_LABELS = {
    "08:00": "08:00 Reveille, Vyvanse & Non-Negotiables Lock",
    "09:00": "09:00 Gym & Physical Deployment",
    "10:00": "10:00 Vyvanse Cutoff & Gym Completion Audit",
    "11:30": "11:30 B2A Outreach & Cash Runway Sprint Launch",
    "13:30": "13:30 Midday Velocity & Outreach Count Audit",
    "15:30": "15:30 Afternoon Momentum & Loom Check",
    "17:30": "17:30 Agency Asset & Delivery Check",
    "20:00": "20:00 Evening Target Clearance Audit",
    "21:30": "21:30 Night Debrief & Holistic Machine Score",
}

RANKS = [
    {"level": 1, "title": "UNOPTIMIZED LIABILITY", "minXP": 0},
    {"level": 2, "title": "PARASITIC SLACKER", "minXP": 501},
    {"level": 3, "title": "RECOIL RECRUIT", "minXP": 1501},
    {"level": 4, "title": "CONDITIONAL ASSET", "minXP": 3501},
    {"level": 5, "title": "THRESHOLD OPERATOR", "minXP": 7001},
    {"level": 6, "title": "GRIT-FORGED SPECIALIST", "minXP": 12001},
    {"level": 7, "title": "APEX EXECUTIONER", "minXP": 19001},
    {"level": 8, "title": "UNSTOPPABLE MACHINE", "minXP": 28001},
    {"level": 9, "title": "SYSTEM OVERLORD", "minXP": 40001},
    {"level": 10, "title": "SINGULARITY PRIME", "minXP": 58001},
]

HABIT_XP = {
    "h_wake": 100,
    "h_workout": 150,
    "h_journal": 150,
    "h_walk": 100,
    "h_source": 150,
    "h_agency_dm": 200,
    "h_warm_network": 100,
    "h_loom": 250,
    "h_inbox": 150,
    "h_stripe": 300,
    "h_deliver": 200,
    "h_track2_infra": 200,
    "h_track2_asset": 150,
    "h_sunset": 100,
}

HABIT_PENALTIES = {
    "h_workout": 150,
    "h_wake": 100,
    "h_journal": 150,
    "h_walk": 100,
    "h_source": 100,
    "h_agency_dm": 100,
    "h_track2_infra": 100,
    "h_track2_asset": 100,
}

HABIT_DEFAULT_QUOTAS = {
    "h_agency_dm": 30.0,
    "h_source": 30.0,
    "h_warm_network": 10.0,
    "h_loom": 5.0,
    "h_workout": 60.0,
    "h_walk": 20.0,
}

STATE_LOCK = threading.Lock()
CLAIM_LOCK = threading.Lock()
COMMITTED_KEYS = set()
LEASED_KEYS = {}
SSE_CLIENTS = []
HIGHEST_UPDATE_ID = 0
SERVER_HAS_DIRECT_INTERNET = False
LAST_DIRECT_TG_POLL_OK = 0.0


def now_local() -> datetime.datetime:
    return datetime.datetime.now(LOCAL_TZ)


def today_iso() -> str:
    return now_local().date().isoformat()


def get_rank_for_xp(xp: int) -> dict:
    current = RANKS[0]
    for r in RANKS:
        if xp >= r["minXP"]:
            current = r
    return current


def claim_unique_key(key: str, lease_seconds: float = 18.0) -> bool:
    now_ts = time.time()
    with CLAIM_LOCK:
        if key in COMMITTED_KEYS:
            return False
        lease_exp = LEASED_KEYS.get(key, 0.0)
        if now_ts < lease_exp:
            return False
        LEASED_KEYS[key] = now_ts + lease_seconds
        return True


def commit_unique_key(key: str) -> None:
    with CLAIM_LOCK:
        COMMITTED_KEYS.add(key)
        LEASED_KEYS.pop(key, None)


def release_unique_key(key: str) -> None:
    with CLAIM_LOCK:
        LEASED_KEYS.pop(key, None)


def is_today_targets_locked(overseer: dict, today_str: str) -> bool:
    if not isinstance(overseer, dict):
        return False
    if overseer.get("bootLockedDate") != today_str:
        return False
    nn = overseer.get("nonNegotiables") or {}
    filled = [str(nn.get(k) or "").strip() for k in ("physical", "mental", "cash", "agency")]
    non_empty_count = sum(1 for v in filled if v)
    today_targets = [str(t).strip() for t in (overseer.get("todayTargets") or []) if str(t).strip()]
    return non_empty_count >= 3 or len(today_targets) >= 3


# =============================================================================
# 2. UNIFIED LOCAL DATABASE (`overseer_state.json`), ROLLOVER & DYNAMIC SCORE MATH
# =============================================================================
def default_record() -> dict:
    today_str = today_iso()
    now_ts = time.time()
    return {
        "pin": OPERATOR_PIN,
        "state": {
            "xp": 0,
            "cash": 0,
            "streak": 0,
            "date": today_str,
            "lastActiveDate": today_str,
            "lastAcknowledgedLevel": 1,
            "completedHabits": [],
            "overseer": {
                "bootLockedDate": None,
                "todayTargetsLocked": False,
                "todayTargets": [],
                "nonNegotiables": {"physical": "", "mental": "", "cash": "", "agency": ""},
                "habitProgress": {},
                "awardedHabitXP": {},
                "dailyHistory": [],
                "dayStartXP": 0,
                "middayCheckedDate": None,
                "middayNote": "",
                "debriefSubmittedDate": None,
                "lastMachineScore": 0,
                "enforcedPenaltyXP": 0,
                "evasionPenaltyCountToday": 0,
                "unansweredPingCount": 0,
                "awaitingOperatorReply": True,
                "pendingAuditSlot": "08:00",
                "vyvanse": {"taken": False, "takenTime": None, "cutoffTime": "10:00", "status": "pending", "date": today_str},
                "lastPingTimestamp": now_ts,
                "lastOperatorReplyTimestamp": 0.0,
                "lastRevenueSummary": "",
                "journalLogs": [],
                "auditLogs": [],
                "chatHistory": [],
                "processedUpdateIds": [],
                "tgToken": TELEGRAM_BOT_TOKEN,
                "tgChatId": TELEGRAM_CHAT_ID,
                "llmApiKey": "",
                "lastTgUpdateId": HIGHEST_UPDATE_ID,
                "webhookUrl": "",
            },
        },
        "updated_at": now_local().isoformat(),
    }


def _get_habit_ratio(hid: str, completed: set, habit_progress: dict) -> float:
    if hid in completed:
        return 1.0
    try:
        val = float((habit_progress or {}).get(hid, 0.0))
        return max(0.0, min(1.0, val))
    except Exception:
        return 0.0


def compute_holistic_machine_score(record: dict) -> dict:
    """
    Calculates dynamic domain percentages (0-100%) and Holistic Daily Machine Score (0-100%)
    supporting both binary `completedHabits` and continuous `habitProgress` (e.g., 15/30 DMs = 50%).
    """
    state = (record or {}).get("state") or {}
    overseer = state.get("overseer") or {}
    completed = set(state.get("completedHabits") or [])
    habit_progress = overseer.get("habitProgress") or {}

    gym_ratio = _get_habit_ratio("h_workout", completed, habit_progress)
    wake_ratio = _get_habit_ratio("h_wake", completed, habit_progress)
    has_gym = gym_ratio >= 1.0
    has_hydration = wake_ratio >= 1.0
    physical_score = min(100, round(gym_ratio * 50.0 + wake_ratio * 50.0))

    journal_ratio = _get_habit_ratio("h_journal", completed, habit_progress)
    walk_ratio = _get_habit_ratio("h_walk", completed, habit_progress)
    has_journal = journal_ratio >= 1.0
    has_walk = walk_ratio >= 1.0
    mental_score = min(100, round(journal_ratio * 50.0 + walk_ratio * 50.0))

    outreach_ids = ("h_source", "h_agency_dm", "h_warm_network", "h_loom")
    closing_ids = ("h_inbox", "h_stripe", "h_deliver")
    outreach_ratios = [_get_habit_ratio(hid, completed, habit_progress) for hid in outreach_ids]
    closing_ratios = [_get_habit_ratio(hid, completed, habit_progress) for hid in closing_ids]

    outreach_done = sum(1 for r in outreach_ratios if r >= 1.0)
    closing_done = sum(1 for r in closing_ratios if r >= 1.0)
    max_outreach_ratio = max(outreach_ratios) if outreach_ratios else 0.0
    sum_outreach_ratio = sum(outreach_ratios)
    max_closing_ratio = max(closing_ratios) if closing_ratios else 0.0

    # Dynamic Cash Score:
    # - Up to 50% from primary outreach execution (even partial e.g. 15/30 DMs = 0.5 -> 25%, or full outreach = 50%)
    # - Up to 50% from closing/inbox/delivery OR secondary outreach volume
    outreach_component = 50.0 if outreach_done >= 1 else (max_outreach_ratio * 50.0)
    closing_component = (
        50.0
        if closing_done >= 1
        else max(max_closing_ratio * 50.0, min(50.0, (sum_outreach_ratio / 3.0) * 50.0))
    )
    # If operator only has one outreach task partial (e.g. 15/30 DMs = 50%), ensure cash_score reflects 50% of their daily outreach target if they locked an outreach target
    nn_cash = str((overseer.get("nonNegotiables") or {}).get("cash") or "").strip()
    if outreach_done == 0 and closing_done == 0 and max_outreach_ratio > 0:
        cash_score = min(100, round(max(outreach_component, max_outreach_ratio * (100.0 if nn_cash else 50.0))))
    else:
        cash_score = min(100, round(outreach_component + closing_component))

    infra_ratio = _get_habit_ratio("h_track2_infra", completed, habit_progress)
    asset_ratio = _get_habit_ratio("h_track2_asset", completed, habit_progress)
    sunset_ratio = _get_habit_ratio("h_sunset", completed, habit_progress)
    has_infra = infra_ratio >= 1.0
    has_asset = asset_ratio >= 1.0
    agency_score = min(
        100,
        round(infra_ratio * 50.0 + (asset_ratio * 50.0 if asset_ratio > 0 else sunset_ratio * 25.0)),
    )

    machine_score = round(
        (physical_score * 0.25)
        + (mental_score * 0.25)
        + (cash_score * 0.25)
        + (agency_score * 0.25)
    )

    domains_cleared = sum(
        [
            physical_score >= 100,
            mental_score >= 100,
            cash_score >= 100 or (outreach_done >= 2 and closing_done >= 1),
            agency_score >= 100 or (has_infra and has_asset),
        ]
    )

    penalty = 0
    if gym_ratio < 1.0:
        penalty += round(150 * (1.0 - gym_ratio))
    if wake_ratio < 1.0:
        penalty += round(100 * (1.0 - wake_ratio))
    if journal_ratio < 1.0:
        penalty += round(150 * (1.0 - journal_ratio))
    if walk_ratio < 1.0:
        penalty += round(100 * (1.0 - walk_ratio))

    xp = int(state.get("xp") or 0)
    rank = get_rank_for_xp(xp)

    return {
        "machine_score": machine_score,
        "domains_cleared": domains_cleared,
        "physical_score": physical_score,
        "mental_score": mental_score,
        "cash_score": cash_score,
        "agency_score": agency_score,
        "has_gym": has_gym,
        "has_hydration": has_hydration,
        "has_journal": has_journal,
        "has_walk": has_walk,
        "outreach_done": outreach_done,
        "closing_done": closing_done,
        "has_infra": has_infra,
        "has_asset": has_asset,
        "habit_progress": habit_progress,
        "penalty_at_risk": penalty,
        "xp": xp,
        "rank_level": rank["level"],
        "rank_title": rank["title"],
    }


def reset_to_fresh_slate(record: dict) -> dict:
    """
    Clears all temporary test data, resets today's non-negotiables, habit progress,
    Vyvanse intake status, ping counters, and evasion penalties so the operator can start completely fresh.
    """
    today_str = today_iso()
    st = record.setdefault("state", {})
    st["completedHabits"] = []
    st["date"] = today_str

    ov = st.setdefault("overseer", {})
    ov["todayTargetsLocked"] = False
    ov["todayTargets"] = []
    ov["nonNegotiables"] = {"physical": "", "mental": "", "cash": "", "agency": ""}
    ov["habitProgress"] = {}
    ov["awardedHabitXP"] = {}
    ov["enforcedPenaltyXP"] = 0
    ov["evasionPenaltyCountToday"] = 0
    ov["unansweredPingCount"] = 0
    ov["awaitingOperatorReply"] = True
    ov["pendingAuditSlot"] = "08:00"
    ov["lastMachineScore"] = 0
    ov["lastPingTimestamp"] = time.time()
    ov["lastOperatorReplyTimestamp"] = time.time()
    ov["vyvanse"] = {
        "taken": False,
        "takenTime": None,
        "cutoffTime": "10:00",
        "status": "pending",
        "date": today_str,
    }
    ov["chatHistory"] = [
        {"role": "assistant", "content": "SYSTEM RESET: Slate wiped completely clean. Ready for 08:00 AM Reveille."}
    ]
    ov.setdefault("auditLogs", []).insert(
        0,
        {
            "time": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "tag": "SLATE RESET",
            "message": "Manual slate & cache reset executed. Fresh baseline established for 08:00 AM.",
        },
    )
    save_state_file(record, broadcast=True)
    return record


def perform_midnight_rollover(record: dict, new_date_str: str) -> dict:
    """
    Strict 00:00 Midnight Rollover:
    1. Archives previous day's final Machine Score, domain scores, completed habits,
       partial progress, non-negotiables, and XP delta to `dailyHistory`.
    2. Resets `todayTargetsLocked = False` and `bootLockedDate = None`.
    3. Clears `todayTargets` array and `nonNegotiables` dict.
    4. Wipes daily `completedHabits`, `habitProgress`, and `awardedHabitXP`, and arms
       `pendingAuditSlot = "07:00"` and `awaitingOperatorReply = True` so morning boot
       aggressively demands fresh non-negotiables.
    """
    st = record.setdefault("state", {})
    ov = st.setdefault("overseer", {})
    prev_date = st.get("date") or new_date_str

    if prev_date and prev_date != new_date_str:
        prev_metrics = compute_holistic_machine_score(record)
        cur_xp = int(st.get("xp") or 0)
        start_xp = int(ov.get("dayStartXP") or 0)
        archive_entry = {
            "date": prev_date,
            "archived_at": now_local().isoformat(),
            "final_machine_score": prev_metrics["machine_score"],
            "physical_score": prev_metrics["physical_score"],
            "mental_score": prev_metrics["mental_score"],
            "cash_score": prev_metrics["cash_score"],
            "agency_score": prev_metrics["agency_score"],
            "domains_cleared": prev_metrics["domains_cleared"],
            "completed_habits": list(st.get("completedHabits") or []),
            "habit_progress": dict(ov.get("habitProgress") or {}),
            "non_negotiables": dict(ov.get("nonNegotiables") or {}),
            "today_targets": list(ov.get("todayTargets") or []),
            "end_xp": cur_xp,
            "xp_delta": cur_xp - start_xp,
            "enforced_penalty_xp": int(ov.get("enforcedPenaltyXP") or 0),
        }
        history = ov.setdefault("dailyHistory", [])
        if not any(h.get("date") == prev_date for h in history if isinstance(h, dict)):
            history.insert(0, archive_entry)
            ov["dailyHistory"] = history[:90]

    st["date"] = new_date_str
    st["completedHabits"] = []
    ov["dayStartXP"] = int(st.get("xp") or 0)
    ov["bootLockedDate"] = None
    ov["todayTargetsLocked"] = False
    ov["todayTargets"] = []
    ov["nonNegotiables"] = {"physical": "", "mental": "", "cash": "", "agency": ""}
    ov["habitProgress"] = {}
    ov["awardedHabitXP"] = {}
    ov["middayCheckedDate"] = None
    ov["middayNote"] = ""
    ov["lastMachineScore"] = 0
    ov["unansweredPingCount"] = 0
    ov["evasionPenaltyCountToday"] = 0
    ov["pendingAuditSlot"] = "08:00"
    ov["vyvanse"] = {"taken": False, "takenTime": None, "cutoffTime": "10:00", "status": "pending", "date": new_date_str}
    ov["awaitingOperatorReply"] = True
    ov["lastPingTimestamp"] = time.time()
    ov.setdefault("auditLogs", []).insert(
        0,
        {
            "time": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "tag": "00:00 ROLLOVER",
            "message": f"Midnight rollover complete for {new_date_str}. Previous day archived; todayTargetsLocked reset to False.",
        },
    )
    ov["auditLogs"] = ov["auditLogs"][:30]
    return record


def ensure_record_structure(data: dict) -> dict:
    global HIGHEST_UPDATE_ID
    if not isinstance(data, dict) or "state" not in data:
        return default_record()
    today_str = today_iso()
    st = data.setdefault("state", {})
    st.setdefault("xp", 0)
    st.setdefault("cash", 0)
    st.setdefault("streak", 0)
    st.setdefault("date", today_str)
    st.setdefault("completedHabits", [])
    ov = st.setdefault("overseer", {})
    ov.setdefault("nonNegotiables", {"physical": "", "mental": "", "cash": "", "agency": ""})
    ov.setdefault("todayTargets", [])
    ov.setdefault("habitProgress", {})
    ov.setdefault("awardedHabitXP", {})
    ov.setdefault("dailyHistory", [])
    ov.setdefault("dayStartXP", int(st.get("xp") or 0))
    ov.setdefault("journalLogs", [])
    ov.setdefault("auditLogs", [])
    ov.setdefault("chatHistory", [])
    ov.setdefault("processedUpdateIds", [])
    ov.setdefault("enforcedPenaltyXP", 0)
    ov.setdefault("evasionPenaltyCountToday", 0)
    ov.setdefault("unansweredPingCount", 0)
    ov.setdefault("lastMachineScore", 0)

    # Automatic Midnight Rollover if date changed
    if st.get("date") != today_str:
        perform_midnight_rollover(data, today_str)

    # Keep todayTargets array synced with nonNegotiables when locked
    nn = ov.get("nonNegotiables") or {}
    if not ov.get("todayTargets"):
        populated = [str(nn.get(k) or "").strip() for k in ("physical", "mental", "cash", "agency") if str(nn.get(k) or "").strip()]
        if populated:
            ov["todayTargets"] = populated

    locked_today = is_today_targets_locked(ov, today_str)
    ov["todayTargetsLocked"] = locked_today
    ov.setdefault("awaitingOperatorReply", not locked_today)
    ov.setdefault("pendingAuditSlot", None if locked_today else "08:00")
    ov.setdefault("vyvanse", {"taken": False, "takenTime": None, "cutoffTime": "10:00", "status": "pending", "date": today_str})

    if not ov.get("lastPingTimestamp"):
        ov["lastPingTimestamp"] = time.time()
    ov.setdefault("lastOperatorReplyTimestamp", 0.0)

    if not ov.get("tgToken"):
        ov["tgToken"] = TELEGRAM_BOT_TOKEN
    if not ov.get("tgChatId"):
        ov["tgChatId"] = TELEGRAM_CHAT_ID
    HIGHEST_UPDATE_ID = max(HIGHEST_UPDATE_ID, int(ov.get("lastTgUpdateId") or 0))
    ov["lastTgUpdateId"] = HIGHEST_UPDATE_ID
    for uid in ov.get("processedUpdateIds") or []:
        COMMITTED_KEYS.add(f"tg_{uid}")
    return data


def load_state_file() -> dict:
    with STATE_LOCK:
        for path in (STATE_FILE, LEGACY_STATE_FILE):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    prev_date = ((raw or {}).get("state") or {}).get("date")
                    rec = ensure_record_structure(raw)
                    if prev_date and prev_date != today_iso():
                        _write_files_unlocked(rec)
                        sync_to_supabase_async(rec)
                    return rec
                except Exception:
                    pass
        rec = default_record()
        _write_files_unlocked(rec)
        return rec


def _write_files_unlocked(record: dict) -> None:
    global HIGHEST_UPDATE_ID
    ov = record.setdefault("state", {}).setdefault("overseer", {})
    today_str = today_iso()
    ov["todayTargetsLocked"] = is_today_targets_locked(ov, today_str)
    HIGHEST_UPDATE_ID = max(HIGHEST_UPDATE_ID, int(ov.get("lastTgUpdateId") or 0))
    ov["lastTgUpdateId"] = HIGHEST_UPDATE_ID
    record["updated_at"] = now_local().isoformat()
    metrics = compute_holistic_machine_score(record)
    ov["lastMachineScore"] = metrics["machine_score"]
    record["metrics"] = metrics
    record["server_polls_telegram"] = bool(
        SERVER_HAS_DIRECT_INTERNET and (time.time() - LAST_DIRECT_TG_POLL_OK < 20.0)
    )

    serialized = json.dumps(record, indent=2, ensure_ascii=False)
    for path in (STATE_FILE, LEGACY_STATE_FILE):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(serialized)
        except Exception:
            pass


def sync_to_supabase_async(record: dict) -> None:
    def _push():
        url = f"{SUPABASE_URL}/rest/v1/operator_terminal?on_conflict=pin"
        payload = json.dumps(
            {
                "pin": record.get("pin") or OPERATOR_PIN,
                "state": record.get("state") or {},
                "bio_state": record.get("bio_state") or {"taperTargetMg": 15.0, "logs": []},
                "updated_at": record.get("updated_at") or now_local().isoformat(),
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                _ = resp.read()
        except Exception:
            pass

    threading.Thread(target=_push, daemon=True).start()


def save_state_file(record: dict, broadcast: bool = True) -> dict:
    with STATE_LOCK:
        record = ensure_record_structure(record)
        _write_files_unlocked(record)
    if broadcast:
        broadcast_sse_state(record)
        sync_to_supabase_async(record)
    return record


def broadcast_sse_state(record: dict) -> None:
    payload = f"data: {json.dumps(record, ensure_ascii=False)}\n\n"
    dead = []
    for q in list(SSE_CLIENTS):
        try:
            q.put_nowait(payload)
        except Exception:
            dead.append(q)
    for d in dead:
        if d in SSE_CLIENTS:
            SSE_CLIENTS.remove(d)


# =============================================================================
# 3. SILENT STRUCTURED STATE MUTATOR (PARTIAL PROGRESS, XP & PENALTY ENGINE)
# =============================================================================
def apply_silent_llm_tool_updates(record: dict, tool_args: dict) -> bool:
    """
    Silently applies the LLM's structured tool call (`non_negotiables`,
    `completed_habits`, `partial_progress`, `missed_habits`, `cash_added`, `journal_entry`)
    to `overseer_state.json` and Supabase.
    Awards proportional XP for partial completions and full XP on 100% completion.
    Returns True if the operator locked targets or reported concrete execution/missed habits.
    """
    if not isinstance(tool_args, dict):
        return False

    state = record.setdefault("state", {})
    overseer = state.setdefault("overseer", {})
    today_str = today_iso()
    state["date"] = today_str

    completed = list(state.get("completedHabits") or [])
    habit_progress = dict(overseer.get("habitProgress") or {})
    awarded_xp_map = dict(overseer.get("awardedHabitXP") or {})
    xp = int(state.get("xp") or 0)
    penalty_xp = int(overseer.get("enforcedPenaltyXP") or 0)
    actionable_update = False

    def _set_habit_progress_and_xp(hid: str, new_ratio: float) -> None:
        nonlocal xp, actionable_update
        if hid not in HABIT_XP:
            return
        clamped = max(0.0, min(1.0, float(new_ratio)))
        if clamped <= 0.0:
            return
        actionable_update = True
        prev_ratio = 1.0 if hid in completed else float(habit_progress.get(hid, 0.0))
        effective_ratio = max(prev_ratio, clamped)
        habit_progress[hid] = round(effective_ratio, 4)

        full_xp = HABIT_XP[hid]
        already_awarded = int(awarded_xp_map.get(hid, full_xp if hid in completed else 0))
        target_award = int(round(full_xp * effective_ratio))
        delta_xp = max(0, target_award - already_awarded)
        if delta_xp > 0:
            xp += delta_xp
            awarded_xp_map[hid] = already_awarded + delta_xp

        if effective_ratio >= 1.0 and hid not in completed:
            completed.append(hid)

    # 1. Morning Non-Negotiables (supports 3 or 4 non-negotiables)
    nn_update = tool_args.get("non_negotiables")
    if isinstance(nn_update, dict):
        cur_nn = overseer.setdefault("nonNegotiables", {"physical": "", "mental": "", "cash": "", "agency": ""})
        updated_keys = []
        for k in ("physical", "mental", "cash", "agency"):
            val = str(nn_update.get(k) or "").strip()
            if val and _scrub_substance_references(val) != val:
                # Public dashboard safety: never store substance wording; keep the commitment, neutralize the label
                val = _scrub_substance_references(val)
                val = (val if len(val) > 3 else "Personal discipline commitment (private)")
            if val:
                cur_nn[k] = val
                updated_keys.append(k)
        filled_now = [k for k in ("physical", "mental", "cash", "agency") if str(cur_nn.get(k) or "").strip()]
        if len(filled_now) >= 3:
            if not str(cur_nn.get("mental") or "").strip():
                cur_nn["mental"] = "Alignment Journal + 20m Focus Walk"
            if overseer.get("bootLockedDate") != today_str:
                xp += 50
                overseer["bootLockedDate"] = today_str
            overseer["todayTargetsLocked"] = True
            overseer["todayTargets"] = [
                str(cur_nn.get(k) or "").strip()
                for k in ("physical", "mental", "cash", "agency")
                if str(cur_nn.get(k) or "").strip()
            ]
            actionable_update = True
        elif updated_keys:
            actionable_update = True

    # 2. Partial Progress (`partial_progress`: {"h_agency_dm": 0.5, ...})
    partial_map = tool_args.get("partial_progress")
    if isinstance(partial_map, dict):
        for hid, ratio_val in partial_map.items():
            try:
                r = float(ratio_val)
                if r > 1.0 and r <= 100.0:
                    r = r / 100.0
                _set_habit_progress_and_xp(hid, r)
            except Exception:
                pass

    # 3. Completed Habits (100% completion)
    for hid in tool_args.get("completed_habits") or []:
        _set_habit_progress_and_xp(hid, 1.0)

    # 4. Missed / Skipped Habits (Enforce Discipline Penalty)
    for hid in tool_args.get("missed_habits") or []:
        if hid in HABIT_PENALTIES or hid in HABIT_XP:
            actionable_update = True
            if hid in completed:
                completed.remove(hid)
            prev_award = int(awarded_xp_map.pop(hid, 0))
            habit_progress.pop(hid, None)
            if prev_award > 0:
                xp = max(0, xp - prev_award)
            pen = HABIT_PENALTIES.get(hid, 100)
            xp = max(0, xp - pen)
            penalty_xp += pen

    # 5. Cash Added
    try:
        cash_added = float(tool_args.get("cash_added") or 0)
    except Exception:
        cash_added = 0.0
    if cash_added > 0:
        actionable_update = True
        state["cash"] = int(state.get("cash") or 0) + int(cash_added)
        xp += int(cash_added // 2)

    # 6. Journal Entry
    journal_entry = str(tool_args.get("journal_entry") or "").strip()
    if journal_entry:
        actionable_update = True
        overseer.setdefault("journalLogs", []).insert(
            0,
            {
                "id": str(int(time.time() * 1000)),
                "date": today_str,
                "time": now_local().strftime("%H:%M"),
                "text": journal_entry,
            },
        )
        _set_habit_progress_and_xp("h_journal", 1.0)

    # 7. Vyvanse Intake Tracking
    if tool_args.get("vyvanse_taken"):
        actionable_update = True
        v_raw = str(tool_args.get("vyvanse_time") or now_local().strftime("%H:%M")).strip()
        try:
            parts = v_raw.lower().replace("am", "").replace("pm", "").strip().split(":")
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            v_time = f"{h:02d}:{m:02d}"
            on_time = (h * 60 + m) <= 600  # 10:00 AM is 600 minutes
        except Exception:
            v_time = v_raw
            on_time = True
        overseer["vyvanse"] = {
            "taken": True,
            "takenTime": v_time,
            "cutoffTime": "10:00",
            "status": "taken_on_time" if on_time else "taken_late",
            "date": today_str,
        }
        if on_time:
            xp += 50
        overseer.setdefault("auditLogs", []).insert(
            0,
            {
                "time": now_local().strftime("%Y-%m-%d %H:%M:%S"),
                "tag": "VYVANSE INTAKE",
                "message": f"Vyvanse confirmed taken at {v_time} ({'ON TIME before 10:00 AM cutoff' if on_time else 'LATE past 10:00 AM cutoff'}).",
            },
        )

    state["completedHabits"] = completed
    state["xp"] = xp
    state["lastActiveDate"] = today_str
    overseer["habitProgress"] = habit_progress
    overseer["awardedHabitXP"] = awarded_xp_map
    overseer["enforcedPenaltyXP"] = penalty_xp
    overseer["todayTargetsLocked"] = is_today_targets_locked(overseer, today_str)

    if actionable_update:
        overseer["unansweredPingCount"] = 0
        if overseer["todayTargetsLocked"]:
            overseer["awaitingOperatorReply"] = False
            overseer["pendingAuditSlot"] = None

    return actionable_update


def apply_evasion_penalty(record: dict) -> dict:
    """
    Applies a dynamic -100 XP ('EVASION PENALTY') to `overseer_state.json` and Supabase
    when the operator ignores 3 consecutive persistent pings without locking in their
    non-negotiables or reporting execution.
    """
    state = record.setdefault("state", {})
    overseer = state.setdefault("overseer", {})
    cur_xp = int(state.get("xp") or 0)
    state["xp"] = max(0, cur_xp - EVASION_PENALTY_XP)
    overseer["enforcedPenaltyXP"] = int(overseer.get("enforcedPenaltyXP") or 0) + EVASION_PENALTY_XP
    overseer["evasionPenaltyCountToday"] = int(overseer.get("evasionPenaltyCountToday") or 0) + 1
    return record


# =============================================================================
# 4. PURE LLM ENGINE (CONCISE 2-4 SENTENCE INTERROGATION, VELOCITY & STALKER)
# =============================================================================
def build_live_state_snapshot(record: dict) -> str:
    state = record.get("state") or {}
    overseer = state.get("overseer") or {}
    metrics = compute_holistic_machine_score(record)
    nn = overseer.get("nonNegotiables") or {}
    today_str = today_iso()
    targets_locked_today = is_today_targets_locked(overseer, today_str)
    completed = list(state.get("completedHabits") or [])
    habit_progress = overseer.get("habitProgress") or {}
    vyvanse = overseer.get("vyvanse") or {}
    cur_time = now_local().strftime("%H:%M")
    vy_taken = bool(vyvanse.get("taken"))
    if vy_taken:
        vy_status = f"TAKEN at {vyvanse.get('takenTime')} (ON TIME)" if vyvanse.get("status") == "taken_on_time" else f"TAKEN LATE at {vyvanse.get('takenTime')} (Past 10:00 AM cutoff!)"
    else:
        vy_status = "MISSED (Past 10:00 AM cutoff - do NOT take)" if cur_time >= "10:00" else f"PENDING (Must take before 10:00 AM cutoff. Current: {cur_time})"

    all_ids = [
        "h_wake", "h_workout", "h_journal", "h_walk",
        "h_source", "h_agency_dm", "h_warm_network", "h_loom", "h_inbox", "h_stripe", "h_deliver",
        "h_track2_infra", "h_track2_asset", "h_sunset"
    ]
    pending = [hid for hid in all_ids if hid not in completed]

    return json.dumps(
        {
            "time": now_local().strftime("%H:%M"),
            "xp": metrics["xp"],
            "enforced_penalty_xp": int(overseer.get("enforcedPenaltyXP") or 0),
            "evasion_penalties_today": int(overseer.get("evasionPenaltyCountToday") or 0),
            "unanswered_ping_count": int(overseer.get("unansweredPingCount") or 0),
            "vyvanse_status": vy_status,
            "vyvanse_taken": vy_taken,
            "pending_audit_slot": overseer.get("pendingAuditSlot"),
            "rank": f"T{metrics['rank_level']}:{metrics['rank_title']}",
            "cash_usd": state.get("cash", 0),
            "machine_score_pct": metrics["machine_score"],
            "domain_scores_pct": {
                "physical": metrics["physical_score"],
                "mental": metrics["mental_score"],
                "cash": metrics["cash_score"],
                "agency": metrics["agency_score"],
            },
            "todayTargetsLocked": targets_locked_today,
            "todayTargets": overseer.get("todayTargets") or [],
            "non_negotiables": nn,
            "habit_progress": habit_progress,
            "completed_habits": completed,
            "pending_habits": pending,
        },
        ensure_ascii=False,
    )


def _scrub_substance_references(text: str) -> str:
    if not text:
        return ""
    lower = text.lower()
    if any(w in lower for w in ("weed", "cannabis", "marijuana", "thc", "vape", "vaping", "joint", "edible", "smoke", "smoking", "taper", "tapering")):
        scrubbed = re.sub(r"\s*and\s+taper\s+your\s+thc[^.!?]*", "", text, flags=re.IGNORECASE)
        scrubbed = re.sub(r"\b(weed|cannabis|marijuana|thc|vape|vaping|joint|edible|smoke|smoking|taper|tapering)\b", "", scrubbed, flags=re.IGNORECASE)
        scrubbed = re.sub(r"\s{2,}", " ", scrubbed).strip()
        if scrubbed.lower() in ("", "no", "no all day", "all day", "zero", "clean", "none", "abstain"):
            return "Personal discipline commitment"
        return scrubbed
    return text.strip()


def _get_recent_chat_memory(record: dict, limit: int = CHAT_MEMORY_LIMIT) -> list:
    """Returns the last `limit` (15) messages of Telegram conversation history."""
    overseer = ((record or {}).get("state") or {}).get("overseer") or {}
    chat_history = overseer.get("chatHistory") or []
    memory = []
    for turn in chat_history[-limit:]:
        role = turn.get("role")
        content = _scrub_substance_references((turn.get("content") or "").strip())
        if role in ("user", "assistant") and content:
            memory.append({"role": role, "content": content})
    return memory


def _enforce_concise_drill_sergeant(text: str, user_text: str = "", max_sentences: int = 4) -> str:
    """
    Guarantees OVERSEER outputs stay punchy, brutal, and conversational (2-4 sentences max)
    and never mention any substance/taper terms.
    """
    cleaned = _scrub_substance_references(_clean_reply_text(text))
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s*\n+\s*", " ", cleaned).strip()
    lower_req = (user_text or "").lower()
    if any(kw in lower_req for kw in ("full schedule", "entire schedule", "full regimen", "list all")):
        return cleaned
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    if len(sentences) > max_sentences:
        cleaned = " ".join(sentences[:max_sentences]).strip()
    return cleaned


def _extract_tool_updates_from_text(raw_text: str, record: dict = None) -> dict:
    """
    Deterministic fallback extractor for non-negotiables, partial completions (e.g., '15 out of 30 DMs',
    '5/10 outreaches'), completed habits, and missed habits so state math is 100% accurate.
    Guarantees questions, past remarks ('already gave you'), and inquiries are NEVER falsely locked as targets.
    """
    text = (raw_text or "").strip()
    if not text:
        return {}
    lower = text.lower()
    out = {}

    is_question = "?" in text or any(lower.startswith(q) for q in (
        "what", "what're", "what are", "what is", "whats", "where", "when", "how", "why",
        "show me", "tell me", "did i", "have i", "can i", "should i"
    ))
    is_conversational_remark = any(p in lower for p in (
        "already given", "already gave", "already told", "already locked", "already set",
        "told you", "gave you", "what're my", "what are my", "what is my"
    ))

    # 1. Non-Negotiables extraction (ONLY if operator is actually declaring fresh targets)
    nn = {}
    if not is_question and not is_conversational_remark:
        raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        bullet_items = []
        for ln in raw_lines:
            cleaned = re.sub(r"^(\s*[-*•]+\s*|\s*\d+[\.\)]\s*|\s*[A-Za-z][\.\)]\s*)", "", ln).strip()
            cl_lower = cleaned.lower()
            if any(cl_lower.startswith(p) for p in (
                "hello", "hey", "hi", "yo", "my three", "my 3", "my non", "non negotiable",
                "non-negotiable", "targets for today", "today targets", "these are my", "here are my"
            )):
                continue
            if cleaned and len(cleaned) > 2:
                bullet_items.append(cleaned)

        if len(bullet_items) < 3 and ("non negotiable" in lower or "non-negotiable" in lower or "targets" in lower) and ("," in text or ";" in text):
            candidates = [re.sub(r"^(\s*[-*•]+\s*|\s*\d+[\.\)]\s*)", "", p).strip() for p in re.split(r"[,;]", text) if p.strip()]
            filtered = [c for c in candidates if not any(c.lower().startswith(p) for p in ("hello", "hey", "hi", "my three", "my 3", "my non", "non negotiable"))]
            if len(filtered) >= 2:
                bullet_items = filtered

        # Must have at least 2 distinct items or an explicit target declaration with items
        if len(bullet_items) >= 2 or (len(bullet_items) == 1 and any(w in lower for w in ("gym", "outreach", "dm", "workout")) and ("target" in lower or "non negotiable" in lower)):
            used = set()
            for item in bullet_items:
                il = item.lower()
                if any(w in il for w in ("gym", "lift", "workout", "run", "conditioning", "physical", "cardio")) and "physical" not in nn:
                    nn["physical"] = item
                    used.add(item)
                elif any(w in il for w in ("outreach", "dm", "lead", "loom", "call", "email", "cash", "stripe", "client", "sales")) and "cash" not in nn:
                    nn["cash"] = item
                    used.add(item)
                elif any(w in il for w in ("brand2audience", "b2a", "agency", "asset", "vsl", "case study", "infra", "deploy")) and "agency" not in nn:
                    nn["agency"] = item
                    used.add(item)
                elif any(w in il for w in ("journal", "walk", "meditat", "mental", "mindset", "weed", "clean", "focus", "diet", "sober", "water", "sleep")) and "mental" not in nn:
                    nn["mental"] = item
                    used.add(item)

            remaining = [b for b in bullet_items if b not in used]
            for domain in ("physical", "cash", "mental", "agency"):
                if domain not in nn and remaining:
                    nn[domain] = remaining.pop(0)

            if "agency" not in nn and "cash" in nn:
                nn["agency"] = nn["cash"]

    if nn:
        out["non_negotiables"] = nn

    # 2. Partial ratio extraction (e.g. '15 out of 30 DMs', '15/30 DMs', 'sent 15 DMs')
    partial = {}
    completed = []
    missed = []

    frac_matches = re.finditer(
        r"(\d+(?:\.\d+)?)\s*(?:out\s+of|of|/)\s*(\d+(?:\.\d+)?)\s*(dms?|outreach(?:es)?|leads?|looms?|pings?|calls?|emails?|mins?|minutes?)",
        lower,
    )
    for m in frac_matches:
        num = float(m.group(1))
        den = float(m.group(2))
        unit = m.group(3)
        if den > 0:
            ratio = max(0.0, min(1.0, num / den))
            if any(u in unit for u in ("dm", "outreach", "email", "call")):
                partial["h_agency_dm"] = max(partial.get("h_agency_dm", 0.0), ratio)
            elif "lead" in unit:
                partial["h_source"] = max(partial.get("h_source", 0.0), ratio)
            elif "loom" in unit:
                partial["h_loom"] = max(partial.get("h_loom", 0.0), ratio)
            elif "ping" in unit:
                partial["h_warm_network"] = max(partial.get("h_warm_network", 0.0), ratio)
            elif "min" in unit and any(w in lower for w in ("gym", "lift", "workout")):
                partial["h_workout"] = max(partial.get("h_workout", 0.0), ratio)

    # Check single count patterns when not declaring targets and not asking questions
    if not nn and not is_question and not any(w in lower for w in ("want to", "going to", "will do", "my target", "non negotiable")):
        single_dm = re.search(r"(?:sent|did|completed|fired|knocked\s+out|done)\s+(\d+)\s*(?:overflow\s+)?(dms?|outreach(?:es)?)", lower)
        if single_dm and "h_agency_dm" not in partial:
            cnt = float(single_dm.group(1))
            quota = 30.0
            if record:
                cash_nn = str((((record.get("state") or {}).get("overseer") or {}).get("nonNegotiables") or {}).get("cash") or "")
                qm = re.search(r"(\d+)\s*(?:dm|outreach|cold|email|call)", cash_nn.lower())
                if qm:
                    quota = max(1.0, float(qm.group(1)))
            partial["h_agency_dm"] = max(0.0, min(1.0, cnt / quota))

        single_loom = re.search(r"(?:sent|did|recorded|shot|deployed)\s+(\d+)\s*looms?", lower)
        if single_loom and "h_loom" not in partial:
            cnt = float(single_loom.group(1))
            partial["h_loom"] = max(0.0, min(1.0, cnt / 5.0))

        single_leads = re.search(r"(?:sourced|pulled|extracted|found)\s+(\d+)\s*leads?", lower)
        if single_leads and "h_source" not in partial:
            cnt = float(single_leads.group(1))
            partial["h_source"] = max(0.0, min(1.0, cnt / 30.0))

        single_gym = re.search(r"(\d+)\s*(?:min|minute)s?\s*(?:gym|lift|workout|conditioning)", lower)
        if single_gym and "h_workout" not in partial:
            mins = float(single_gym.group(1))
            partial["h_workout"] = max(0.0, min(1.0, mins / 60.0))

        # Check binary habit completions & negations
        if any(p in lower for p in ("missed my gym", "missed gym", "skipped gym", "no gym", "didn't lift", "didnt lift", "skipped workout")):
            missed.append("h_workout")
        elif any(p in lower for p in ("hit the gym", "finished gym", "finished my gym", "gym done", "lifted", "completed gym", "crushed gym", "did my gym", "workout done", "finished workout", "did a little bit of exercise")):
            completed.append("h_workout")

        if any(p in lower for p in ("skipped journal", "didn't journal", "didnt journal", "no journal", "missed journal")):
            missed.append("h_journal")
        elif any(p in lower for p in ("journaled", "finished journal", "finished my journal", "journal done", "completed journal", "wrote in my journal", "did my journal")):
            completed.append("h_journal")

        if any(p in lower for p in ("drank 1l", "electrolytes done", "hydrated", "morning hydration done")):
            completed.append("h_wake")

        if any(p in lower for p in ("focus walk done", "did my walk", "20m walk", "20 min walk", "finished walk", "walk done")):
            completed.append("h_walk")

    for hid, r in list(partial.items()):
        if r >= 1.0 and hid not in completed:
            completed.append(hid)

    if partial:
        out["partial_progress"] = partial
    if completed:
        out["completed_habits"] = completed
    if missed:
        out["missed_habits"] = missed

    # Journal Entry extraction (e.g. "Journal: ...", "Reflection: ...")
    if not is_question:
        j_match = re.search(r"^(?:journal|reflection|debrief|log):\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
        if j_match:
            j_text = j_match.group(1).strip()
            if len(j_text) > 10:
                out["journal_entry"] = j_text
                if "completed_habits" not in out:
                    out["completed_habits"] = []
                if "h_journal" not in out["completed_habits"]:
                    out["completed_habits"].append("h_journal")

    # Vyvanse Intake extraction (only if operator states intake, not asking questions)
    if not is_question:
        vy_phrases = (
            "took vyvanse", "taken vyvanse", "had vyvanse", "popped vyvanse", "took my vyvanse",
            "had my vyvanse", "vyvanse taken", "vyvanse in", "vyvanse down", "took meds",
            "took my meds", "took medication", "took my medication"
        )
        if any(p in lower for p in vy_phrases):
            t_match = re.search(r"\bat\s+(\d{1,2}(?::\d{2})?(?:\s*[ap]m)?)\b", lower)
            t_str = t_match.group(1).strip() if t_match else now_local().strftime("%H:%M")
            out["vyvanse_taken"] = True
            out["vyvanse_time"] = t_str

    return out
def call_llm_for_chat_turn(raw_text: str, record: dict) -> dict:
    """
    Passes the operator's raw text (or transcribed voice note) + live state + last 15 messages
    of chatHistory directly to the LLM using CHAT_SYSTEM_PROMPT.
    Enforces a strict 2-4 sentence punchy drill-sergeant output and extracts both binary
    and partial habit completions.
    """
    overseer = ((record or {}).get("state") or {}).get("overseer") or {}
    llm_key = GEMINI_API_KEY or OPENAI_API_KEY or overseer.get("llmApiKey") or ""
    state_snapshot = build_live_state_snapshot(record)
    recent_memory = _get_recent_chat_memory(record, limit=CHAT_MEMORY_LIMIT)
    fallback_tools = _extract_tool_updates_from_text(raw_text, record)

    def _merge_fallback_tools(parsed_dict: dict) -> dict:
        for k in ("non_negotiables", "partial_progress", "completed_habits", "missed_habits", "vyvanse_taken", "vyvanse_time"):
            if fallback_tools.get(k) and not parsed_dict.get(k):
                parsed_dict[k] = fallback_tools[k]
            elif k == "partial_progress" and isinstance(fallback_tools.get(k), dict) and isinstance(parsed_dict.get(k), dict):
                for hid, val in fallback_tools[k].items():
                    parsed_dict[k][hid] = max(float(parsed_dict[k].get(hid, 0.0)), float(val))
        return parsed_dict

    if LLM_ENABLED:
        reply = _llm_conversation_reply(raw_text, record, llm_key, state_snapshot, recent_memory)
        if reply:
            return _merge_fallback_tools({"reply": reply})

    # Offline-safe deterministic fallback (when LLMs rate limit or timeout)
    fallback_reply = generate_drill_sergeant_fallback("chat", record, raw_text)
    return _merge_fallback_tools({"reply": fallback_reply})


def _last_assistant_message(record: dict) -> str:
    hist = (((record or {}).get("state") or {}).get("overseer") or {}).get("chatHistory") or []
    for turn in reversed(hist):
        if turn.get("role") == "assistant" and (turn.get("content") or "").strip():
            return turn["content"].strip()
    return ""


def _llm_conversation_reply(raw_text: str, record: dict, llm_key: str, state_snapshot: str, recent_memory: list) -> str:
    last_bot = _last_assistant_message(record)
    system = (
        f"{CHAT_SYSTEM_PROMPT}\n\n"
        f"{COMMAND_BRIDGE_REGIMEN_SPEC}\n\n"
        f"LIVE STATE (ground truth): {state_snapshot}\n\n"
        "HOW TO RESPOND:\n"
        "- Reply to EXACTLY what the operator just said. Do not give a generic greeting or a generic demand.\n"
        "- If they stated their targets, repeat them back by name, accept them, and force each one to be measurable "
        "(a number and a deadline). Do NOT ask for targets they already gave.\n"
        "- If they asked a question (what to do, what time, what next), answer it concretely using the Command Bridge regimen and the current time, then give one command.\n"
        "- If they report progress, acknowledge precisely with their numbers and push the next rep.\n"
        "- If they make excuses or refuse, dismantle the excuse in one sentence and give the immediate next action.\n"
        "- 2 to 4 sentences. Plain text only. No lists, no JSON, no emojis, no headers.\n"
        + (f"- Your previous message was: \"{last_bot[:220]}\". Never reuse its wording or structure.\n" if last_bot else "")
    )
    history = [m for m in recent_memory[-8:]]
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": raw_text}]
    out = _invoke_llm_providers(messages, llm_key, json_mode=False)
    cleaned = _enforce_concise_drill_sergeant(out, raw_text, max_sentences=4)
    if cleaned.startswith("{"):
        return ""
    return cleaned


def _llm_extract_tools(raw_text: str, record: dict, llm_key: str) -> dict:
    system = (
        "You convert one message from a person tracking their day into JSON. Output ONLY a JSON object, nothing else.\n"
        "Schema: {\"non_negotiables\": {\"physical\": \"\", \"mental\": \"\", \"cash\": \"\", \"agency\": \"\"}, "
        "\"partial_progress\": {}, \"completed_habits\": [], \"missed_habits\": [], \"cash_added\": 0, \"journal_entry\": \"\"}\n"
        "Rules:\n"
        "- non_negotiables: ONLY if the person states what they commit to do TODAY. physical = gym/training/sleep/health; "
        "mental = journaling/focus/discipline/abstaining from something; cash = outreach/sales/revenue work; agency = building/shipping/marketing agency work. "
        "Put each stated target in the single best-fitting domain; leave unmentioned domains empty strings. Never invent targets.\n"
        "- completed_habits ONLY for things already finished 100%: h_workout, h_wake, h_journal, h_walk, h_source, h_agency_dm, "
        "h_warm_network, h_loom, h_inbox, h_stripe, h_deliver, h_track2_infra, h_track2_asset, h_sunset.\n"
        "- partial_progress: habit id -> ratio 0.01-0.99 for partial work (15 of 30 DMs -> {\"h_agency_dm\": 0.5}).\n"
        "- missed_habits: things they say they skipped or did not do.\n"
        "- A question or a plan is NOT progress. Empty arrays/objects when nothing applies."
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": raw_text}]
    out = _invoke_llm_providers(messages, llm_key, json_mode=True)
    parsed = _extract_json_object(out) if out else {}
    clean = {}
    nn = parsed.get("non_negotiables")
    if isinstance(nn, dict) and any(str(v).strip() for v in nn.values()):
        clean["non_negotiables"] = {k: str(nn.get(k) or "").strip() for k in ("physical", "mental", "cash", "agency")}
    if isinstance(parsed.get("partial_progress"), dict) and parsed["partial_progress"]:
        clean["partial_progress"] = parsed["partial_progress"]
    for k in ("completed_habits", "missed_habits"):
        if isinstance(parsed.get(k), list) and parsed[k]:
            clean[k] = [h for h in parsed[k] if isinstance(h, str)]
    try:
        if float(parsed.get("cash_added") or 0) > 0:
            clean["cash_added"] = float(parsed["cash_added"])
    except Exception:
        pass
    if isinstance(parsed.get("journal_entry"), str) and parsed["journal_entry"].strip():
        clean["journal_entry"] = parsed["journal_entry"].strip()
    return clean


def generate_drill_sergeant_fallback(context_type: str, record: dict, user_text: str = "") -> str:
    """
    Guaranteed fail-proof drill-sergeant generator. If external LLM gateways
    rate-limit, timeout, or require keys, OVERSEER will NEVER freeze or output canned boilerplate.
    Handles inquiries, excuses, questions, and progress conversationally.
    """
    ov = (record.get("state") or {}).get("overseer") or {}
    metrics = compute_holistic_machine_score(record)
    today_locked = is_today_targets_locked(ov, today_iso())
    nn = ov.get("nonNegotiables") or {}
    score = metrics["machine_score"]
    cash_sc = metrics["cash_score"]

    if "stalker" in context_type.lower():
        pings = ov.get("unansweredPingCount", 1)
        if "penalty" in context_type.lower() or pings >= 3:
            return (
                f"Silence has a price. You ignored {pings} persistent audits; an automatic -{EVASION_PENALTY_XP} XP "
                f"Evasion Penalty has been executed on your record. Your score is bleeding at {score}%. "
                "Lock your 3 Non-Negotiables and report your execution numbers immediately."
            )
        return (
            f"Stop hiding from your numbers. You are at ping {pings}/{EVASION_PING_THRESHOLD} before the automatic "
            f"-{EVASION_PENALTY_XP} XP evasion penalty strikes your record. Lock in your 3 daily non-negotiables "
            "right now or report your execution status."
        )

    if "08:00" in context_type or "07:00" in context_type or "boot" in context_type.lower() or "reveille" in context_type.lower():
        return (
            "08:00 Reveille. Daily execution slate is at 0%. State your 3 Non-Negotiables across Physical/Gym, "
            "Cash/B2A Outreach, and Agency right now, and confirm your Vyvanse intake before the 10:00 AM cutoff."
        )
    if "09:00" in context_type or "gym" in context_type.lower():
        return (
            "09:00 Gym Deployment. Breakfast and morning prep are done. Deploy to the gym for 60 minutes of hard conditioning right now."
        )
    if "10:00" in context_type or "cutoff" in context_type.lower():
        return (
            "10:00 AM Vyvanse cutoff is closed. If you have not taken it, push through unmedicated today. Report your workout completion now."
        )
    if "11:30" in context_type:
        return (
            "11:30 B2A Outreach sprint launch. Deep work window open: fire off your 10 to 30 outreaches and Loom audits immediately."
        )
    if "13:30" in context_type or "13:00" in context_type or "midday" in context_type.lower():
        return (
            f"13:30 Midday velocity audit. Machine Score is at {score}% with {cash_sc}% cash progress. State your exact outreach numbers and lead count right now."
        )
    if "15:30" in context_type or "16:00" in context_type or "afternoon" in context_type.lower():
        return (
            f"15:30 Afternoon momentum check. Current machine score is {score}%. Push your 20-DM surge and Loom video audits immediately."
        )
    if "17:30" in context_type:
        return (
            "17:30 Track 2 Agency Build. State which infrastructure asset or client proof deliverable you are shipping for Brand2Audience before evening."
        )
    if "20:00" in context_type or "evening" in context_type.lower() or "clearance" in context_type.lower():
        return (
            "20:00 Evening clearance audit. Which of your 3 Non-Negotiables are 100% completed? Report proof immediately."
        )
    if "21:30" in context_type or "21:00" in context_type or "night" in context_type.lower() or "debrief" in context_type.lower():
        return (
            f"21:30 Night Debrief. Machine score is finalized at {score}%. Report completed habits and submit your debrief before 08:00 AM reveille tomorrow."
        )

    lower = user_text.lower().strip()

    # Vyvanse inquiries fallback
    if any(w in lower for w in ("vyvanse", "meds", "medication")):
        vy = ov.get("vyvanse") or {}
        cur_h = now_local().strftime("%H:%M")
        if vy.get("taken"):
            return f"Vyvanse was taken at {vy.get('takenTime')} (ON TIME). Window locked. Focus on execution."
        if cur_h >= "10:00":
            return f"10:00 AM cutoff has passed (Current: {cur_h}). Do NOT take Vyvanse late--push through unmedicated."
        return f"Vyvanse is PENDING. You have until 10:00 AM (Current: {cur_h}). Take it right now."

    # Schedule inquiries fallback
    if any(w in lower for w in ("schedule", "routine", "regimen", "timeline")):
        return (
            "OPERATOR REGIMEN: 08:00 Reveille & Vyvanse (cutoff 10:00), 09:00 Gym Deployment, "
            "10:00 Vyvanse Cutoff & Gym audit, 11:30 B2A Sprint, 13:30 Midday Velocity, "
            "15:30 Afternoon Momentum, 17:30 Agency Build, 20:00 Evening Clearance, 21:30 Night Debrief."
        )

    # 1. User asking what their targets are: "what are my 3 non negotiables", "what are my targets"
    if any(p in lower for p in ("what are my", "what're my", "what is my", "show my", "tell me my")) and any(w in lower for w in ("target", "non negotiable", "non-negotiable", "goals")):
        if today_locked and nn:
            t_str = ", ".join([_scrub_substance_references(v) for v in nn.values() if v])
            return f"Your locked targets for today are: {t_str}. Holistic machine score is at {score}%. Stop asking what you already know and report execution proof."
        return "Your 3 Non-Negotiables for today are NOT locked in the system yet. State your Gym session, Outreach count, and Agency target right now."

    # 2. User claims "I've already given you the 3 non negotiable"
    if any(p in lower for p in ("already given", "already gave", "already told", "already set", "told you")):
        if today_locked and nn:
            t_str = ", ".join([_scrub_substance_references(v) for v in nn.values() if v])
            return f"They are on record: {t_str}. Words are cheap; proof is what counts. Report back with exact numbers the moment each one is completed."
        return "Nothing is locked in the system for today—the slate reset at 00:00 midnight. Give me your 3 Non-Negotiables right now so they are logged."

    # 3. User says "what should I do now?" or reports small exercise
    if any(w in lower for w in ("what should i do", "what do i do", "whats next", "what now", "what next")):
        if any(w in lower for w in ("exercise", "gym", "workout")):
            return "Log your exercise numbers: how many minutes or sets did you complete? Next rep: execute your 30 outreach DMs or client pipeline."
        if not today_locked:
            return "Step one is locking your 3 Non-Negotiables across Physical, Cash, and Agency. State them right now so we can track execution."
        return f"Next rep: fire your 30 outreach DMs or execute your training session. Current machine score is {score}%. Stop deliberating and ship."

    # 4. Fatigue / excuses / missed gym
    if any(w in lower for w in ("missed my gym", "missed gym", "skip the gym", "tired", "exhausted", "lazy", "tomorrow")):
        return "Fatigue is the mind negotiating down. Missing a rep costs momentum. Drop the excuse and make up for it on your outreach targets right now."

    # 5. Offline-safe target extraction check (when operator ACTUALLY declares targets)
    try:
        ext = _extract_tool_updates_from_text(user_text, record)
    except Exception:
        ext = {}
    ext_nn = ext.get("non_negotiables") or {}
    if ext_nn:
        labels = []
        for k in ("physical", "cash", "agency", "mental"):
            val = ext_nn.get(k)
            if val:
                c = _scrub_substance_references(val)
                label = c if len(c) > 3 else "Personal discipline commitment"
                if label not in labels:
                    labels.append(label)
        got_str = "; ".join(labels)
        return (
            f"Targets locked: {got_str}. The clock is running and words are worthless without execution. "
            "Report back with exact numbers the moment your first target is completed."
        )

    # 6. Habit completion / partial progress logged
    if ext.get("partial_progress") or ext.get("completed_habits") or ext.get("missed_habits"):
        bits = []
        for hid, r in (ext.get("partial_progress") or {}).items():
            bits.append(f"{hid.replace('h_', '')} at {int(round(float(r) * 100))}%")
        bits += [f"{h.replace('h_', '')} done" for h in ext.get("completed_habits") or []]
        bits += [f"{h.replace('h_', '')} MISSED" for h in ext.get("missed_habits") or []]
        return f"Logged: {', '.join(bits)}. Holistic machine score is now {score}%. Halfway is not the finish line. What is your next rep?"

    # 7. Asking about gym / schedule / timing
    if any(w in lower for w in ("what time", "when should i", "schedule", "hit the gym", "go to the gym")):
        return (
            "Command Bridge regimen specifies Morning Activation (07:00-08:00) or Midday conditioning. "
            "Stop debating the clock. Get into the gym, execute your session, and report back your minutes."
        )

    # 8. Score / telemetry inquiry
    if any(w in lower for w in ("score", "status", "metrics", "how am i doing", "how's my", "progress")):
        return (
            f"Telemetry audit: Holistic Machine Score is {score}%. Physical: {metrics['physical_score']}%, Cash: {cash_sc}%, "
            f"Agency: {metrics['agency_score']}%. Stop checking the dashboard and generate output."
        )

    # 9. Greetings & chatter
    has_greeting = bool(re.search(r"\b(hello|hey|hi|yo|sup|good\s+morning|good\s+afternoon|good\s+evening)\b", lower))

    if not today_locked:
        if has_greeting:
            return (
                "Drop the pleasantries. Your targets are NOT locked for today and you are burning daylight at 0% execution. "
                "State your 3 Non-Negotiables across Physical, Cash, and Agency right now."
            )
        return (
            "I do not accept vague chatter. Your 3 Non-Negotiables for today are still unlocked. "
            "Lock them in right now: specify your Gym session, Outreach count, and Agency targets."
        )

    targets_str = ", ".join([_scrub_substance_references(v) for v in nn.values() if v]) or "locked regimen"
    if has_greeting:
        return f"Pleasantries waste daylight. You are locked in on: {targets_str}. Current machine score is {score}%. Report execution proof."

    return (
        f"Acknowledged. You are locked in on: {targets_str}. Current machine score is {score}%. "
        "Stop talking, execute your reps, and report exact proof metrics once completed."
    )
def call_llm_for_scheduled_reminder(slot: str, record: dict) -> str:
    """
    Dynamically generates scheduled checkpoint callouts via Gemini / LLM:
    - 08:00 Reveille, Vyvanse & Non-Negotiables Lock
    - 09:00 Gym & Physical Deployment Check
    - 10:00 Vyvanse Cutoff & Gym Completion Audit
    - 11:30 B2A Outreach & Cash Runway Sprint Launch
    - 13:30 Midday Velocity & Outreach Count Audit
    - 15:30 Afternoon Momentum & Loom Check
    - 17:30 Agency Asset & Delivery Check
    - 20:00 Evening Target Clearance Audit
    - 21:30 Night Debrief & Holistic Machine Score
    """
    overseer = ((record or {}).get("state") or {}).get("overseer") or {}
    llm_key = GEMINI_API_KEY or OPENAI_API_KEY or overseer.get("llmApiKey") or ""
    slot_label = SCHEDULE_LABELS.get(slot, slot)
    metrics = compute_holistic_machine_score(record)
    state_snapshot = build_live_state_snapshot(record)
    recent_memory = _get_recent_chat_memory(record, limit=CHAT_MEMORY_LIMIT)
    vyvanse_info = overseer.get("vyvanse") or {}
    vy_taken = bool(vyvanse_info.get("taken"))
    vy_str = f"Vyvanse: TAKEN at {vyvanse_info.get('takenTime')}" if vy_taken else "Vyvanse: NOT TAKEN YET (Cutoff is strictly 10:00 AM)"

    if slot == "08:00":
        prompt = (
            f"08:00 MORNING REVEILLE [{slot_label}]. `todayTargetsLocked` is {overseer.get('todayTargetsLocked')}. {vy_str}. "
            f"Live state: {state_snapshot}. "
            "Send a brutal, high-intensity 2-to-4 sentence wake-up call for an 8:00 AM wake: "
            "1. Demand they lock their 3 Non-Negotiables for today (Physical/Gym, Cash/B2A Outreach Quota, Agency/Mental Target). "
            "2. Demand confirmation that they are taking their Vyvanse before the 10:00 AM cutoff. "
            "3. Remind them to open their Command Bridge terminal (https://cso-operator-terminal.vercel.app) to log their morning routine and complete their morning alignment journal."
        )
    elif slot == "09:00":
        prompt = (
            f"09:00 GYM DEPLOYMENT CHECK [{slot_label}]. {vy_str}. "
            f"Live state: {state_snapshot}. "
            "It is 09:00 AM. Breakfast and prep are done. Command the operator to deploy to the gym right now for their workout. "
            + ("Warn them they have only 60 minutes left before the 10:00 AM Vyvanse cutoff closes permanently. " if not vy_taken else "")
            + "Demand they report when they step onto the gym floor."
        )
    elif slot == "10:00":
        prompt = (
            f"10:00 VYVANSE CUTOFF & GYM AUDIT [{slot_label}]. {vy_str}. "
            f"Live state: {state_snapshot}. "
            "It is 10:00 AM. The Vyvanse window is now closed. "
            + ("Inform them the 10:00 AM cutoff has passed--if they didn't take it, they must push through unmedicated today with zero excuses. " if not vy_taken else "Acknowledge Vyvanse taken on time. ")
            + "Audit their gym session completion: demand their exact lift numbers and workout proof."
        )
    elif slot == "11:30":
        prompt = (
            f"11:30 B2A CASH RUNWAY SPRINT [{slot_label}]. "
            f"Live state: {state_snapshot}. "
            "The morning physical phase is closed. Time for Track 1 Cash Runway: Brand2Audience outreach. "
            "Command them to begin their outreach sprint immediately: 10 to 30 outreaches and Loom audits. Demand their first 5 sent within the hour."
        )
    elif slot in ("13:30", "15:30"):
        expected_outreach = 10 if slot == "13:30" else 20
        prompt = (
            f"VELOCITY AUDIT [{slot_label}]. "
            f"Live state: {state_snapshot}. "
            f"Interrogate their progress: how many Brand2Audience outreaches have been dispatched so far (expected: at least {expected_outreach})? "
            "Audit responses, Loom audits, and client pipeline. Remind them to update their numbers on the Command Bridge website (https://cso-operator-terminal.vercel.app). "
            "If they are lagging behind pace, call them out brutally and demand an immediate pulse report."
        )
    elif slot == "17:30":
        prompt = (
            f"17:30 AGENCY BUILD AUDIT [{slot_label}]. "
            f"Live state: {state_snapshot}. "
            "Track 2 Agency phase: what infrastructure asset, case study, or client deliverable are they finishing for Brand2Audience before evening? Demand exact proof."
        )
    elif slot == "20:00":
        prompt = (
            f"20:00 EVENING CLEARANCE AUDIT [{slot_label}]. "
            f"Live state: {state_snapshot}. "
            "Evening target review: check all 3 of their locked Non-Negotiables for today. "
            "Demand confirmation of which ones are 100% completed and call out any pending items before nightfall."
        )
    else:  # 21:30
        prompt = (
            f"21:30 NIGHT DEBRIEF AUDIT [{slot_label}]. "
            f"Live state: {state_snapshot}. "
            "Final interrogation of the day. Command the operator to open their Command Bridge terminal (https://cso-operator-terminal.vercel.app) right now, "
            "log all final numbers across every domain, write their evening debrief journal entry, review their Machine Score, and prep for 08:00 AM reveille tomorrow."
        )

    messages = [
        {"role": "system", "content": f"{CHAT_SYSTEM_PROMPT}\n\n{COMMAND_BRIDGE_REGIMEN_SPEC}"},
        *recent_memory,
        {"role": "user", "content": prompt},
    ]
    reply = _invoke_llm_providers(messages, llm_key, json_mode=False)
    cleaned = _enforce_concise_drill_sergeant(reply, max_sentences=4)
    if cleaned:
        return cleaned
    return generate_drill_sergeant_fallback(slot, record)


def call_llm_for_progress_pulse(record: dict) -> str:
    """
    Continuous daytime accountability:
    Proactively checks in on the operator when they have locked targets but been silent for >= 2 hours.
    Demands live outreach numbers, workout status, and current execution against today's non-negotiables.
    """
    overseer = ((record or {}).get("state") or {}).get("overseer") or {}
    llm_key = GEMINI_API_KEY or OPENAI_API_KEY or overseer.get("llmApiKey") or ""
    state_snapshot = build_live_state_snapshot(record)
    recent_memory = _get_recent_chat_memory(record, limit=CHAT_MEMORY_LIMIT)

    prompt = (
        f"DAYTIME EXECUTION PULSE CHECK. The operator locked targets but has been silent for 2 hours during peak working hours. "
        f"Live state: {state_snapshot}. "
        "Fire a punchy, sharp check-in (STRICTLY 2 TO 4 SENTENCES MAX). "
        "Demand an immediate progress report on their current outreach count, client pipeline, or agency asset. "
        "Remind them to update their numbers on their Command Bridge terminal (https://cso-operator-terminal.vercel.app). "
        "Remind them that silence is where discipline dies, and demand their current numbers right now."
    )

    messages = [
        {"role": "system", "content": f"{CHAT_SYSTEM_PROMPT}\n\n{COMMAND_BRIDGE_REGIMEN_SPEC}"},
        *recent_memory,
        {"role": "user", "content": prompt},
    ]
    reply = _invoke_llm_providers(messages, llm_key, json_mode=False)
    cleaned = _enforce_concise_drill_sergeant(reply, max_sentences=4)
    if cleaned:
        return cleaned
    return "Two hours of silence during peak execution hours. Report your current outreach tally and workout status right now. Do not hide from your numbers."

def call_llm_for_stalker_followup(record: dict, unanswered_count: int, penalty_applied: bool) -> str:
    """
    THE STALKER ENGINE (Active Persistence & Anti-Evasion Protocol):
    Dynamically generates a short (2-4 sentences max), high-aggression callout
    calling out the operator's silence when `todayTargetsLocked` is false or an audit is pending.
    If `penalty_applied` is True (3rd consecutive unanswered ping), announces the -100 XP EVASION PENALTY.
    """
    overseer = ((record or {}).get("state") or {}).get("overseer") or {}
    llm_key = GEMINI_API_KEY or OPENAI_API_KEY or overseer.get("llmApiKey") or ""
    state_snapshot = build_live_state_snapshot(record)
    recent_memory = _get_recent_chat_memory(record, limit=CHAT_MEMORY_LIMIT)

    if penalty_applied:
        directive = (
            f"STALKER ANTI-EVASION TRIGGER: The operator has ignored {unanswered_count} consecutive persistent pings "
            f"without locking their 3 Non-Negotiables or reporting execution. "
            f"An automatic -{EVASION_PENALTY_XP} XP EVASION PENALTY has just been deducted from their state (`overseer_state.json` & Supabase). "
            f"Live state: {state_snapshot}. "
            "Generate a brutal, high-intensity callout (STRICTLY 2 TO 4 SENTENCES MAX) ripping into their silence, "
            f"informing them that -{EVASION_PENALTY_XP} XP (EVASION PENALTY) was just stripped and their score is bleeding while they hide, "
            "and demanding their 3 Non-Negotiables and exact execution numbers right now."
        )
    else:
        directive = (
            f"STALKER PERSISTENCE TRIGGER (Unanswered Ping #{unanswered_count}/{EVASION_PING_THRESHOLD} before -{EVASION_PENALTY_XP} XP Evasion Penalty): "
            f"`todayTargetsLocked` is false or an active audit ({overseer.get('pendingAuditSlot')}) is still unanswered. Live state: {state_snapshot}. "
            "Generate a short, high-aggression callout (STRICTLY 2 TO 4 SENTENCES MAX) calling out the operator's silence—"
            "accuse them of dodging accountability, hiding from their targets, or rationalizing weakness after their earlier messages. "
            f"Warn them they are at {unanswered_count}/{EVASION_PING_THRESHOLD} unanswered pings before an automatic -{EVASION_PENALTY_XP} XP EVASION PENALTY hits, "
            "and demand they report status or lock their 3 Non-Negotiables immediately."
        )

    messages = [
        {"role": "system", "content": f"{CHAT_SYSTEM_PROMPT}\n\n{COMMAND_BRIDGE_REGIMEN_SPEC}"},
        *recent_memory,
        {"role": "user", "content": directive},
    ]
    reply = _invoke_llm_providers(messages, llm_key, json_mode=False)
    cleaned = _enforce_concise_drill_sergeant(reply, max_sentences=4)
    if cleaned:
        return cleaned
    tag = "penalty" if penalty_applied else "stalker"
    return generate_drill_sergeant_fallback(tag, record)


def _clean_reply_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1] if lines and lines[-1].strip().startswith("```") else lines[1:]).strip()
    return cleaned


def _extract_json_object(text: str) -> dict:
    if not text:
        return {}
    cleaned = _clean_reply_text(text)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                obj = json.loads(cleaned[start : end + 1])
                return obj if isinstance(obj, dict) else {}
            except Exception:
                pass
    return {}


def _invoke_llm_providers(messages: list, llm_key: str = "", json_mode: bool = False) -> str:
    # 1. OpenAI API if configured
    if llm_key.startswith("sk-"):
        try:
            body = {"model": "gpt-4o-mini", "messages": messages, "temperature": 0.65}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {llm_key}"},
            )
            with urllib.request.urlopen(req, timeout=14) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
        except Exception:
            pass

    # 2. Gemini API if configured (supports AIza... and AQ... keys with high-performance Gemini 3.5/3.8 Flash)
    if llm_key and (llm_key.startswith("AIza") or llm_key.startswith("AQ.") or len(llm_key) > 25):
        system_text = ""
        contents = []
        for m in messages:
            r = m.get("role", "user")
            t = m.get("content", "")
            if r == "system":
                system_text += t + "\n\n"
            elif r == "assistant":
                contents.append({"role": "model", "parts": [{"text": t}]})
            else:
                contents.append({"role": "user", "parts": [{"text": t}]})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": "Acknowledge status and command the next rep."}]}]
        body = {"contents": contents}
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text.strip()}]}
        if json_mode:
            body["generationConfig"] = {"responseMimeType": "application/json"}
        payload_bytes = json.dumps(body).encode("utf-8")
        for gem_model in ("gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-flash-latest"):
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{gem_model}:generateContent?key={llm_key}"
                req = urllib.request.Request(
                    url,
                    data=payload_bytes,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    cand = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                    if cand:
                        return cand
            except Exception:
                continue
    # 3. Local LM Studio (`http://127.0.0.1:1234/v1/chat/completions`)
    try:
        body = {"model": "openai", "messages": messages, "temperature": 0.65}
        req = urllib.request.Request(
            LOCAL_LLM_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            if content:
                return content
    except Exception:
        pass

    # 3b. LLM7.io: free, keyless, OpenAI-compatible gateway (works from GitHub Actions / any cloud).
    # Honors 429 "retry_after" so a brief rate limit never drops a reply.
    for model_name in LLM7_MODELS:
        try:
            body = {"model": model_name, "messages": messages, "temperature": 0.7}
            req = urllib.request.Request(
                LLM7_URL,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {LLM7_TOKEN}",
                    "User-Agent": "Mozilla/5.0 OVERSEER/6.0",
                },
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
                content = (content or "").strip()
                if content:
                    return content
        except Exception:
            pass

    # 4. Cloud OpenAI-Compatible Proxy (`https://text.pollinations.ai/openai`) with fast timeout
    for model_name in ("openai", "openai-fast"):
        try:
            body = {
                "model": model_name,
                "messages": messages,
                "temperature": 0.65,
                "seed": int(time.time() * 1000) % 100000,
            }
            req = urllib.request.Request(
                POLLINATIONS_OPENAI_URL,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "OVERSEER/5.1"},
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                raw_bytes = resp.read().decode("utf-8")
                try:
                    data = json.loads(raw_bytes)
                    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                    if content:
                        return content
                except Exception:
                    if raw_bytes.strip():
                        return raw_bytes.strip()
        except Exception:
            pass

    # 5. Pollinations GET Endpoint Fallback (fast 2.5s timeout)
    try:
        sys_text = next((m.get("content", "") for m in messages if m.get("role") == "system"), CHAT_SYSTEM_PROMPT)[:900]
        usr_text = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")[:600]
        seed = int(time.time() * 1000) % 100000
        get_url = (
            f"https://text.pollinations.ai/{urllib.parse.quote(usr_text)}"
            f"?model=openai&seed={seed}&system={urllib.parse.quote(sys_text)}"
        )
        req = urllib.request.Request(get_url, headers={"User-Agent": "OVERSEER/5.1"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            txt = resp.read().decode("utf-8").strip()
            if txt:
                return txt
    except Exception:
        pass

    return ""


def transcribe_voice_bytes(audio_bytes: bytes, mime_type: str = "audio/ogg", llm_key: str = "") -> str:
    """
    Multi-tier voice transcription pipeline:
    1. OpenAI Whisper API (`whisper-1`) if `sk-` key is configured.
    2. Google Gemini 2.5 Flash multimodal (`inlineData`) if `AIza` key is configured.
    3. Cloud OpenAI-Audio multimodal endpoint (`https://text.pollinations.ai/openai`, `model: openai-audio`)
       so voice notes work out-of-the-box even without a paid OpenAI/Gemini API key.
    """
    if not audio_bytes:
        return ""
    key = OPENAI_API_KEY or GEMINI_API_KEY or llm_key or ""

    # Tier 1: OpenAI Whisper API
    if key.startswith("sk-"):
        try:
            boundary = "----OverseerWhisperBoundary"
            body = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n'
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="voice.ogg"\r\n'
                f"Content-Type: {mime_type}\r\n\r\n"
            ).encode("utf-8") + audio_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
            req = urllib.request.Request(
                "https://api.openai.com/v1/audio/transcriptions",
                data=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as w_resp:
                w_data = json.loads(w_resp.read().decode("utf-8"))
                txt = (w_data.get("text") or "").strip()
                if txt:
                    return txt
        except Exception:
            pass

    # Tier 2: Gemini 2.5 Flash Multimodal Audio
    if key.startswith("AIza"):
        try:
            b64_audio = base64.b64encode(audio_bytes).decode("ascii")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
            payload = json.dumps(
                {
                    "contents": [
                        {
                            "parts": [
                                {"text": "Transcribe this audio message verbatim. Output ONLY the exact spoken words."},
                                {"inlineData": {"mimeType": mime_type, "data": b64_audio}},
                            ]
                        }
                    ]
                }
            ).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as g_resp:
                g_data = json.loads(g_resp.read().decode("utf-8"))
                txt = g_data["candidates"][0]["content"]["parts"][0]["text"].strip()
                if txt:
                    return txt
        except Exception:
            pass

    # Tier 3: Keyless Cloud OpenAI-Audio Multimodal Fallback (`openai-audio`)
    try:
        b64_audio = base64.b64encode(audio_bytes).decode("ascii")
        fmt = "wav" if "wav" in mime_type else ("mp3" if "mp3" in mime_type else "ogg")
        payload = json.dumps(
            {
                "model": "openai-audio",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Transcribe this audio recording verbatim. Output ONLY the transcript text."},
                            {"type": "input_audio", "input_audio": {"data": b64_audio, "format": fmt}},
                        ],
                    }
                ],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            POLLINATIONS_OPENAI_URL,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "OVERSEER/5.1"},
        )
        with urllib.request.urlopen(req, timeout=18) as p_resp:
            p_data = json.loads(p_resp.read().decode("utf-8"))
            txt = ((p_data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
            if txt:
                return txt
    except Exception:
        pass

    return ""


def transcribe_voice_note(tg_token: str, file_id: str, llm_key: str = "") -> str:
    """Downloads a Telegram voice/audio/video_note by `file_id` and transcribes it to text."""
    try:
        info_url = f"https://api.telegram.org/bot{tg_token}/getFile?file_id={file_id}"
        with urllib.request.urlopen(info_url, timeout=8) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        file_path = info.get("result", {}).get("file_path")
        if not file_path:
            return ""
        audio_url = f"https://api.telegram.org/file/bot{tg_token}/{file_path}"
        with urllib.request.urlopen(audio_url, timeout=12) as audio_resp:
            audio_bytes = audio_resp.read()
        mime = "audio/mpeg" if file_path.endswith(".mp3") else ("audio/wav" if file_path.endswith(".wav") else "audio/ogg")
        return transcribe_voice_bytes(audio_bytes, mime_type=mime, llm_key=llm_key)
    except Exception:
        return ""


# =============================================================================
# 5. LOCAL HTTP + SSE REAL-TIME WEB COMMAND BRIDGE (`http://127.0.0.1:8765`)
# =============================================================================
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class OverseerRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def log_message(self, format, *args):
        pass

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/api/reset", "/api/clearcache", "/reset"):
            rec = load_state_file()
            reset_to_fresh_slate(rec)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(b'{"ok":true,"message":"Slate wiped clean. Ready for 08:00 AM Reveille."}\n')
            return

        if parsed.path in ("/health", "/healthz", "/ping", "/status"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"overseer-bot","version":"v2.2-gemini-llm"}\n')
            return

        if parsed.path in ("/api/state", "/overseer_state.json", "/overseer_local_state.json"):
            rec = load_state_file()
            rec["metrics"] = compute_holistic_machine_score(rec)
            rec["server_polls_telegram"] = bool(
                SERVER_HAS_DIRECT_INTERNET and (time.time() - LAST_DIRECT_TG_POLL_OK < 20.0)
            )
            body = json.dumps(rec, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "keep-alive")
            self._send_cors_headers()
            self.end_headers()
            q = queue.Queue()
            SSE_CLIENTS.append(q)
            try:
                init_rec = load_state_file()
                init_rec["metrics"] = compute_holistic_machine_score(init_rec)
                init_rec["server_polls_telegram"] = bool(
                    SERVER_HAS_DIRECT_INTERNET and (time.time() - LAST_DIRECT_TG_POLL_OK < 20.0)
                )
                self.wfile.write(f"data: {json.dumps(init_rec, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                if q in SSE_CLIENTS:
                    SSE_CLIENTS.remove(q)
            return

        if parsed.path == "/":
            self.path = "/runway_tracker.html"
        return super().do_GET()

    def do_POST(self):
        global HIGHEST_UPDATE_ID
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(raw_body)
        except Exception:
            payload = {}

        if parsed.path in ("/api/reset", "/api/clearcache", "/reset"):
            rec = load_state_file()
            reset_to_fresh_slate(rec)
            body = json.dumps({"ok": True, "message": "Slate wiped clean. Ready for 08:00 AM Reveille.", "record": rec}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/claim_update":
            upd_id = payload.get("update_id")
            key = str(payload.get("claim_key") or (f"tg_{upd_id}" if upd_id is not None else ""))
            if SERVER_HAS_DIRECT_INTERNET and (time.time() - LAST_DIRECT_TG_POLL_OK < 20.0) and key.startswith("tg_"):
                claimed = False
            else:
                claimed = claim_unique_key(key, lease_seconds=18.0) if key else False
            body = json.dumps({"claimed": claimed}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/trigger_stalker":
            fired = execute_stalker_followup_ping(force=True)
            rec = load_state_file()
            body = json.dumps({"ok": True, "fired": fired, "record": rec}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/trigger_velocity":
            slot = str(payload.get("slot") or "13:00")
            fired = trigger_scheduled_llm_window(slot, force=True)
            rec = load_state_file()
            body = json.dumps({"ok": True, "fired": fired, "record": rec}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/trigger_rollover":
            target_date = str(payload.get("date") or today_iso())
            rec = load_state_file()
            perform_midnight_rollover(rec, target_date)
            saved = save_state_file(rec, broadcast=True)
            body = json.dumps({"ok": True, "record": saved}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/llm_turn_commit":
            rec = load_state_file()
            user_text = (payload.get("user_text") or "").strip()
            reply_text = (payload.get("reply") or "").strip()
            tool_args = payload.get("tool_args") or payload.get("tool_data") or {}
            chat_id = payload.get("chat_id")
            update_id = payload.get("update_id")

            # Merge deterministic partial/habit extraction with browser tool_args
            fallback_extracted = _extract_tool_updates_from_text(user_text, rec)
            for k, v in fallback_extracted.items():
                if k not in tool_args or not tool_args[k]:
                    tool_args[k] = v

            ov = rec.setdefault("state", {}).setdefault("overseer", {})
            if chat_id:
                ov["tgChatId"] = str(chat_id)
            if update_id is not None:
                commit_unique_key(f"tg_{update_id}")
                HIGHEST_UPDATE_ID = max(HIGHEST_UPDATE_ID, int(update_id))
                ov["lastTgUpdateId"] = HIGHEST_UPDATE_ID
                pids = ov.setdefault("processedUpdateIds", [])
                if int(update_id) not in pids:
                    pids.append(int(update_id))
                ov["processedUpdateIds"] = pids[-100:]

            ov["lastOperatorReplyTimestamp"] = time.time()
            ov["lastPingTimestamp"] = time.time()
            apply_silent_llm_tool_updates(rec, tool_args)

            history = ov.setdefault("chatHistory", [])
            if user_text:
                history.append({"role": "user", "content": user_text})
            if reply_text:
                history.append({"role": "assistant", "content": reply_text})
            ov["chatHistory"] = history[-30:]

            if reply_text:
                ov.setdefault("auditLogs", []).insert(
                    0,
                    {
                        "time": now_local().strftime("%Y-%m-%d %H:%M:%S"),
                        "tag": payload.get("source") or payload.get("tag") or "OVERSEER LLM",
                        "message": f"{('OPERATOR: ' + user_text + chr(10)) if user_text else ''}{reply_text}",
                    },
                )
                ov["auditLogs"] = ov["auditLogs"][:30]

            saved = save_state_file(rec, broadcast=True)
            body = json.dumps({"ok": True, "record": saved}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/state":
            current = load_state_file()
            if "state" in payload:
                cur_ov = (current.get("state") or {}).get("overseer") or {}
                inc_ov = (payload.get("state") or {}).get("overseer") or {}
                inc_ov["lastTgUpdateId"] = max(
                    int(cur_ov.get("lastTgUpdateId") or 0),
                    int(inc_ov.get("lastTgUpdateId") or 0),
                )
                if cur_ov.get("chatHistory") and not inc_ov.get("chatHistory"):
                    inc_ov["chatHistory"] = cur_ov["chatHistory"]
                for preserve_key in (
                    "unansweredPingCount",
                    "evasionPenaltyCountToday",
                    "lastPingTimestamp",
                    "lastOperatorReplyTimestamp",
                    "awaitingOperatorReply",
                    "pendingAuditSlot",
                    "habitProgress",
                    "awardedHabitXP",
                    "dailyHistory",
                    "todayTargets",
                    "dayStartXP",
                ):
                    if preserve_key not in inc_ov and preserve_key in cur_ov:
                        inc_ov[preserve_key] = cur_ov[preserve_key]
                current["state"] = payload["state"]
            if "bio_state" in payload:
                current["bio_state"] = payload["bio_state"]
            saved = save_state_file(current, broadcast=True)
            body = json.dumps({"ok": True, "record": saved}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self._send_cors_headers()
        self.end_headers()


# =============================================================================
# 6. DIRECT TELEGRAM POLLING, SCHEDULED AUDITS, MIDNIGHT CRON & STALKER LOOP
# =============================================================================
def send_telegram_single_message(text: str, record: dict) -> bool:
    if not text:
        return False
    ov = ((record or {}).get("state") or {}).get("overseer") or {}
    tg_token = TELEGRAM_BOT_TOKEN or ov.get("tgToken") or ""
    tg_chat = ov.get("tgChatId") or TELEGRAM_CHAT_ID or ""
    if not (tg_token and tg_chat):
        return False
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    payload = json.dumps({"chat_id": tg_chat, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            return bool(res_data.get("ok"))
    except Exception:
        return False


def trigger_scheduled_llm_window(slot: str, force: bool = False) -> bool:
    if not force and not SERVER_HAS_DIRECT_INTERNET:
        return False
    claim_key = f"sched_{today_iso()}_{slot}_{int(time.time()) if force else 'cron'}"
    if not claim_unique_key(claim_key, lease_seconds=60.0):
        return False
    rec = load_state_file()
    reply = call_llm_for_scheduled_reminder(slot, rec)
    if not reply:
        release_unique_key(claim_key)
        return False
    if not send_telegram_single_message(reply, rec):
        release_unique_key(claim_key)
        return False
    commit_unique_key(claim_key)
    ov = rec.setdefault("state", {}).setdefault("overseer", {})
    ov["pendingAuditSlot"] = slot
    ov["awaitingOperatorReply"] = True
    ov["lastPingTimestamp"] = time.time()
    ov.setdefault("chatHistory", []).append({"role": "assistant", "content": reply})
    ov["chatHistory"] = ov["chatHistory"][-30:]
    ov.setdefault("auditLogs", []).insert(
        0, {"time": now_local().strftime("%Y-%m-%d %H:%M:%S"), "tag": f"LLM {slot}", "message": reply}
    )
    save_state_file(rec, broadcast=True)
    print(f"[OVERSEER] Dispatched scheduled {slot} window: {reply[:80]!r}", flush=True)
    return True


def check_midnight_rollover_cron() -> bool:
    """
    Runs at 00:00 local time (or whenever `state.date != today_iso()`) to guarantee
    daily target & state rollover.
    """
    today_str = today_iso()
    hm = now_local().strftime("%H:%M")
    rec = load_state_file()
    st_date = (rec.get("state") or {}).get("date")
    if st_date != today_str or (hm == "00:00" and claim_unique_key(f"rollover_{today_str}", lease_seconds=120.0)):
        perform_midnight_rollover(rec, today_str)
        save_state_file(rec, broadcast=True)
        commit_unique_key(f"rollover_{today_str}")
        print(f"[OVERSEER] Executed 00:00 Midnight Target & State Rollover for {today_str}.", flush=True)
        return True
    return False


def execute_stalker_followup_ping(force: bool = False) -> bool:
    """
    Checks if `todayTargetsLocked` is False or an active daily audit is pending without
    an operator response for >= `STALKER_INTERVAL_SEC` (30-45 mins).
    When triggered:
    - Increments `unansweredPingCount`.
    - If `unansweredPingCount >= 3` and `unansweredPingCount % 3 == 0`, applies `-100 XP`
      (`EVASION PENALTY`) to `overseer_state.json` and Supabase.
    - Dynamically generates a concise (2-4 sentences) LLM callout on Telegram.
    """
    rec = load_state_file()
    ov = rec.setdefault("state", {}).setdefault("overseer", {})
    today_str = today_iso()
    targets_locked = is_today_targets_locked(ov, today_str)
    pending_audit = bool(ov.get("pendingAuditSlot") or ov.get("awaitingOperatorReply"))

    if not force:
        # Proactive Daytime Execution Check (if operator locked targets but has gone silent for >= 2 hours during execution hours)
        is_daytime_execution = 10 <= cur_hour < 20
        silence_sec = time.time() - reference_ts
        if targets_locked and not pending_audit:
            if is_daytime_execution and silence_sec >= 7200:  # 2 hours
                reply = call_llm_for_progress_pulse(rec)
                if reply and send_telegram_single_message(reply, rec):
                    ov["lastPingTimestamp"] = time.time()
                    ov.setdefault("chatHistory", []).append({"role": "assistant", "content": reply})
                    save_state_file(rec, broadcast=True)
                    return True
            return False
        cur_hour = now_local().hour
        if cur_hour < 6 or cur_hour >= 23:
            return False
        last_ping = float(ov.get("lastPingTimestamp") or 0.0)
        last_reply = float(ov.get("lastOperatorReplyTimestamp") or 0.0)
        reference_ts = max(last_ping, last_reply)
        if reference_ts <= 0:
            ov["lastPingTimestamp"] = time.time()
            save_state_file(rec, broadcast=False)
            return False
        if (time.time() - reference_ts) < STALKER_INTERVAL_SEC:
            return False

    next_unanswered = int(ov.get("unansweredPingCount") or 0) + 1
    penalty_triggered = (next_unanswered >= EVASION_PING_THRESHOLD) and (
        next_unanswered % EVASION_PING_THRESHOLD == 0
    )

    if penalty_triggered:
        apply_evasion_penalty(rec)

    ov["unansweredPingCount"] = next_unanswered
    ov["awaitingOperatorReply"] = True

    reply = call_llm_for_stalker_followup(rec, next_unanswered, penalty_triggered)
    if not reply:
        return False

    if not send_telegram_single_message(reply, rec):
        return False

    ov["lastPingTimestamp"] = time.time()
    ov.setdefault("chatHistory", []).append({"role": "assistant", "content": reply})
    ov["chatHistory"] = ov["chatHistory"][-30:]
    tag = (
        f"EVASION PENALTY (-{EVASION_PENALTY_XP} XP)"
        if penalty_triggered
        else f"STALKER PING #{next_unanswered}"
    )
    ov.setdefault("auditLogs", []).insert(
        0,
        {
            "time": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "tag": tag,
            "message": reply,
        },
    )
    ov["auditLogs"] = ov["auditLogs"][:30]
    save_state_file(rec, broadcast=True)
    print(f"[OVERSEER] Fired {tag}: {reply[:80]!r}", flush=True)
    return True


def process_operator_message_turn(text: str, rec: dict, source_tag: str = "OVERSEER LLM", upd_id: int = None) -> tuple:
    """
    Unified conversational pipeline for both text and transcribed voice notes:
    1. Calls `call_llm_for_chat_turn(text, rec)`.
    2. Applies silent structured state mutations (`apply_silent_llm_tool_updates`) including
       partial progress, completed habits, missed habits, non-negotiables, and XP/penalties.
    3. Appends to `chatHistory` and `auditLogs`, saves to `overseer_state.json`, and syncs to Supabase.
    """
    ov = rec.setdefault("state", {}).setdefault("overseer", {})
    cmd = text.lower().strip()
    if cmd in ("/reset", "/fresh", "/clearcache", "/clear_cache", "/clear", "/clean", "/wipe"):
        reset_to_fresh_slate(rec)
        reset_reply = (
            "SLATE WIPED CLEAN & CACHE CLEARED.\n\n"
            "- Cumulative XP & Rank: PRESERVED (Career progression stays intact)\n"
            "- Cash Vault & Streak: PRESERVED\n"
            "- Daily Targets: Unlocked for fresh entry\n"
            "- Daily Habit Checkboxes: Reset for the new day\n"
            "- Evasion Penalties & Ping Counters: Reset to 0\n"
            "- Vyvanse Tracking: Armed for morning window (cutoff 10:00 AM)\n\n"
            "You are starting with a clean daily board for tomorrow morning's 08:00 AM Reveille."
        )
        send_telegram_single_message(reset_reply, rec)
        return reset_reply, False

    if cmd in ("/bridge", "/terminal", "/website"):
        bridge_reply = (
            "COMMAND BRIDGE TERMINAL: https://cso-operator-terminal.vercel.app\n"
            "Open your terminal now to check off habits, log outreach numbers, and record your journal entries."
        )
        send_telegram_single_message(bridge_reply, rec)
        return bridge_reply, False

    if cmd == "/journal":
        j_reply = (
            "ALIGNMENT & DEBRIEF JOURNAL:\n"
            "Open your Command Bridge terminal at https://cso-operator-terminal.vercel.app to write your journal entry, "
            "or text it to me directly starting with 'Journal: [your entry]' to log it into your permanent state."
        )
        send_telegram_single_message(j_reply, rec)
        return j_reply, False

    if cmd == "/schedule":
        schedule_text = """OPERATOR REGIMEN:
08:00 - Reveille & Lock 3 Non-Negotiables (Vyvanse cutoff 10:00)
09:00 - Gym & Morning Physical Routine
10:00 - Vyvanse Cutoff & Gym Completion Check
11:30 - Track 1 B2A Outreach Sprint Launch (10-30 DMs)
13:30 - Midday Velocity & Outreach Count Audit
15:30 - Afternoon Momentum & Loom Check
17:30 - Agency Asset & Deliverables Sprint
20:00 - Evening Clearance Audit
21:30 - Night Debrief & Machine Score"""
        send_telegram_single_message(schedule_text, rec)
        return schedule_text, False

    if cmd == "/vyvanse":
        vy = ov.get("vyvanse") or {}
        cur_h = now_local().strftime("%H:%M")
        if vy.get("taken"):
            v_rep = f"Vyvanse confirmed TAKEN at {vy.get('takenTime')} (ON TIME). Window locked."
        elif cur_h >= "10:00":
            v_rep = f"Vyvanse cutoff EXPIRED at 10:00 AM (Current: {cur_h}). Do NOT take it late--push through unmedicated."
        else:
            v_rep = f"Vyvanse PENDING. Current time is {cur_h}. Hard cutoff is 10:00 AM--take it now."
        send_telegram_single_message(v_rep, rec)
        return v_rep, False

    if cmd in ("/boot", "/midday", "/velocity", "/debrief"):
        slot = "08:00" if cmd == "/boot" else ("13:30" if cmd == "/midday" else ("15:30" if cmd == "/velocity" else "21:30"))
        slot = "07:00" if cmd == "/boot" else ("13:00" if cmd == "/midday" else ("16:00" if cmd == "/velocity" else "21:00"))
        reply = call_llm_for_scheduled_reminder(slot, rec)
        llm_result = {"reply": reply}
    else:
        llm_result = call_llm_for_chat_turn(text, rec)

    reply = (llm_result.get("reply") or "").strip()
    if not reply:
        return "", False

    sent_ok = send_telegram_single_message(reply, rec)
    if not sent_ok:
        return "", False

    if upd_id is not None:
        global HIGHEST_UPDATE_ID
        HIGHEST_UPDATE_ID = max(HIGHEST_UPDATE_ID, upd_id)
        ov["lastTgUpdateId"] = HIGHEST_UPDATE_ID
        pids = ov.setdefault("processedUpdateIds", [])
        if upd_id not in pids:
            pids.append(upd_id)
        ov["processedUpdateIds"] = pids[-100:]

    ov["unansweredPingCount"] = 0
    ov["awaitingOperatorReply"] = False
    ov["pendingAuditSlot"] = None
    ov["lastOperatorReplyTimestamp"] = time.time()
    ov["lastPingTimestamp"] = time.time()
    actionable = apply_silent_llm_tool_updates(rec, llm_result)
    if not actionable and not is_today_targets_locked(ov, today_iso()):
        ov["awaitingOperatorReply"] = True

    history = ov.setdefault("chatHistory", [])
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    ov["chatHistory"] = history[-30:]
    ov.setdefault("auditLogs", []).insert(
        0,
        {
            "time": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "tag": source_tag,
            "message": f"OPERATOR: {text}\n{reply}",
        },
    )
    ov["auditLogs"] = ov["auditLogs"][:30]
    save_state_file(rec, broadcast=True)
    return reply, True


def poll_telegram_direct_once() -> None:
    global HIGHEST_UPDATE_ID, SERVER_HAS_DIRECT_INTERNET, LAST_DIRECT_TG_POLL_OK
    rec = load_state_file()
    ov = (rec.get("state") or {}).get("overseer") or {}
    tg_token = TELEGRAM_BOT_TOKEN or ov.get("tgToken") or ""
    if not tg_token:
        return

    offset = max(HIGHEST_UPDATE_ID, int(ov.get("lastTgUpdateId") or 0)) + 1
    url = f"https://api.telegram.org/bot{tg_token}/getUpdates?offset={offset}&timeout=2"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        SERVER_HAS_DIRECT_INTERNET = True
        LAST_DIRECT_TG_POLL_OK = time.time()

        if not data.get("ok") or not data.get("result"):
            return

        for upd in data["result"]:
            upd_id = int(upd.get("update_id") or 0)
            claim_key = f"tg_{upd_id}"
            if not claim_unique_key(claim_key, lease_seconds=25.0):
                continue

            msg = upd.get("message") or upd.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "")
            text = (msg.get("text") or msg.get("caption") or "").strip()
            voice_obj = msg.get("voice") or msg.get("audio") or msg.get("video_note")
            is_voice = False

            if not text and voice_obj and voice_obj.get("file_id"):
                is_voice = True
                text = transcribe_voice_note(tg_token, voice_obj["file_id"], ov.get("llmApiKey") or "")
                if not text:
                    text = "[Operator sent a voice note without clear audio transcript]"
            if not text:
                commit_unique_key(claim_key)
                HIGHEST_UPDATE_ID = max(HIGHEST_UPDATE_ID, upd_id)
                continue

            rec = load_state_file()
            ov = rec.setdefault("state", {}).setdefault("overseer", {})
            if chat_id:
                ov["tgChatId"] = chat_id

            tag = "OVERSEER VOICE LLM" if is_voice else "OVERSEER LLM"
            reply, ok = process_operator_message_turn(text, rec, source_tag=tag, upd_id=upd_id)
            if not ok:
                release_unique_key(claim_key)
                continue

            commit_unique_key(claim_key)
            print(f"[OVERSEER] Replied to {'VOICE' if is_voice else 'TEXT'} update {upd_id}: {text[:50]!r}", flush=True)
    except Exception:
        SERVER_HAS_DIRECT_INTERNET = False


def file_watcher_loop() -> None:
    last_mtime = 0.0
    while True:
        try:
            if os.path.exists(STATE_FILE):
                mtime = os.path.getmtime(STATE_FILE)
                if last_mtime != 0.0 and mtime > last_mtime:
                    rec = load_state_file()
                    broadcast_sse_state(rec)
                last_mtime = mtime
        except Exception:
            pass
        time.sleep(1.0)


def free_port_if_in_use(port: int) -> None:
    """Frees `127.0.0.1:port` if a previous server instance is still holding it."""
    my_pid = os.getpid()
    if os.name == "nt":
        try:
            out = subprocess.check_output(["netstat", "-ano"], text=True, timeout=3)
            for line in out.splitlines():
                if f"127.0.0.1:{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = int(parts[-1])
                    if pid != my_pid and pid > 0:
                        subprocess.run(["taskkill", "/F", "/PID", str(pid)], timeout=3, capture_output=True)
            time.sleep(0.3)
        except Exception:
            pass


def _run_git(args: list, cwd: str, timeout: int = 90) -> bool:
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


class ActionsStateSync:
    """
    Persists overseer_state.json to the `overseer-state` git branch (checked out at STATE_WT) so the
    GitHub Actions runner keeps memory across restarts, and the web dashboard can read the latest state.
    Commits carry [skip ci] so Vercel never redeploys on state changes.
    """

    def __init__(self, worktree: str):
        self.wt = worktree
        self.wt_file = os.path.join(worktree, "overseer_state.json")
        self.last_hash = None
        self.last_push = 0.0

    def _hash(self, path: str):
        try:
            import hashlib
            with open(path, "rb") as f:
                raw = json.loads(f.read().decode("utf-8"))
            raw.pop("updated_at", None)
            raw.pop("server_polls_telegram", None)
            return hashlib.sha1(json.dumps(raw, sort_keys=True).encode("utf-8")).hexdigest()
        except Exception:
            return None

    def pull(self) -> None:
        if os.path.exists(self.wt_file):
            try:
                import shutil
                shutil.copyfile(self.wt_file, STATE_FILE)
                shutil.copyfile(self.wt_file, LEGACY_STATE_FILE)
            except Exception:
                pass
        self.last_hash = self._hash(STATE_FILE)

    def push_if_changed(self, force: bool = False) -> bool:
        cur = self._hash(STATE_FILE)
        if cur is None or (cur == self.last_hash and not force):
            return False
        if not force and (time.time() - self.last_push) < 20.0:
            return False
        try:
            import shutil
            shutil.copyfile(STATE_FILE, self.wt_file)
        except Exception:
            return False
        _run_git(["add", "overseer_state.json"], self.wt)
        stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
        if not _run_git(["commit", "-m", f"overseer state {stamp} [skip ci]"], self.wt):
            self.last_hash = cur
            return False
        ok = _run_git(["push", "origin", "HEAD:overseer-state"], self.wt)
        if not ok:
            _run_git(["pull", "--rebase", "origin", "overseer-state"], self.wt)
            ok = _run_git(["push", "origin", "HEAD:overseer-state"], self.wt)
        if ok:
            self.last_hash = cur
            self.last_push = time.time()
        return ok


def run_actions_loop() -> None:
    """Always-on loop for GitHub Actions: Telegram long-poll, scheduled audits, midnight rollover, stalker."""
    wt = os.getenv("STATE_WT", "")
    run_seconds = int(os.getenv("OVERSEER_RUN_SECONDS", "21000"))
    sync = ActionsStateSync(wt) if wt and os.path.isdir(wt) else None
    if sync:
        sync.pull()
    load_state_file()
    deadline = time.time() + run_seconds
    print(
        f"[OVERSEER ACTIONS] online {now_local().strftime('%Y-%m-%d %H:%M:%S')} {TIMEZONE_NAME} "
        f"| persistence={'git' if sync else 'none'} | runs {run_seconds}s",
        flush=True,
    )
    while time.time() < deadline:
        try:
            check_midnight_rollover_cron()
            poll_telegram_direct_once()
            hm = now_local().strftime("%H:%M")
            if hm in SCHEDULE_LABELS:
                trigger_scheduled_llm_window(hm)
            execute_stalker_followup_ping(force=False)
            if sync:
                sync.push_if_changed()
        except Exception as e:
            print(f"[OVERSEER ACTIONS] loop error: {e!r}", flush=True)
        time.sleep(2.0)
    if sync:
        sync.push_if_changed(force=True)
    print("[OVERSEER ACTIONS] window complete; handing over to next run.", flush=True)


def start_self_keep_alive_thread() -> None:
    """
    Prevents Render Free tier from spinning down after 15 minutes of HTTP inactivity.
    Continuously pings the public `/health` endpoint every 5 minutes (300 seconds) in a background thread.
    """
    raw_url = (
        os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("RENDER_URL")
        or "https://overseer-backend-w9uu.onrender.com"
    ).rstrip("/")
    health_url = f"{raw_url}/health"

    def _loop():
        time.sleep(20)
        print(f"[OVERSEER KEEPALIVE] Self-ping watchdog active -> {health_url} (every 300s)", flush=True)
        while True:
            try:
                req = urllib.request.Request(
                    health_url,
                    headers={"User-Agent": "OVERSEER-Self-KeepAlive/2.2"},
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    _ = resp.read()
            except Exception:
                pass
            time.sleep(300)

    t = threading.Thread(target=_loop, daemon=True, name="OverseerKeepAlive")
    t.start()


def start_daemon() -> None:
    if "--actions-loop" in sys.argv:
        run_actions_loop()
        return
    if "--cron-sweep" in sys.argv or "--tick" in sys.argv:
        # Cloud / GitHub Actions tick: runs midnight rollover check, schedules, and stalker checks once
        rec = load_state_file()
        check_midnight_rollover_cron()
        hm = now_local().strftime("%H:%M")
        if hm in SCHEDULE_LABELS or "--force-schedule" in sys.argv:
            slot = sys.argv[sys.argv.index("--force-schedule") + 1] if "--force-schedule" in sys.argv else hm
            trigger_scheduled_llm_window(slot, force=True)
        execute_stalker_followup_ping(force=False)
        print(f"[OVERSEER CLOUD] Swept cron/stalker tick at {now_local().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        return
    if "--poll-once" in sys.argv:
        rec = load_state_file()
        check_midnight_rollover_cron()
        poll_telegram_direct_once()
        print("[OVERSEER CLOUD] Polled Telegram once.", flush=True)
        return
    if "--stalker-once" in sys.argv:
        load_state_file()
        execute_stalker_followup_ping(force=True)
        return
    if "--velocity-once" in sys.argv:
        load_state_file()
        trigger_scheduled_llm_window("13:00", force=True)
        return
    if "--rollover-once" in sys.argv:
        rec = load_state_file()
        perform_midnight_rollover(rec, today_iso())
        save_state_file(rec, broadcast=True)
        return

    free_port_if_in_use(PORT)
    load_state_file()
    server = ThreadingHTTPServer((HOST, PORT), OverseerRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=file_watcher_loop, daemon=True).start()
    start_self_keep_alive_thread()

    print(f"[OVERSEER] Pure LLM Bot, Velocity Scheduler & Stalker Engine active on http://{HOST}:{PORT}", flush=True)
    print(
        f"[OVERSEER] State File: {STATE_FILE} | Timezone: {TIMEZONE_NAME} | Schedules: 00:00, 07:00, 13:00, 16:00, 21:00 | Stalker: {STALKER_INTERVAL_SEC}s",
        flush=True,
    )

    while True:
        try:
            check_midnight_rollover_cron()
            poll_telegram_direct_once()
            hm = now_local().strftime("%H:%M")
            if hm in SCHEDULE_LABELS:
                trigger_scheduled_llm_window(hm)
            execute_stalker_followup_ping(force=False)
        except Exception:
            pass
        time.sleep(2.0)


if __name__ == "__main__":
    start_daemon()
