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
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("resumeai-bot")

# ---------------------------------------------------------------------------
# Configuração das Variáveis de Ambiente
# ---------------------------------------------------------------------------
REQUIRED_ENV_VARS = [
    "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GEMINI_API_KEY",
    "TRELLO_API_KEY", "TRELLO_TOKEN", "TRELLO_LIST_ID"
]
missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if missing:
    raise RuntimeError(f"Variáveis faltando: {', '.join(missing)}")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TRELLO_API_KEY = os.environ["TRELLO_API_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TRELLO_LIST_ID = os.environ["TRELLO_LIST_ID"]
TRELLO_MEMBER_ID = os.environ.get("TRELLO_MEMBER_ID")  # Seu ID: 6a618e84843f10fff9bb7e25

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
# Health Check (Render / UptimeRobot)
# ---------------------------------------------------------------------------
@app_flask.route("/")
def health_check():
    return "✅ ResumeAI Bot está online!", 200

# ---------------------------------------------------------------------------
# Gemini: Análise aprimorada
# ---------------------------------------------------------------------------
def analyze_with_gemini(text_content: str) -> dict:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"""
Você é um assistente de produtividade. Analise o texto abaixo e extraia a tarefa principal.

Data de hoje: {today_str}
Texto:
\"\"\"{text_content}\"\"\"

Responda APENAS com JSON válido:
{{
    "title": "Título curto (máx 60 caracteres)",
    "description": "Descrição clara + contexto",
    "due_date": "YYYY-MM-DD ou null",
    "priority": "alta" | "média" | "baixa",
    "labels": ["urgente", "dev", "design", "reunião", ...]
}}
"""
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        raw = (response.text or "").strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        return data
    except Exception as e:
        logger.error(f"Erro no Gemini: {e}")
        raise

# ---------------------------------------------------------------------------
# Trello: Criar card
# ---------------------------------------------------------------------------
def create_trello_card(task_data: dict, slack_link: str = None) -> dict:
    url = "https://api.trello.com/1/cards"
    full_desc = task_data["description"]
    if slack_link:
        full_desc += f"\n\n🔗 Link Slack: {slack_link}"

    query = {
        "key": TRELLO_API_KEY,
        "token": TRELLO_TOKEN,
        "idList": TRELLO_LIST_ID,
        "name": task_data["title"][:60],
        "desc": full_desc,
    }

    if task_data.get("due_date") and str(task_data["due_date"]).lower() != "null":
        query["due"] = task_data["due_date"]

    if TRELLO_MEMBER_ID:
        query["idMembers"] = TRELLO_MEMBER_ID

    # Labels (exemplo)
    labels = task_data.get("labels", [])
    if labels:
        # Você pode mapear labels do Trello aqui depois
        pass

    resp = requests.post(url, params=query, timeout=10)
    resp.raise_for_status()
    return resp.json()

# ---------------------------------------------------------------------------
# Processamento principal
# ---------------------------------------------------------------------------
def process_task_request(client, say, channel_id, thread_ts, raw_text, is_thread):
    try:
        if is_thread:
            replies = client.conversations_replies(channel=channel_id, ts=thread_ts)
            messages = [m.get("text", "") for m in replies.get("messages", [])]
            full_context = "\n---\n".join(messages)
        else:
            full_context = raw_text

        bot_id = get_bot_user_id(client)
        full_context = full_context.replace(f"<@{bot_id}>", "").strip()

        if not full_context or len(full_context) < 10:
            say("⚠️ Texto muito curto. Tente novamente.", thread_ts=thread_ts)
            return

        # Permalink
        try:
            permalink = client.chat_getPermalink(channel=channel_id, message_ts=thread_ts)["permalink"]
        except:
            permalink = ""

        task_data = analyze_with_gemini(full_context)
        card = create_trello_card(task_data, permalink)

        due = f"\n📅 Prazo: {task_data.get('due_date')}" if task_data.get("due_date") else ""
        priority = f"\n🔥 Prioridade: {task_data.get('priority', 'média')}" if task_data.get("priority") else ""

        say(
            f"✅ Card criado no Trello!\n"
            f"📌 *{card['name']}*{due}{priority}\n"
            f"🔗 <{card['url']}|Ver no Trello>",
            thread_ts=thread_ts
        )
    except Exception as e:
        logger.exception("Erro ao processar")
        say(f"❌ Erro: {str(e)}", thread_ts=thread_ts)

# ---------------------------------------------------------------------------
# Handlers Slack
# ---------------------------------------------------------------------------
@app_slack.event("app_mention")
def handle_mention(body, say, client):
    event = body["event"]
    if event.get("bot_id") or already_processed(body.get("event_id")):
        return
    threading.Thread(
        target=process_task_request,
        args=(client, say, event["channel"], event.get("thread_ts") or event["ts"], event.get("text", ""), "thread_ts" in event),
        daemon=True
    ).start()

@app_slack.event("message")
def handle_message(body, say, client):
    event = body["event"]
    if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id") or already_processed(body.get("event_id")):
        return
    threading.Thread(
        target=process_task_request,
        args=(client, say, event["channel"], event.get("thread_ts") or event["ts"], event.get("text", ""), "thread_ts" in event),
        daemon=True
    ).start()

def already_processed(event_id):
    if not event_id:
        return False
    with _PROCESSED_LOCK:
        if event_id in _PROCESSED_EVENTS:
            return True
        _PROCESSED_EVENTS.add(event_id)
        if len(_PROCESSED_EVENTS) > 500:
            _PROCESSED_EVENTS.pop()
        return False

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    threading.Thread(target=lambda: SocketModeHandler(app_slack, SLACK_APP_TOKEN).start(), daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port, threaded=True)
