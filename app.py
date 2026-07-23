import os
import json
import requests
import threading
from flask import Flask
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google import genai

# 1. Configuração dos Tokens (vindos do Render)
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TRELLO_API_KEY = os.environ.get("TRELLO_API_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")
TRELLO_LIST_ID = os.environ.get("TRELLO_LIST_ID")

# Inicializa o Slack App e Flask
app_slack = App(token=SLACK_BOT_TOKEN)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
app_flask = Flask(__name__)

# Rota para enganar o Render e manter o bot acordado
@app_flask.route("/")
def health_check():
    return "Bot do Slack está vivo e rodando 24/7!", 200

def analyze_with_gemini(text_content):
    prompt = f"""
    Você é um assistente de produtividade. Analise o seguinte texto/thread do Slack e extraia a tarefa principal.
    Texto: "{text_content}"
    Responda EXCLUSIVAMENTE em formato JSON (sem formatação de código markdown) com a estrutura:
    {{
        "title": "Título curto e objetivo da tarefa (máx 60 caracteres)",
        "description": "Resumo claro do que precisa ser feito, contexto e responsáveis."
    }}
    """
    response = gemini_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
    cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned_text)

def create_trello_card(title, description, slack_link=None):
    url = "https://api.trello.com/1/cards"
    full_description = description
    if slack_link:
        full_description += f"\n\n🔗 **Link da conversa no Slack:** {slack_link}"
        
    query = {
        'key': TRELLO_API_KEY,
        'token': TRELLO_TOKEN,
        'idList': TRELLO_LIST_ID,
        'name': title,
        'desc': full_description
    }
    response = requests.post(url, params=query)
    return response.json()

# Handler de Menções e Mensagens Diretas
@app_slack.event("app_mention")
@app_slack.event("message")
def handle_slack_events(body, say, client):
    event = body.get("event", {})
    if event.get("bot_id"): return # Ignora mensagens do próprio bot

    channel_id = event.get("channel")
    thread_ts = event.get("thread_ts", event.get("ts"))
    
    try:
        if "thread_ts" in event:
            replies = client.conversations_replies(channel=channel_id, ts=thread_ts)
            messages = [msg.get("text", "") for msg in replies.get("messages", [])]
            full_context = "\n---\n".join(messages)
        else:
            full_context = event.get("text", "")

        permalink_resp = client.chat_getPermalink(channel=channel_id, message_ts=thread_ts)
        slack_link = permalink_resp.get("permalink", "")

        task_data = analyze_with_gemini(full_context)
        card = create_trello_card(task_data["title"], task_data["description"], slack_link)

        say(
            text=f"✅ Tarefa criada no Trello!\n📌 *{card['name']}*\n🔗 <{card['url']}|Ver Card no Trello>",
            thread_ts=thread_ts
        )
    except Exception as e:
        say(text=f"❌ Erro ao processar a tarefa: {str(e)}", thread_ts=thread_ts)

# Função para rodar o Slack em Background
def run_slack_bot():
    handler = SocketModeHandler(app_slack, SLACK_APP_TOKEN)
    handler.start()

if __name__ == "__main__":
    # Inicia o bot do Slack em uma thread separada
    threading.Thread(target=run_slack_bot, daemon=True).start()
    
    # Inicia o servidor Web (Flask) na porta exigida pelo Render
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)
