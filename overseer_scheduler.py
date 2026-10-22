#!/usr/bin/env python3
"""
OVERSEER Bot & Real-Time Command Bridge Server (`overseer_bot.py`)
==================================================================
100% Pure LLM Architecture + Active Persistence & Anti-Evasion Protocol:
1. RELENTLESS FOLLOW-UP LOOP (THE STALKER ENGINE):
   - Every 30 minutes (`STALKER_INTERVAL_SEC`, default 1800s), checks if `todayTargetsLocked`
     is False OR an active daily audit (`pendingAuditSlot` / `awaitingOperatorReply`) is pending
     without an operator target-lock or execution report.
   - Dynamically generates a short, high-aggression LLM callout on Telegram calling out the
     operator's silence, referencing their earlier excuses/messages from `chatHistory`.
2. CONCISE, HIGH-INTENSITY DRILL SERGEANT PERSONA:
   - Strictly punchy, brutal, and conversational (2–4 sentences max per message).
   - No huge walls of text or giant schedule dumps unless explicitly asked.
3. UNANSWERED PINGS & AUTOMATIC XP PENALTIES:
   - Tracks `unansweredPingCount` across persistent pings.
   - If the operator ignores 3 consecutive persistent pings without locking in their
     non-negotiables or reporting execution, applies a dynamic `-100 XP` (`EVASION PENALTY`)
     to `overseer_state.json` and Supabase, and notifies the operator on Telegram that their
     score is bleeding while they stall.
4. FULL CHAT CONVERSATIONAL MEMORY:
   - Passes the last 15 messages of `chatHistory` into the LLM context array on every API call
     (interactive chat turns, scheduled 07:00/13:00/21:00 audits, and Stalker Engine pings).
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
    return non_empty_count >= 3


# =============================================================================
# 2. UNIFIED LOCAL DATABASE (`overseer_state.json`), SUPABASE & SSE SYNC
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
                "nonNegotiables": {"physical": "", "mental": "", "cash": "", "agency": ""},
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
        "bio_state": {"taperTargetMg": 15.0, "logs": []},
        "updated_at": now_local().isoformat(),
    }


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
    ov.setdefault("journalLogs", [])
    ov.setdefault("auditLogs", [])
    ov.setdefault("chatHistory", [])
    ov.setdefault("processedUpdateIds", [])
    ov.setdefault("enforcedPenaltyXP", 0)
    ov.setdefault("evasionPenaltyCountToday", 0)
    ov.setdefault("unansweredPingCount", 0)
    ov.setdefault("lastMachineScore", 0)

    # Reset daily lock & counters if date rolled over
    if st.get("date") != today_str:
        st["date"] = today_str
        ov["todayTargetsLocked"] = False
        ov["unansweredPingCount"] = 0
        ov["evasionPenaltyCountToday"] = 0
        ov["pendingAuditSlot"] = "07:00"
        ov["awaitingOperatorReply"] = True

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
    data.setdefault("bio_state", {"taperTargetMg": 15.0, "logs": []})
    return data


def compute_holistic_machine_score(record: dict) -> dict:
    state = (record or {}).get("state") or {}
    completed = set(state.get("completedHabits") or [])

    has_gym = "h_workout" in completed
    has_hydration = "h_wake" in completed
    physical_score = (50 if has_gym else 0) + (50 if has_hydration else 0)

    has_journal = "h_journal" in completed
    has_walk = "h_walk" in completed
    mental_score = (50 if has_journal else 0) + (50 if has_walk else 0)

    outreach_ids = {"h_source", "h_agency_dm", "h_warm_network", "h_loom"}
    closing_ids = {"h_inbox", "h_stripe", "h_deliver"}
    outreach_done = len(completed.intersection(outreach_ids))
    closing_done = len(completed.intersection(closing_ids))
    cash_score = min(
        100,
        round(
            (50 if outreach_done > 0 else 0)
            + (50 if closing_done > 0 else (50 if outreach_done >= 3 else (outreach_done / 4) * 50))
        ),
    )

    has_infra = "h_track2_infra" in completed
    has_asset = "h_track2_asset" in completed
    has_sunset = "h_sunset" in completed
    agency_score = min(
        100, (50 if has_infra else 0) + (50 if has_asset else (25 if has_sunset else 0))
    )

    machine_score = round(
        (physical_score * 0.25)
        + (mental_score * 0.25)
        + (cash_score * 0.25)
        + (agency_score * 0.25)
    )

    domains_cleared = sum(
        [
            physical_score == 100,
            mental_score == 100,
            cash_score >= 100 or (outreach_done >= 2 and closing_done >= 1),
            has_infra and has_asset,
        ]
    )

    penalty = 0
    if not has_gym:
        penalty += 150
    if not has_hydration:
        penalty += 100
    if not has_journal:
        penalty += 150
    if not has_walk:
        penalty += 100

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
        "penalty_at_risk": penalty,
        "xp": xp,
        "rank_level": rank["level"],
        "rank_title": rank["title"],
    }


def load_state_file() -> dict:
    with STATE_LOCK:
        for path in (STATE_FILE, LEGACY_STATE_FILE):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return ensure_record_structure(json.load(f))
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
# 3. SILENT STRUCTURED STATE MUTATOR & EVASION PENALTY ENGINE
# =============================================================================
def apply_silent_llm_tool_updates(record: dict, tool_args: dict) -> bool:
    """
    Silently applies the LLM's structured tool call (`non_negotiables`,
    `completed_habits`, `missed_habits`, `cash_added`, `journal_entry`)
    to `overseer_state.json` and Supabase.
    Returns True if the operator locked targets or reported concrete execution/missed habits.
    """
    if not isinstance(tool_args, dict):
        return False

    state = record.setdefault("state", {})
    overseer = state.setdefault("overseer", {})
    today_str = today_iso()
    state["date"] = today_str

    completed = list(state.get("completedHabits") or [])
    xp = int(state.get("xp") or 0)
    penalty_xp = int(overseer.get("enforcedPenaltyXP") or 0)
    actionable_update = False

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
        # If the user gave 3 non-negotiables (e.g. physical, cash, agency) and mental was omitted,
        # auto-fill mental with "Alignment Journal + 20m Silent Focus Walk" if at least 3 were given
        filled_now = [k for k in ("physical", "mental", "cash", "agency") if str(cur_nn.get(k) or "").strip()]
        if len(filled_now) >= 3:
            if not str(cur_nn.get("mental") or "").strip():
                cur_nn["mental"] = "Alignment Journal + 20m Focus Walk"
            if overseer.get("bootLockedDate") != today_str:
                xp += 50
                overseer["bootLockedDate"] = today_str
            overseer["todayTargetsLocked"] = True
            actionable_update = True
        elif updated_keys:
            actionable_update = True

    # 2. Completed Habits
    for hid in tool_args.get("completed_habits") or []:
        if hid in HABIT_XP:
            actionable_update = True
            if hid not in completed:
                completed.append(hid)
                xp += HABIT_XP[hid]

    # 3. Missed / Skipped Habits (Enforce Discipline Penalty)
    for hid in tool_args.get("missed_habits") or []:
        if hid in HABIT_PENALTIES or hid in HABIT_XP:
            actionable_update = True
            if hid in completed:
                completed.remove(hid)
                xp = max(0, xp - HABIT_XP.get(hid, 100))
            pen = HABIT_PENALTIES.get(hid, 100)
            xp = max(0, xp - pen)
            penalty_xp += pen

    # 4. Cash Added
    try:
        cash_added = float(tool_args.get("cash_added") or 0)
    except Exception:
        cash_added = 0.0
    if cash_added > 0:
        actionable_update = True
        state["cash"] = int(state.get("cash") or 0) + int(cash_added)
        xp += int(cash_added // 2)

    # 5. Journal Entry
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
        if "h_journal" not in completed:
            completed.append("h_journal")
            xp += HABIT_XP["h_journal"]

    state["completedHabits"] = completed
    state["xp"] = xp
    state["lastActiveDate"] = today_str
    overseer["enforcedPenaltyXP"] = penalty_xp
    overseer["todayTargetsLocked"] = is_today_targets_locked(overseer, today_str)

    # Reset stalker unanswered counter when the operator reports execution or locks targets
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
# 4. PURE LLM ENGINE (CONCISE 2-4 SENTENCE INTERROGATION & STALKER ENGINE)
# =============================================================================
def build_live_state_snapshot(record: dict) -> str:
    state = record.get("state") or {}
    overseer = state.get("overseer") or {}
    metrics = compute_holistic_machine_score(record)
    nn = overseer.get("nonNegotiables") or {}
    today_str = today_iso()
    targets_locked_today = is_today_targets_locked(overseer, today_str)
    completed = list(state.get("completedHabits") or [])
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
            "todayTargetsLocked": targets_locked_today,
            "non_negotiables": nn,
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
    # Flatten multi-paragraph walls into a single tight paragraph
    cleaned = re.sub(r"\s*\n+\s*", " ", cleaned).strip()
    lower_req = (user_text or "").lower()
    if any(kw in lower_req for kw in ("full schedule", "entire schedule", "full regimen", "list all")):
        return cleaned
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    if len(sentences) > max_sentences:
        cleaned = " ".join(sentences[:max_sentences]).strip()
    return cleaned


def _extract_tool_updates_from_text(raw_text: str) -> dict:
    """
    Ensures silent state extraction succeeds even if an upstream LLM provider
    returned plain conversational text instead of a JSON wrapper.
    """
    text = (raw_text or "").strip()
    if not text:
        return {}
    lower = text.lower()
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

    return {"non_negotiables": nn} if nn else {}


def call_llm_for_chat_turn(raw_text: str, record: dict) -> dict:
    """
    Passes the operator's raw text + live state + last 15 messages of chatHistory
    directly to the LLM using `CHAT_SYSTEM_PROMPT`.
    Enforces a strict 2-4 sentence punchy drill-sergeant output.
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
        '  "completed_habits": [],\n'
        '  "missed_habits": [],\n'
        '  "cash_added": 0,\n'
        '  "journal_entry": ""\n'
        "}\n"
        "Valid habit IDs: h_workout, h_wake, h_journal, h_walk, h_source, h_agency_dm, h_warm_network, "
        "h_loom, h_inbox, h_stripe, h_deliver, h_track2_infra, h_track2_asset, h_sunset."
    )

    messages = [{"role": "system", "content": system_instruction}, *recent_memory, {"role": "user", "content": raw_text}]
    fallback_tools = _extract_tool_updates_from_text(raw_text)

    # Stage 1: Structured JSON extraction call
    raw_llm_output = _invoke_llm_providers(messages, llm_key, json_mode=True)
    if raw_llm_output:
        parsed = _extract_json_object(raw_llm_output)
        if parsed and isinstance(parsed.get("reply"), str) and parsed["reply"].strip():
            parsed["reply"] = _enforce_concise_drill_sergeant(parsed["reply"], raw_text, max_sentences=4)
            if not parsed.get("non_negotiables") and fallback_tools.get("non_negotiables"):
                parsed["non_negotiables"] = fallback_tools["non_negotiables"]
            return parsed
        cleaned_raw = _enforce_concise_drill_sergeant(raw_llm_output, raw_text, max_sentences=4)
        if cleaned_raw and not cleaned_raw.startswith("{"):
            return {"reply": cleaned_raw, **fallback_tools}

    # Stage 2: Plain conversational fallback if JSON mode failed or timed out
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
        return {"reply": cleaned_plain, **fallback_tools}

    return {"reply": ""}


