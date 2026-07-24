import os
import json
import logging
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, redirect, render_template_string
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
# Environment variables
# ---------------------------------------------------------------------------
REQUIRED = [
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "GEMINI_API_KEY",
    "TRELLO_API_KEY",
    "DATABASE_URL",
    "BASE_URL",
]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TRELLO_API_KEY = os.environ["TRELLO_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
BASE_URL = os.environ["BASE_URL"].rstrip("/")
APP_NAME = os.environ.get("APP_NAME", "ResumeAI")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------
app_slack = App(token=SLACK_BOT_TOKEN)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
app_flask = Flask(__name__)

_BOT_USER_ID = None
_PROCESSED = set()
_PROCESSED_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    """Create tables if they do not exist."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    slack_user_id   TEXT PRIMARY KEY,
                    slack_name      TEXT,
                    trello_token    TEXT NOT NULL,
                    trello_member_id TEXT,
                    trello_list_id  TEXT,
                    trello_board_id TEXT,
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
        conn.commit()
    logger.info("Database initialized")


def get_user(slack_user_id: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE slack_user_id = %s", (slack_user_id,))
            return cur.fetchone()


def save_user(
    slack_user_id: str,
    trello_token: str,
    trello_member_id: str = None,
    trello_list_id: str = None,
    trello_board_id: str = None,
    slack_name: str = None,
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (
                    slack_user_id, slack_name, trello_token, trello_member_id,
                    trello_list_id, trello_board_id, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (slack_user_id) DO UPDATE SET
                    slack_name = COALESCE(EXCLUDED.slack_name, users.slack_name),
                    trello_token = EXCLUDED.trello_token,
                    trello_member_id = COALESCE(EXCLUDED.trello_member_id, users.trello_member_id),
                    trello_list_id = COALESCE(EXCLUDED.trello_list_id, users.trello_list_id),
                    trello_board_id = COALESCE(EXCLUDED.trello_board_id, users.trello_board_id),
                    updated_at = NOW()
                """,
                (
                    slack_user_id,
                    slack_name,
                    trello_token,
                    trello_member_id,
                    trello_list_id,
                    trello_board_id,
                ),
            )
        conn.commit()


def update_user_list(slack_user_id: str, list_id: str, board_id: str = None):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET trello_list_id = %s,
                    trello_board_id = COALESCE(%s, trello_board_id),
                    updated_at = NOW()
                WHERE slack_user_id = %s
                """,
                (list_id, board_id, slack_user_id),
            )
        conn.commit()


def delete_user(slack_user_id: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE slack_user_id = %s", (slack_user_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Trello helpers (per-user token)
# ---------------------------------------------------------------------------
def trello_get(path: str, token: str, params: dict = None):
    p = {"key": TRELLO_API_KEY, "token": token}
    if params:
        p.update(params)
    r = requests.get(f"https://api.trello.com/1{path}", params=p, timeout=15)
    r.raise_for_status()
    return r.json()


def trello_member(token: str):
    return trello_get("/members/me", token)


def trello_boards(token: str):
    return trello_get("/members/me/boards", token, {"fields": "name,id,closed", "filter": "open"})


def trello_lists(token: str, board_id: str):
    return trello_get(f"/boards/{board_id}/lists", token, {"fields": "name,id,closed"})


def create_trello_card_for_user(user: dict, task_data: dict, slack_link: str = None):
    url = "https://api.trello.com/1/cards"
    desc = task_data.get("description", "")
    if slack_link:
        desc += f"\n\n🔗 Link Slack: {slack_link}"

    query = {
        "key": TRELLO_API_KEY,
        "token": user["trello_token"],
        "idList": user["trello_list_id"],
        "name": task_data["title"][:60],
        "desc": desc,
    }
    if task_data.get("due_date") and str(task_data.get("due_date")).lower() != "null":
        query["due"] = task_data["due_date"]
    if user.get("trello_member_id"):
        query["idMembers"] = user["trello_member_id"]

    r = requests.post(url, params=query, timeout=15)
    r.raise_for_status()
    return r.json()


def list_recent_tasks_for_user(user: dict) -> str:
    if not user.get("trello_list_id"):
        return "⚠️ Você ainda não escolheu uma lista padrão. Use `/primeiro-login` novamente."
    try:
        cards = trello_get(
            f"/lists/{user['trello_list_id']}/cards",
            user["trello_token"],
            {"limit": 10},
        )
        if not cards:
            return "Nenhuma tarefa pendente no momento."
        msg = "📋 *Últimas tarefas:*\n"
        for c in cards:
            due = f" | 📅 {c.get('due')[:10]}" if c.get("due") else ""
            msg += f"• <{c['url']}|{c['name']}>{due}\n"
        return msg
    except Exception as e:
        logger.exception("list tasks error")
        return f"❌ Erro ao buscar tarefas: {e}"


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def analyze_with_gemini(text_content: str) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"""Você é um assistente de produtividade. Analise o texto abaixo e extraia a tarefa principal.

