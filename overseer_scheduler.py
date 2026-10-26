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


def _resilient_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    except Exception:
        if host not in _DOH_CACHE:
            try:
                req = urllib.request.Request(
                    f"https://1.1.1.1/dns-query?name={host}&type=A",
                    headers={"Accept": "application/dns-json"},
                )
                with urllib.request.urlopen(req, timeout=4) as r:
                    ans = json.loads(r.read().decode("utf-8")).get("Answer") or []
                    ips = [a["data"] for a in ans if a.get("type") == 1 and a.get("data")]
                    if ips:
                        _DOH_CACHE[host] = ips[0]
            except Exception:
                pass
        resolved = _DOH_CACHE.get(host, host)
        return _ORIG_GETADDRINFO(resolved, port, family, type, proto, flags)


socket.getaddrinfo = _resilient_getaddrinfo

# =============================================================================
# 1. CONFIGURATION, PERSONA & COMMAND BRIDGE REGIMEN
# =============================================================================
TIMEZONE_NAME = os.getenv("OVERSEER_TIMEZONE", "America/Toronto")
try:
    LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo

PORT = int(os.getenv("OVERSEER_PORT", "8765"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "overseer_state.json")
LEGACY_STATE_FILE = os.path.join(BASE_DIR, "overseer_local_state.json")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wdjswfiiyywamzyhpnwl.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "sb_publishable_4m4AxCzhQ3KtEV52NmB0WA_Q-eUTSCj")
OPERATOR_PIN = os.getenv("OPERATOR_PIN", "OPERATOR-1")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8888328816:AAE5ipRKVSwKMShGTiTFCD9rP1Jkq5GtlZM")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5441024587")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:1234/v1/chat/completions")
POLLINATIONS_OPENAI_URL = "https://text.pollinations.ai/openai"

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

COMMAND_BRIDGE_REGIMEN_SPEC = (
    "COMMAND BRIDGE CONTEXT (Use for situational awareness; DO NOT dump the whole list at once — stay 2-4 sentences max): "
    "(1) 06:00-07:45 PHYSICAL: h_wake (06:00 wakeup + 1L electrolytes & morning hydration), h_workout (Gym/Hard Conditioning). "
    "(2) 07:15-08:00 MENTAL: h_journal (Alignment Journal + lock 3 Non-Negotiables), h_walk (20m silent Focus Walk). "
    "(3) 08:00-16:30 TRACK 1 CASH RUNWAY (50% upfront Stripe deposit, zero retainers): h_source (30 leads), h_agency_dm (30 Overflow DMs), "
    "h_warm_network (10 warm pings), h_loom (5 Loom audits), h_inbox (<5m reply), h_stripe (50% deposit link), h_deliver (2h sprint/20-DM surge). "
    "(4) 16:30-22:00 TRACK 2 AGENCY: h_track2_infra (agency infra/positioning), h_track2_asset (Case Study/VSL proof asset), h_sunset (21:00 Debrief). "
    "NON-NEGOTIABLES RULE: When `todayTargetsLocked` is false, demand the operator lock their 3 Non-Negotiables for today (1. Physical/Gym, 2. Cash/Outreach Quota, 3. Agency/Mental Target) and direct their immediate next move in 2-4 sharp sentences."
)

