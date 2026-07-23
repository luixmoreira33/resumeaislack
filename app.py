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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("resumeai-bot")

# Configurações
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TRELLO_API_KEY = os.environ["TRELLO_API_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TRELLO_LIST_ID = os.environ["TRELLO_LIST_ID"]
TRELLO_MEMBER_ID = os.environ.get("TRELLO_MEMBER_ID")  # 6a618e84843f10fff9bb7e25

app_slack = App(token=SLACK_BOT_TOKEN)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
app_flask = Flask(__name__)

_BOT_USER_ID = None

def get_bot_user_id(client):
    global _BOT_USER_ID
    if _BOT_USER_ID is None:
        _BOT_USER_ID = client.auth_test()["user_id"]
    return _BOT_USER_ID

# Healthcheck
@app_flask.route("/")
def health_check():
    return "✅ ResumeAI Bot está online!", 200

# Gemini
def analyze_with_gemini(text_content: str) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"""Analise e extraia a tarefa principal. Hoje: {today}
Texto: {text_content}
Responda SOMENTE JSON: {{"title":"...", "description":"...", "due_date":"YYYY-MM-DD ou null", "priority":"alta|média|baixa", "labels":["urgente","dev",...]}}"""
    resp = gemini_client.models.generate_content(
        model="gemini-2.5-flash-exp",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    raw = (resp.text or "").strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# Trello Card
def create_trello_card(task_data: dict, slack_link: str = None):
    url = "https://api.trello.com/1/cards"
    desc = task_data.get("description", "")
    if slack_link:
        desc += f"\n\n🔗 Link Slack: {slack_link}"

    query = {
        "key": TRELLO_API_KEY, "token": TRELLO_TOKEN, "idList": TRELLO_LIST_ID,
        "name": task_data["title"][:60], "desc": desc
    }
    if task_data.get("due_date") and str(task_data.get("due_date")).lower() != "null":
        query["due"] = task_data["due_date"]
    if TRELLO_MEMBER_ID:
        query["idMembers"] = TRELLO_MEMBER_ID

    r = requests.post(url, params=query, timeout=10)
    r.raise_for_status()
    return r.json()

# Listar tarefas
def list_recent_tasks():
    url = f"https://api.trello.com/1/lists/{TRELLO_LIST_ID}/cards?limit=10"
    r = requests.get(url, params={"key": TRELLO_API_KEY, "token": TRELLO_TOKEN})
    if r.ok:
        cards = r.json()
        if not cards:
            return "Nenhuma tarefa pendente no momento."
        msg = "📋 *Últimas tarefas:*\n"
        for c in cards:
            due = f" | 📅 {c.get('due')[:10]}" if c.get('due') else ""
            msg += f"• <{c['url']}|{c['name']}>{due}\n"
        return msg
    return "❌ Erro ao buscar tarefas."

# Processar menção/DM
def process_task_request(client, say, channel_id, thread_ts, raw_text, is_thread):
    try:
        if is_thread:
            replies = client.conversations_replies(channel=channel_id, ts=thread_ts)
            full_context = "\n---\n".join(m.get("text", "") for m in replies.get("messages", []))
        else:
            full_context = raw_text

        bot_id = get_bot_user_id(client)
        full_context = full_context.replace(f"<@{bot_id}>", "").strip()

        if len(full_context) < 15:
            say("⚠️ Texto muito curto.", thread_ts=thread_ts)
            return

        permalink = ""
        try:
            permalink = client.chat_getPermalink(channel=channel_id, message_ts=thread_ts)["permalink"]
        except:
            pass

        task = analyze_with_gemini(full_context)
        card = create_trello_card(task, permalink)

        due = f"\n📅 {task.get('due_date')}" if task.get('due_date') else ""
        prio = f"\n🔥 Prioridade: {task.get('priority','média')}" if task.get('priority') else ""

        say(f"✅ Card criado!\n📌 *{card['name']}*{due}{prio}\n🔗 <{card['url']}|Abrir no Trello>", thread_ts=thread_ts)
    except Exception as e:
        logger.error(e)
        say(f"❌ Erro: {str(e)}", thread_ts=thread_ts)

# Handlers
@app_slack.event("app_mention")
def handle_mention(body, say, client):
    event = body["event"]
    if event.get("bot_id"): return
    threading.Thread(target=process_task_request, args=(client, say, event["channel"], event.get("thread_ts") or event["ts"], event.get("text",""), bool(event.get("thread_ts"))), daemon=True).start()

@app_slack.event("message")
def handle_dm(body, say, client):
    event = body["event"]
    if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"): return
    threading.Thread(target=process_task_request, args=(client, say, event["channel"], event.get("thread_ts") or event["ts"], event.get("text",""), bool(event.get("thread_ts"))), daemon=True).start()

@app_slack.command("/tarefas")
def handle_tarefas(ack, say):
    ack()
    say(list_recent_tasks())

# Start
if __name__ == "__main__":
    threading.Thread(target=lambda: SocketModeHandler(app_slack, SLACK_APP_TOKEN).start(), daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port, threaded=True)