Data de hoje: {today}

Texto:
\"\"\"{text_content}\"\"\"

Responda APENAS com JSON válido (sem markdown):
{{
  "title": "Título curto e objetivo (máx 60 caracteres)",
  "description": "Resumo claro do que precisa ser feito, contexto e responsáveis",
  "due_date": "YYYY-MM-DD ou null",
  "priority": "alta|média|baixa",
  "labels": ["urgente", "dev", "design", "reunião"]
}}
"""
    resp = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    raw = (resp.text or "").strip().replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    if "title" not in data or "description" not in data:
        raise ValueError(f"Resposta incompleta do Gemini: {data}")
    return data


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------
def get_bot_user_id(client):
    global _BOT_USER_ID
    if _BOT_USER_ID is None:
        _BOT_USER_ID = client.auth_test()["user_id"]
    return _BOT_USER_ID


def already_processed(event_id: str) -> bool:
    if not event_id:
        return False
    with _PROCESSED_LOCK:
        if event_id in _PROCESSED:
            return True
        _PROCESSED.add(event_id)
        if len(_PROCESSED) > 500:
            _PROCESSED.pop()
        return False


def connect_message(slack_user_id: str) -> str:
    link = f"{BASE_URL}/trello/connect?slack_user_id={quote(slack_user_id)}"
    return (
        f"🔗 *Conecte seu Trello para começar a usar o {APP_NAME}*\n\n"
        f"1. Clique no link abaixo\n"
        f"2. Autorize o acesso no Trello\n"
        f"3. Escolha a lista padrão onde as tarefas serão criadas\n\n"
        f"<{link}|Conectar meu Trello>\n\n"
        f"_Ou digite `/primeiro-login` a qualquer momento._"
    )


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------
def process_task_request(client, say, channel_id, thread_ts, raw_text, is_thread, slack_user_id):
    try:
        user = get_user(slack_user_id)
        if not user or not user.get("trello_token") or not user.get("trello_list_id"):
            say(connect_message(slack_user_id), thread_ts=thread_ts)
            return

        if is_thread:
            replies = client.conversations_replies(channel=channel_id, ts=thread_ts)
            full_context = "\n---\n".join(m.get("text", "") for m in replies.get("messages", []))
        else:
            full_context = raw_text

        bot_id = get_bot_user_id(client)
        full_context = full_context.replace(f"<@{bot_id}>", "").strip()

        if len(full_context) < 10:
            say("⚠️ Texto muito curto para gerar uma tarefa.", thread_ts=thread_ts)
            return

        permalink = ""
        try:
            permalink = client.chat_getPermalink(channel=channel_id, message_ts=thread_ts).get("permalink", "")
        except Exception:
            pass

        task = analyze_with_gemini(full_context)
        card = create_trello_card_for_user(user, task, permalink)

        due = f"\n📅 Prazo: {task.get('due_date')}" if task.get("due_date") and str(task.get("due_date")).lower() != "null" else ""
        prio = f"\n🔥 Prioridade: {task.get('priority', 'média')}" if task.get("priority") else ""

        say(
            f"✅ Tarefa criada no *seu* Trello!\n"
            f"📌 *{card['name']}*{due}{prio}\n"
            f"🔗 <{card['url']}|Abrir card>",
            thread_ts=thread_ts,
        )
    except Exception as e:
        logger.exception("process_task_request error")
        say(f"❌ Erro ao processar: {e}", thread_ts=thread_ts)


# ---------------------------------------------------------------------------
# Slack event handlers
# ---------------------------------------------------------------------------
@app_slack.event("app_mention")
def handle_mention(body, say, client):
    event = body.get("event", {})
    if event.get("bot_id") or already_processed(body.get("event_id")):
        return
    slack_user_id = event.get("user")
    channel_id = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    is_thread = "thread_ts" in event
    threading.Thread(
        target=process_task_request,
        args=(client, say, channel_id, thread_ts, event.get("text", ""), is_thread, slack_user_id),
        daemon=True,
    ).start()


@app_slack.event("message")
def handle_dm(body, say, client):
    event = body.get("event", {})
    if event.get("channel_type") != "im":
        return
    if event.get("subtype") or event.get("bot_id"):
        return
    if already_processed(body.get("event_id")):
        return
    slack_user_id = event.get("user")
    channel_id = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    is_thread = "thread_ts" in event
    threading.Thread(
        target=process_task_request,
        args=(client, say, channel_id, thread_ts, event.get("text", ""), is_thread, slack_user_id),
        daemon=True,
    ).start()


@app_slack.command("/tarefas")
def handle_tarefas(ack, say, command):
    ack()
    user_id = command.get("user_id")
    user = get_user(user_id)
    if not user or not user.get("trello_list_id"):
        say(connect_message(user_id))
        return
    say(list_recent_tasks_for_user(user))


@app_slack.command("/primeiro-login")
def handle_primeiro_login(ack, say, command):
    ack()
    user_id = command.get("user_id")
    say(connect_message(user_id))


@app_slack.command("/desconectar")
def handle_desconectar(ack, say, command):
    ack()
    user_id = command.get("user_id")
    delete_user(user_id)
    say("✅ Seu Trello foi desconectado. Use `/primeiro-login` quando quiser vincular de novo.")


# ---------------------------------------------------------------------------
# Flask routes — health + Trello OAuth
# ---------------------------------------------------------------------------
@app_flask.route("/")
def health_check():
    return f"✅ {APP_NAME} está online!", 200


CONNECT_PAGE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ app_name }} — Conectar Trello</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
    .card { border: 1px solid #e5e5e5; border-radius: 12px; padding: 24px; }
    h1 { font-size: 1.4rem; margin: 0 0 8px; }
    p { color: #555; line-height: 1.5; }
    a.btn { display: inline-block; background: #0079BF; color: #fff; text-decoration: none;
            padding: 12px 20px; border-radius: 8px; font-weight: 600; margin-top: 12px; }
    a.btn:hover { background: #026aa7; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Conectar Trello ao {{ app_name }}</h1>
    <p>Autorize o acesso para que o bot possa criar cards no <strong>seu</strong> Trello.</p>
    <p>Você será redirecionado ao Trello. Após autorizar, escolha a lista padrão.</p>
    <a class="btn" href="{{ auth_url }}">Autorizar no Trello</a>
  </div>
</body>
</html>
"""

