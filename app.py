import os
import json
import logging
import threading
from datetime import datetime, timezone
import requests
from flask import Flask
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("resumeai-bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REQUIRED_ENV_VARS = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GEMINI_API_KEY",
                     "TRELLO_API_KEY", "TRELLO_TOKEN", "TRELLO_LIST_ID"]
missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if missing:
    raise RuntimeError(f"Faltando: {', '.join(missing)}")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TRELLO_API_KEY = os.environ["TRELLO_API_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TRELLO_LIST_ID = os.environ["TRELLO_LIST_ID"]
TRELLO_MEMBER_ID = os.environ.get("TRELLO_MEMBER_ID")  # "6a618e84843f10fff9bb7e25"

app_slack = App(token=SLACK_BOT_TOKEN)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
app_flask = Flask(__name__)

_BOT_USER_ID = None
_PROCESSED_EVENTS = set()
_PROCESSED_LOCK = threading.Lock()

def get_bot_user_id(client):
    global _BOT_USER_ID
    if _BOT_USER_ID is None:
        _BOT_USER_ID = client.auth_test()["user_id"]
    return _BOT_USER_ID

# ---------------------------------------------------------------------------
@app_flask.route("/")
def health_check():
    return "✅ ResumeAI Bot está online!", 200

# ---------------------------------------------------------------------------
def analyze_with_gemini(text_content: str) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"""Analise o texto e extraia a tarefa. Data hoje: {today}
Texto: """ + text_content + """
Responda apenas JSON:
{"title": "...", "description": "...", "due_date": "YYYY-MM-DD ou null", "priority": "alta|média|baixa", "labels": ["urgente", "dev", ...]}"""
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    raw = (response.text or "").strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# ---------------------------------------------------------------------------
def create_trello_card(task_data: dict, slack_link: str = None):
    url = "https://api.trello.com/1/cards"
    desc = task_data["description"]
    if slack_link:
        desc += f"\n\n🔗 {slack_link}"

    query = {
        "key": TRELLO_API_KEY,
        "token": TRELLO_TOKEN,
        "idList": TRELLO_LIST_ID,
        "name": task_data["title"][:60],
        "desc": desc,
    }
    if task_data.get("due_date") and str(task_data.get("due_date")).lower() != "null":
        query["due"] = task_data["due_date"]
    if TRELLO_MEMBER_ID:
        query["idMembers"] = TRELLO_MEMBER_ID

    r = requests.post(url, params=query, timeout=10)
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------------------
def list_recent_tasks():
    url = f"https://api.trello.com/1/lists/{TRELLO_LIST_ID}/cards"
    params = {"key": TRELLO_API_KEY, "token": TRELLO_TOKEN, "limit": 10}
    r = requests.get(url, params=params, timeout=10)
    if r.ok:
        cards = r.json()
        if not cards:
            return "Nenhuma tarefa pendente."
        msg = "📋 *Últimas tarefas:*\n"
        for c in cards[:10]:
            due = f" | 📅 {c.get('due')[:10]}" if c.get('due') else ""
            msg += f"• <{c['url']}|{c['name']}>{due}\n"
        return msg
    return "Erro ao buscar tarefas."

# ---------------------------------------------------------------------------
def process_task_request(...):  # (mesma função anterior, mantida)
    # ... (copie a função completa da resposta anterior se precisar)
    pass  # substitua pela função completa da mensagem anterior

# ---------------------------------------------------------------------------
# Slash Command /tarefas
# ---------------------------------------------------------------------------
@app_slack.command("/tarefas")
def handle_tarefas(ack, say):
    ack()
    say(list_recent_tasks())

# ---------------------------------------------------------------------------
# Handlers (app_mention + DM) — mantenha como na versão anterior
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    threading.Thread(target=lambda: SocketModeHandler(app_slack, SLACK_APP_TOKEN).start(), daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)