def call_llm_for_scheduled_reminder(slot: str, record: dict) -> str:
    """
    Dynamically generates the 07:00, 13:00, or 21:00 audit callout via the LLM API
    with full 15-message conversational memory and strict 2-4 sentence conciseness.
    """
    overseer = ((record or {}).get("state") or {}).get("overseer") or {}
    llm_key = GEMINI_API_KEY or OPENAI_API_KEY or overseer.get("llmApiKey") or ""
    slot_label = SCHEDULE_LABELS.get(slot, slot)
    state_snapshot = build_live_state_snapshot(record)
    recent_memory = _get_recent_chat_memory(record, limit=CHAT_MEMORY_LIMIT)

    prompt = (
        f"It is time for the [{slot_label}]. "
        f"Review today's state in `overseer_state.json`: {state_snapshot}. "
        "Send the operator a punchy, brutal, conversational message (STRICTLY 2 TO 4 SENTENCES MAX) "
        "demanding their 3 Non-Negotiables if `todayTargetsLocked` is false, or auditing their exact numbers across "
        "Gym, Journal, Cash Outreach, and Agency. Reference their earlier chat history if they made excuses or woke up late."
    )
    messages = [
        {"role": "system", "content": f"{CHAT_SYSTEM_PROMPT}\n\n{COMMAND_BRIDGE_REGIMEN_SPEC}"},
        *recent_memory,
        {"role": "user", "content": prompt},
    ]
    reply = _invoke_llm_providers(messages, llm_key, json_mode=False)
    return _enforce_concise_drill_sergeant(reply, max_sentences=4)


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
            f"`todayTargetsLocked` is false or an active audit is still unanswered. Live state: {state_snapshot}. "
            "Generate a short, high-aggression callout (STRICTLY 2 TO 4 SENTENCES MAX) calling out the operator's silence—"
            "accuse them of dodging accountability, hiding from their targets, or rationalizing weakness after their earlier messages. "
            f"Warn them they are at {unanswered_count}/{EVASION_PING_THRESHOLD} unanswered pings before an automatic -{EVASION_PENALTY_XP} XP EVASION PENALTY hits, "
            "and demand they lock their 3 Non-Negotiables immediately."
        )

    messages = [
        {"role": "system", "content": f"{CHAT_SYSTEM_PROMPT}\n\n{COMMAND_BRIDGE_REGIMEN_SPEC}"},
        *recent_memory,
        {"role": "user", "content": directive},
    ]
    reply = _invoke_llm_providers(messages, llm_key, json_mode=False)
    return _enforce_concise_drill_sergeant(reply, max_sentences=4)


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

    # 4. Cloud OpenAI-Compatible Proxy (`https://text.pollinations.ai/openai`) with multi-model retry
    for model_name in ("openai", "openai-fast", "mistral"):
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
                headers={"Content-Type": "application/json", "User-Agent": "OVERSEER/5.0"},
            )
            with urllib.request.urlopen(req, timeout=14.0) as resp:
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
            time.sleep(0.3)

    # 5. Pollinations GET Endpoint Fallback
    try:
        sys_text = next((m.get("content", "") for m in messages if m.get("role") == "system"), CHAT_SYSTEM_PROMPT)[:900]
        usr_text = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")[:600]
        seed = int(time.time() * 1000) % 100000
        get_url = (
            f"https://text.pollinations.ai/{urllib.parse.quote(usr_text)}"
            f"?model=openai&seed={seed}&system={urllib.parse.quote(sys_text)}"
        )
        req = urllib.request.Request(get_url, headers={"User-Agent": "OVERSEER/5.0"})
        with urllib.request.urlopen(req, timeout=14.0) as resp:
            txt = resp.read().decode("utf-8").strip()
            if txt:
                return txt
    except Exception:
        pass

    return ""


