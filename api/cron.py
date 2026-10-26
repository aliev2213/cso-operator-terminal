import os
import json
import sys
from http.server import BaseHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import overseer_bot as bot

class handler(BaseHTTPRequestHandler):
    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        self.run_cron_sweep()

    def do_POST(self):
        self.run_cron_sweep()

    def run_cron_sweep(self):
        """
        Executes midnight rollover, scheduled alerts (07:00, 13:00, 16:00, 21:00),
        and the Stalker persistence engine.
        Can be triggered by Vercel Cron or an external ping (cron-job.org / GitHub Actions).
        """
        try:
            rec = bot.load_state_file()
            bot.check_midnight_rollover_cron()
            hm = bot.now_local().strftime("%H:%M")
            if hm in bot.SCHEDULE_LABELS:
                bot.trigger_scheduled_llm_window(hm, force=False)
            bot.execute_stalker_followup_ping(force=False)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors()
            self.end_headers()
            res = {
                "ok": True,
                "time": bot.now_local().strftime("%Y-%m-%d %H:%M:%S"),
                "todayTargetsLocked": (rec.get("state") or {}).get("overseer", {}).get("todayTargetsLocked", False)
            }
            self.wfile.write(json.dumps(res).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self._send_cors()
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