SCHEDULE_LABELS = {
    "07:00": "07:00 Morning Target Setting",
    "13:00": "13:00 Midday Velocity Check",
    "16:00": "16:00 Afternoon Velocity & Pace Check",
    "21:00": "21:00 Night Audit",
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
                "pendingAuditSlot": "07:00",
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
    ov["pendingAuditSlot"] = "07:00"
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
    ov.setdefault("pendingAuditSlot", None if locked_today else "07:00")

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
    scrubbed = re.sub(r"\s*and\s+taper\s+your\s+thc[^.!?]*", "", text, flags=re.IGNORECASE)
    scrubbed = re.sub(r"\b(weed|cannabis|marijuana|thc|vape|vaping|taper|tapering)\b", "", scrubbed, flags=re.IGNORECASE)
    scrubbed = re.sub(r"\s{2,}", " ", scrubbed).strip()
    return scrubbed


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
    '5/10 outreaches'), completed habits, and missed habits so state math is 100% accurate even if
    an upstream LLM returns plain text or omits a field.
    """
    text = (raw_text or "").strip()
    if not text:
        return {}
    lower = text.lower()
    out = {}

    # 1. Non-Negotiables extraction
    nn = {}
    lines = [re.sub(r"^[\s\-*•\d.)]+", "", ln).strip() for ln in text.splitlines() if ln.strip()]
    bullet_items = [ln for ln in lines if not ln.lower().startswith("my non")]

    if "non negotiable" in lower or "non-negotiable" in lower or "targets for today" in lower or len(bullet_items) >= 3:
        for item in bullet_items:
            il = item.lower()
            if any(w in il for w in ("gym", "lift", "workout", "run", "conditioning", "physical")):
                nn.setdefault("physical", item)
            elif any(w in il for w in ("journal", "walk", "meditat", "mental", "mindset")):
                nn.setdefault("mental", item)
            elif any(w in il for w in ("outreach", "dm", "lead", "loom", "call", "email", "cash", "stripe", "client")):
                nn.setdefault("cash", item)
            elif any(w in il for w in ("brand2audience", "agency", "asset", "vsl", "case study", "infra")):
                nn.setdefault("agency", item)
        if "cash" in nn and "agency" not in nn and ("brand2audience" in lower or "agency" in lower):
            nn["agency"] = nn["cash"]
        if len(nn) < 3 and len(bullet_items) >= 3:
            for key, item in zip(("physical", "mental", "cash", "agency"), bullet_items[:4]):
                nn.setdefault(key, item)
    if nn:
        out["non_negotiables"] = nn

    # 2. Partial ratio extraction (e.g. "15 out of 30 DMs", "15/30 DMs", "sent 15 DMs", "5/10 outreaches", "2 out of 5 looms")
    partial = {}
    completed = []
    missed = []

    # Check explicit X out of Y or X/Y patterns
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

    # Check single count patterns like "sent 15 DMs" or "did 15 outreaches" (when not locking targets)
    if not nn and not any(w in lower for w in ("want to", "going to", "will do", "my target", "non negotiable")):
        single_dm = re.search(r"(?:sent|did|completed|fired|knocked\s+out|done)\s+(\d+)\s*(?:overflow\s+)?(dms?|outreach(?:es)?)", lower)
        if single_dm and "h_agency_dm" not in partial:
            cnt = float(single_dm.group(1))
            # Check if user locked a specific outreach quota in nonNegotiables (e.g. 10 outreaches), else default 30
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
        if any(p in lower for p in ("skipped gym", "no gym", "didn't lift", "didnt lift", "missed gym", "skipped workout")):
            missed.append("h_workout")
        elif any(p in lower for p in ("hit the gym", "finished gym", "gym done", "lifted", "completed gym", "crushed gym", "did my gym")):
            completed.append("h_workout")

        if any(p in lower for p in ("skipped journal", "didn't journal", "didnt journal", "no journal", "missed journal")):
            missed.append("h_journal")
        elif any(p in lower for p in ("journaled", "finished journal", "journal done", "completed journal", "wrote in my journal", "did my journal")):
            completed.append("h_journal")

        if any(p in lower for p in ("drank 1l", "electrolytes done", "hydrated", "morning hydration done")):
            completed.append("h_wake")

        if any(p in lower for p in ("focus walk done", "did my walk", "20m walk", "20 min walk", "finished walk")):
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
    return out


def call_llm_for_chat_turn(raw_text: str, record: dict) -> dict:
    """
    Passes the operator's raw text (or transcribed voice note) + live state + last 15 messages
    of chatHistory directly to the LLM using `CHAT_SYSTEM_PROMPT`.
    Enforces a strict 2-4 sentence punchy drill-sergeant output and extracts both binary
    and partial habit completions.
    """
    overseer = ((record or {}).get("state") or {}).get("overseer") or {}
    llm_key = GEMINI_API_KEY or OPENAI_API_KEY or overseer.get("llmApiKey") or ""
    state_snapshot = build_live_state_snapshot(record)
    recent_memory = _get_recent_chat_memory(record, limit=CHAT_MEMORY_LIMIT)

    system_instruction = (
        f"{CHAT_SYSTEM_PROMPT}\n\n"
        f"{COMMAND_BRIDGE_REGIMEN_SPEC}\n\n"
        f"CURRENT `overseer_state.json` TELEMETRY:\n{state_snapshot}\n\n"
        "Respond with a valid JSON object matching this exact schema (no markdown fences):\n"
        "{\n"
        '  "reply": "Your single punchy, brutal, conversational response (STRICTLY 2 TO 4 SENTENCES MAX). '
        "Reference past excuses or wake-up times from chat history if relevant. "
        "If `todayTargetsLocked` is false, demand their 3 Non-Negotiables (Physical/Gym, Cash/Outreach, Agency/Mental) right now. "
        'Never output walls of text or schedule dumps.",\n'
        '  "non_negotiables": {"physical": "", "mental": "", "cash": "", "agency": ""},\n'
        '  "partial_progress": {"h_agency_dm": 0.5},\n'
        '  "completed_habits": [],\n'
        '  "missed_habits": [],\n'
        '  "cash_added": 0,\n'
        '  "journal_entry": ""\n'
        "}\n"
        "Rules for `partial_progress`: If the operator reports partial completion (e.g., '15 out of 30 DMs' = 0.5 on h_agency_dm, "
        "'2 of 5 Looms' = 0.4 on h_loom, 'half my gym workout' = 0.5 on h_workout), set the float ratio (0.0 to 1.0) in `partial_progress`.\n"
        "Valid habit IDs: h_workout, h_wake, h_journal, h_walk, h_source, h_agency_dm, h_warm_network, "
        "h_loom, h_inbox, h_stripe, h_deliver, h_track2_infra, h_track2_asset, h_sunset."
    )

    messages = [{"role": "system", "content": system_instruction}, *recent_memory, {"role": "user", "content": raw_text}]
    fallback_tools = _extract_tool_updates_from_text(raw_text, record)

    def _merge_fallback_tools(parsed_dict: dict) -> dict:
        for k in ("non_negotiables", "partial_progress", "completed_habits", "missed_habits"):
            if fallback_tools.get(k) and not parsed_dict.get(k):
                parsed_dict[k] = fallback_tools[k]
            elif k == "partial_progress" and isinstance(fallback_tools.get(k), dict) and isinstance(parsed_dict.get(k), dict):
                for hid, val in fallback_tools[k].items():
                    parsed_dict[k][hid] = max(float(parsed_dict[k].get(hid, 0.0)), float(val))
        return parsed_dict

    # Stage 1: Fast structured JSON extraction call (only if API key is provided or local server is up)
    if llm_key:
        raw_llm_output = _invoke_llm_providers(messages, llm_key, json_mode=True)
        if raw_llm_output:
            parsed = _extract_json_object(raw_llm_output)
            if parsed and isinstance(parsed.get("reply"), str) and parsed["reply"].strip():
                parsed["reply"] = _enforce_concise_drill_sergeant(parsed["reply"], raw_text, max_sentences=4)
                return _merge_fallback_tools(parsed)
            cleaned_raw = _enforce_concise_drill_sergeant(raw_llm_output, raw_text, max_sentences=4)
            if cleaned_raw and not cleaned_raw.startswith("{"):
                return _merge_fallback_tools({"reply": cleaned_raw})

        # Stage 2: Plain conversational turn with key
        plain_messages = [
            {
                "role": "system",
                "content": (
                    f"{CHAT_SYSTEM_PROMPT}\n\n{COMMAND_BRIDGE_REGIMEN_SPEC}\n\n"
                    f"LIVE TELEMETRY:\n{state_snapshot}\n\n"
                    "Reply directly to the operator in strictly 2 to 4 brutal sentences. "
                    "Call out any excuses from the conversation history and demand exact numbers and immediate target locks."
                ),
            },
            *recent_memory,
            {"role": "user", "content": raw_text},
        ]
        plain_reply = _invoke_llm_providers(plain_messages, llm_key, json_mode=False)
        cleaned_plain = _enforce_concise_drill_sergeant(plain_reply, raw_text, max_sentences=4)
        if cleaned_plain:
            return _merge_fallback_tools({"reply": cleaned_plain})

    # Stage 3: Instant Deterministic Drill-Sergeant Generator (<0.01s, 0 timeouts)
    fallback_reply = generate_drill_sergeant_fallback("chat", record, raw_text)
    return _merge_fallback_tools({"reply": fallback_reply})


def generate_drill_sergeant_fallback(context_type: str, record: dict, user_text: str = "") -> str:
    """
    Guaranteed fail-proof drill-sergeant generator. If external LLM gateways
    rate-limit, timeout, or require keys, OVERSEER will NEVER freeze or go silent.
    """
    ov = (record.get("state") or {}).get("overseer") or {}
    metrics = compute_holistic_machine_score(record)
    today_locked = ov.get("todayTargetsLocked", False)
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

    if "07:00" in context_type or "boot" in context_type.lower():
        return (
            "System boot initiated. The clock is running and your daily execution slate is at 0%. "
            "State your 3 Non-Negotiables across Physical, Cash/Outreach, and Agency right now—no pleasantries, no excuses."
        )
    if "13:00" in context_type or "midday" in context_type.lower():
        return (
            f"Midday velocity audit. You are sitting at {score}% Machine Score with {cash_sc}% cash progress. "
            "State your exact outreach numbers, gym status, and lead count right now."
        )
    if "16:00" in context_type or "afternoon" in context_type.lower():
        return (
            f"Afternoon velocity checkpoint. Current machine score is {score}% against a 70% requirement. "
            "Deliver your pulse report immediately: exact DM count, asset deployment status, and pipeline execution."
        )
    if "21:00" in context_type or "night" in context_type.lower() or "debrief" in context_type.lower():
        return (
            f"21:00 audit window. Machine score is finalized at {score}%. Report completed habits and "
            "justify any unmet targets before state rollover."
        )

    lower = user_text.lower().strip()
    if not today_locked:
        if any(w in lower for w in ("hello", "hey", "hi", "yo", "sup")):
            return (
                "Drop the pleasantries. Your targets are NOT locked for today and you are burning daylight at 0% execution. "
                "State your 3 Non-Negotiables across Physical, Cash, and Agency right now."
            )
        return (
            "I do not accept vague chatter. Your 3 Non-Negotiables for today are still unlocked. "
            "Lock them in right now: specify your Gym session, Outreach count, and Agency targets."
        )

    targets_str = ", ".join([v for v in nn.values() if v]) or "locked regimen"
    return (
        f"Acknowledged. You are locked in on: {targets_str}. Current machine score is {score}%. "
        "Stop talking, execute your reps, and report exact proof metrics once completed."
    )


def call_llm_for_scheduled_reminder(slot: str, record: dict) -> str:
    """
    Dynamically generates the 07:00, 13:00, 16:00, or 21:00 audit callout via the LLM API
    with full 15-message conversational memory and strict 2-4 sentence conciseness.
    For 13:00 and 16:00 Midday/Afternoon Velocity Checks, evaluates current `overseer_state.json`
    targets against expected pace and issues an immediate warning if lagging.
    """
    overseer = ((record or {}).get("state") or {}).get("overseer") or {}
    llm_key = GEMINI_API_KEY or OPENAI_API_KEY or overseer.get("llmApiKey") or ""
    slot_label = SCHEDULE_LABELS.get(slot, slot)
    metrics = compute_holistic_machine_score(record)
    state_snapshot = build_live_state_snapshot(record)
    recent_memory = _get_recent_chat_memory(record, limit=CHAT_MEMORY_LIMIT)

    if slot in ("13:00", "16:00"):
        expected_score = 45 if slot == "13:00" else 70
        lagging = metrics["machine_score"] < expected_score or not metrics["has_gym"] or metrics["cash_score"] < 50
        lag_status = (
            f"WARNING — OPERATOR IS LAGGING BEHIND PACE (Machine Score: {metrics['machine_score']}% vs {expected_score}% expected by {slot}; "
            f"Physical: {metrics['physical_score']}%, Cash: {metrics['cash_score']}%, Agency: {metrics['agency_score']}%)."
            if lagging
            else f"ON PACE (Machine Score: {metrics['machine_score']}%)."
        )
        prompt = (
            f"SPONTANEOUS MID-DAY VELOCITY CHECK [{slot_label}]. {lag_status} "
            f"Review today's locked targets and progress in `overseer_state.json`: {state_snapshot}. "
            "Fire a punchy, high-aggression progress check (STRICTLY 2 TO 4 SENTENCES MAX) auditing their exact DM/outreach count, "
            "gym status, journaling, and asset shipping progress against the targets they locked this morning. "
            "If they are lagging behind pace, issue an immediate warning and demand a mid-day pulse report right now."
        )
    elif slot == "07:00":
        prompt = (
            f"MORNING SYSTEM BOOT [{slot_label}]. `todayTargetsLocked` is {overseer.get('todayTargetsLocked')}. "
            f"Live state: {state_snapshot}. "
            "Send a sharp, brutal 2-to-4 sentence morning wake-up call demanding the operator lock their 3 fresh Non-Negotiables "
            "for today (1. Physical/Gym, 2. Mental/Journal, 3. Cash/Agency Outreach) immediately."
        )
    else:
        prompt = (
            f"NIGHT DEBRIEF AUDIT [{slot_label}]. Review today's locked morning targets and current execution in `overseer_state.json`: {state_snapshot}. "
            "Send a brutal 2-to-4 sentence end-of-day interrogation explicitly referencing the morning targets they set and demanding final confirmation "
            "on Gym, Journal, Cash outreach count, and Agency progress."
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

    # 2. Gemini API if configured
    if llm_key.startswith("AIza"):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={llm_key}"
            convo = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
            body = {"contents": [{"parts": [{"text": convo}]}]}
            if json_mode:
                body["generationConfig"] = {"responseMimeType": "application/json"}
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=14) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            pass

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
        if targets_locked and not pending_audit:
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
    if cmd in ("/boot", "/midday", "/velocity", "/debrief"):
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


def start_daemon() -> None:
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
    server = ThreadingHTTPServer(("127.0.0.1", PORT), OverseerRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=file_watcher_loop, daemon=True).start()

    print(f"[OVERSEER] Pure LLM Bot, Velocity Scheduler & Stalker Engine active on http://127.0.0.1:{PORT}", flush=True)
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