def transcribe_voice_note(tg_token: str, file_id: str, llm_key: str = "") -> str:
    """Transcribes Telegram voice note via OpenAI Whisper or Gemini multimodal."""
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

        key = OPENAI_API_KEY or GEMINI_API_KEY or llm_key
        if key.startswith("sk-"):
            boundary = "----OverseerWhisperBoundary"
            body = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n'
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="voice.ogg"\r\n'
                "Content-Type: audio/ogg\r\n\r\n"
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
                return (w_data.get("text") or "").strip()

        if key.startswith("AIza"):
            b64_audio = base64.b64encode(audio_bytes).decode("ascii")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
            payload = json.dumps(
                {
                    "contents": [
                        {
                            "parts": [
                                {"text": "Transcribe this audio message verbatim."},
                                {"inlineData": {"mimeType": "audio/ogg", "data": b64_audio}},
                            ]
                        }
                    ]
                }
            ).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as g_resp:
                g_data = json.loads(g_resp.read().decode("utf-8"))
                return g_data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        pass
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

        if parsed.path == "/api/llm_turn_commit":
            rec = load_state_file()
            user_text = (payload.get("user_text") or "").strip()
            reply_text = (payload.get("reply") or "").strip()
            tool_args = payload.get("tool_args") or payload.get("tool_data") or {}
            chat_id = payload.get("chat_id")
            update_id = payload.get("update_id")

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
# 6. DIRECT TELEGRAM POLLING, SCHEDULED AUDITS & STALKER ENGINE LOOP
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