CALLBACK_PAGE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ app_name }} — Finalizando</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 40px auto; padding: 0 16px; }
    .card { border: 1px solid #e5e5e5; border-radius: 12px; padding: 24px; }
  </style>
</head>
<body>
  <div class="card">
    <p id="msg">Finalizando conexão com o Trello…</p>
  </div>
  <script>
    (function () {
      var hash = window.location.hash || "";
      var params = new URLSearchParams(hash.replace(/^#/, ""));
      var token = params.get("token");
      var err = params.get("error");
      var uid = "{{ slack_user_id }}";
      if (err || !token) {
        document.getElementById("msg").textContent = "Autorização cancelada ou token ausente. Feche esta aba e tente novamente no Slack.";
        return;
      }
      fetch("/trello/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slack_user_id: uid, token: token })
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.redirect) {
          window.location = data.redirect;
        } else if (data.error) {
          document.getElementById("msg").textContent = "Erro: " + data.error;
        } else {
          document.getElementById("msg").textContent = "Conectado! Você já pode voltar ao Slack.";
        }
      })
      .catch(function (e) {
        document.getElementById("msg").textContent = "Erro de rede: " + e;
      });
    })();
  </script>
</body>
</html>
"""

SELECT_LIST_PAGE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ app_name }} — Escolher lista</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 560px; margin: 40px auto; padding: 0 16px; }
    .card { border: 1px solid #e5e5e5; border-radius: 12px; padding: 24px; }
    h1 { font-size: 1.3rem; }
    select, button { width: 100%; padding: 12px; margin-top: 8px; font-size: 1rem; border-radius: 8px; }
    button { background: #0079BF; color: #fff; border: none; font-weight: 600; cursor: pointer; }
    button:hover { background: #026aa7; }
    .board { margin-top: 16px; padding-top: 12px; border-top: 1px solid #eee; }
    .board h3 { margin: 0 0 6px; font-size: 1rem; color: #333; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Escolha a lista padrão</h1>
    <p>As tarefas criadas pelo bot irão para esta lista do Trello.</p>
    <form method="POST" action="/trello/select-list">
      <input type="hidden" name="slack_user_id" value="{{ slack_user_id }}">
      {% for board in boards %}
        <div class="board">
          <h3>{{ board.name }}</h3>
          <select name="list_id">
            {% for lst in board.lists %}
              <option value="{{ lst.id }}|{{ board.id }}">{{ lst.name }}</option>
            {% endfor %}
          </select>
        </div>
      {% endfor %}
      <button type="submit">Salvar lista padrão</button>
    </form>
  </div>
</body>
</html>
"""

SUCCESS_PAGE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ app_name }} — Conectado</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 40px auto; padding: 0 16px; text-align: center; }
    .card { border: 1px solid #e5e5e5; border-radius: 12px; padding: 32px; }
    h1 { color: #0079BF; }
  </style>
</head>
<body>
  <div class="card">
    <h1>✅ Trello conectado!</h1>
    <p>Você já pode voltar ao Slack e marcar o bot ou enviar mensagens no DM.</p>
    <p>Comandos úteis: <code>/tarefas</code> · <code>/primeiro-login</code> · <code>/desconectar</code></p>
  </div>
</body>
</html>
"""


@app_flask.route("/trello/connect")
def trello_connect():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id:
        return "slack_user_id é obrigatório", 400

    return_url = f"{BASE_URL}/trello/callback?slack_user_id={quote(slack_user_id)}"
    auth_url = (
        "https://trello.com/1/authorize"
        f"?expiration=never"
        f"&scope=read,write"
        f"&response_type=token"
        f"&name={quote(APP_NAME)}"
        f"&key={TRELLO_API_KEY}"
        f"&return_url={quote(return_url)}"
        f"&callback_method=fragment"
    )
    return render_template_string(CONNECT_PAGE, app_name=APP_NAME, auth_url=auth_url)


@app_flask.route("/trello/callback")
def trello_callback():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id:
        return "slack_user_id é obrigatório", 400
    return render_template_string(
        CALLBACK_PAGE, app_name=APP_NAME, slack_user_id=slack_user_id
    )


@app_flask.route("/trello/save", methods=["POST"])
def trello_save():
    data = request.get_json(force=True, silent=True) or {}
    slack_user_id = (data.get("slack_user_id") or "").strip()
    token = (data.get("token") or "").strip()
    if not slack_user_id or not token:
        return {"error": "slack_user_id e token são obrigatórios"}, 400

    try:
        member = trello_member(token)
        save_user(
            slack_user_id=slack_user_id,
            trello_token=token,
            trello_member_id=member.get("id"),
            slack_name=member.get("fullName") or member.get("username"),
        )
        return {"redirect": f"/trello/select-list?slack_user_id={quote(slack_user_id)}"}
    except Exception as e:
        logger.exception("trello_save error")
        return {"error": str(e)}, 500


@app_flask.route("/trello/select-list", methods=["GET", "POST"])
def trello_select_list():
    if request.method == "POST":
        slack_user_id = request.form.get("slack_user_id", "").strip()
        raw = request.form.get("list_id", "")
        if not slack_user_id or "|" not in raw:
            return "Dados inválidos", 400
        list_id, board_id = raw.split("|", 1)
        update_user_list(slack_user_id, list_id, board_id)
        return render_template_string(SUCCESS_PAGE, app_name=APP_NAME)

    slack_user_id = request.args.get("slack_user_id", "").strip()
    user = get_user(slack_user_id)
    if not user or not user.get("trello_token"):
        return "Usuário não encontrado. Refaça o fluxo /primeiro-login.", 404

    try:
        boards_raw = trello_boards(user["trello_token"])
        boards = []
        for b in boards_raw:
            if b.get("closed"):
                continue
            lists = [
                lst for lst in trello_lists(user["trello_token"], b["id"])
                if not lst.get("closed")
            ]
            if lists:
                boards.append({"id": b["id"], "name": b["name"], "lists": lists})
        if not boards:
            return "Nenhum board/lista aberto encontrado no seu Trello.", 400
        return render_template_string(
            SELECT_LIST_PAGE,
            app_name=APP_NAME,
            slack_user_id=slack_user_id,
            boards=boards,
        )
    except Exception as e:
        logger.exception("select-list error")
        return f"Erro ao listar boards: {e}", 500


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    threading.Thread(
        target=lambda: SocketModeHandler(app_slack, SLACK_APP_TOKEN).start(),
        daemon=True,
    ).start()
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port, threaded=True)
