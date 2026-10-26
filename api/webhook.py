import os
import json
import sys
from http.server import BaseHTTPRequestHandler

# Add root directory to sys.path so we can import overseer_bot directly
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
        # Health check and basic diagnostics
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._send_cors()
        self.end_headers()
        res = {
            "status": "online",
            "mode": "serverless_webhook",
            "bot_active": True,
            "timezone": bot.TIMEZONE_NAME,
            "server_time": bot.now_local().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.wfile.write(json.dumps(res).encode("utf-8"))

    def do_POST(self):
        """
        Receives incoming Telegram updates directly via Webhook:
        1. Reads update payload.
        2. Passes through process_operator_message_turn.
        3. Updates state in memory and replies immediately to Telegram.
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length).decode("utf-8")
            update = json.loads(raw_body) if raw_body else {}

            upd_id = int(update.get("update_id") or 0)
            msg = update.get("message") or update.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "")
            text = (msg.get("text") or msg.get("caption") or "").strip()
            voice_obj = msg.get("voice") or msg.get("audio") or msg.get("video_note")

            rec = bot.load_state_file()
            ov = rec.setdefault("state", {}).setdefault("overseer", {})
            if chat_id:
                ov["tgChatId"] = chat_id

            is_voice = False
            if not text and voice_obj and voice_obj.get("file_id"):
                is_voice = True
                text = bot.transcribe_voice_note(
                    ov.get("tgToken") or bot.TELEGRAM_BOT_TOKEN,
                    voice_obj["file_id"],
                    ov.get("llmApiKey") or ""
                )
                if not text:
                    text = "[Operator sent a voice note without clear audio transcript]"

            if text:
                source_tag = "OVERSEER VOICE LLM" if is_voice else "OVERSEER LLM"
                reply, ok = bot.process_operator_message_turn(text, rec, source_tag=source_tag, upd_id=upd_id)
                bot.save_state_file(rec, broadcast=False)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors()
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
        except Exception as e:
            self.send_response(200) # Always return 200 to Telegram so it doesn't repeatedly retry
            self.send_header("Content-Type", "application/json")
            self._send_cors()
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