def trigger_scheduled_llm_window(slot: str) -> None:
    if not SERVER_HAS_DIRECT_INTERNET:
        return
    claim_key = f"sched_{today_iso()}_{slot}"
    if not claim_unique_key(claim_key, lease_seconds=60.0):
        return
    rec = load_state_file()
    reply = call_llm_for_scheduled_reminder(slot, rec)
    if not reply:
        release_unique_key(claim_key)
        return
    if not send_telegram_single_message(reply, rec):
        release_unique_key(claim_key)
        return
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
        # Do not stalk during sleeping hours (23:00 - 06:59) unless forced
        cur_hour = now_local().hour
        if cur_hour < 7 or cur_hour >= 23:
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
            voice = msg.get("voice") or msg.get("audio")
            if not text and voice and voice.get("file_id"):
                text = transcribe_voice_note(tg_token, voice["file_id"], ov.get("llmApiKey") or "")
                if not text:
                    text = "[Operator sent a voice note]"
            if not text:
                commit_unique_key(claim_key)
                HIGHEST_UPDATE_ID = max(HIGHEST_UPDATE_ID, upd_id)
                continue

            rec = load_state_file()
            ov = rec.setdefault("state", {}).setdefault("overseer", {})
            if chat_id:
                ov["tgChatId"] = chat_id

            cmd = text.lower()
            if cmd in ("/boot", "/midday", "/debrief"):
                slot = "07:00" if cmd == "/boot" else ("13:00" if cmd == "/midday" else "21:00")
                reply = call_llm_for_scheduled_reminder(slot, rec)
                llm_result = {"reply": reply}
            else:
                llm_result = call_llm_for_chat_turn(text, rec)

            reply = (llm_result.get("reply") or "").strip()
            if not reply:
                release_unique_key(claim_key)
                continue

            sent_ok = send_telegram_single_message(reply, rec)
            if not sent_ok:
                release_unique_key(claim_key)
                continue

            commit_unique_key(claim_key)
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
                # Operator replied with an excuse or pleasantry without locking targets; keep Stalker active
                ov["awaitingOperatorReply"] = True

            history = ov.setdefault("chatHistory", [])
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            ov["chatHistory"] = history[-30:]
            ov.setdefault("auditLogs", []).insert(
                0,
                {
                    "time": now_local().strftime("%Y-%m-%d %H:%M:%S"),
                    "tag": "OVERSEER LLM",
                    "message": f"OPERATOR: {text}\n{reply}",
                },
            )
            ov["auditLogs"] = ov["auditLogs"][:30]
            save_state_file(rec, broadcast=True)
            print(f"[OVERSEER] Replied to update {upd_id}: {text[:50]!r}", flush=True)
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
    if "--stalker-once" in sys.argv:
        load_state_file()
        execute_stalker_followup_ping(force=True)
        return

    free_port_if_in_use(PORT)
    load_state_file()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), OverseerRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=file_watcher_loop, daemon=True).start()

    print(f"[OVERSEER] Pure LLM Bot & Stalker Engine active on http://127.0.0.1:{PORT}", flush=True)
    print(
        f"[OVERSEER] State File: {STATE_FILE} | Timezone: {TIMEZONE_NAME} | Stalker Interval: {STALKER_INTERVAL_SEC}s",
        flush=True,
    )

    while True:
        try:
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
