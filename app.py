import os
import json
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

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
# Environment
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
MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "5"))
ATAS_POLL_SECONDS = int(os.environ.get("ATAS_POLL_SECONDS", "900"))  # 15 min

# Google OAuth (opcional até configurar o projeto no Google Cloud)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
GOOGLE_SCOPES = " ".join(
    [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/documents.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "openid",
        "email",
        "profile",
    ]
)

# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------
app_slack = App(token=SLACK_BOT_TOKEN)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
app_flask = Flask(__name__)

_BOT_USER_ID = None
_PROCESSED = set()
_PROCESSED_LOCK = threading.Lock()

IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "image/heic",
    "image/heif",
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    slack_user_id       TEXT PRIMARY KEY,
                    slack_name          TEXT,
                    trello_token        TEXT,
                    trello_member_id    TEXT,
                    trello_list_id      TEXT,
                    trello_board_id     TEXT,
                    google_email        TEXT,
                    google_refresh_token TEXT,
                    google_access_token TEXT,
                    google_token_expiry TIMESTAMPTZ,
                    atas_enabled        BOOLEAN DEFAULT FALSE,
                    atas_last_check_at  TIMESTAMPTZ,
                    atas_last_doc_id    TEXT,
                    created_at          TIMESTAMPTZ DEFAULT NOW(),
                    updated_at          TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
            # Migração segura se a tabela antiga já existir
            for col, typedef in [
                ("google_email", "TEXT"),
                ("google_refresh_token", "TEXT"),
                ("google_access_token", "TEXT"),
                ("google_token_expiry", "TIMESTAMPTZ"),
                ("atas_enabled", "BOOLEAN DEFAULT FALSE"),
                ("atas_last_check_at", "TIMESTAMPTZ"),
                ("atas_last_doc_id", "TEXT"),
            ]:
                cur.execute(
                    f"""
                    DO $$ BEGIN
                        ALTER TABLE users ADD COLUMN {col} {typedef};
                    EXCEPTION WHEN duplicate_column THEN NULL;
                    END $$;
                    """
                )
            # trello_token pode ter sido NOT NULL na versão antiga
            try:
                cur.execute("ALTER TABLE users ALTER COLUMN trello_token DROP NOT NULL")
            except Exception:
                conn.rollback()
        conn.commit()
    logger.info("Database initialized (users + Google/atas columns)")


def get_user(slack_user_id: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE slack_user_id = %s", (slack_user_id,))
            return cur.fetchone()


def ensure_user(slack_user_id: str, slack_name: str = None):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (slack_user_id, slack_name, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (slack_user_id) DO UPDATE SET
                    slack_name = COALESCE(EXCLUDED.slack_name, users.slack_name),
                    updated_at = NOW()
                """,
                (slack_user_id, slack_name),
            )
        conn.commit()


def save_trello(
    slack_user_id: str,
    trello_token: str,
    trello_member_id: str = None,
    slack_name: str = None,
):
    ensure_user(slack_user_id, slack_name)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users SET
                    trello_token = %s,
                    trello_member_id = COALESCE(%s, trello_member_id),
                    slack_name = COALESCE(%s, slack_name),
                    updated_at = NOW()
                WHERE slack_user_id = %s
                """,
                (trello_token, trello_member_id, slack_name, slack_user_id),
            )
        conn.commit()


def update_user_list(slack_user_id: str, list_id: str, board_id: str = None):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users SET
                    trello_list_id = %s,
                    trello_board_id = COALESCE(%s, trello_board_id),
                    updated_at = NOW()
                WHERE slack_user_id = %s
                """,
                (list_id, board_id, slack_user_id),
            )
        conn.commit()


def save_google(
    slack_user_id: str,
    refresh_token: str = None,
    access_token: str = None,
    email: str = None,
    expiry=None,
):
    ensure_user(slack_user_id)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users SET
                    google_refresh_token = COALESCE(%s, google_refresh_token),
                    google_access_token = COALESCE(%s, google_access_token),
                    google_email = COALESCE(%s, google_email),
                    google_token_expiry = COALESCE(%s, google_token_expiry),
                    updated_at = NOW()
                WHERE slack_user_id = %s
                """,
                (refresh_token, access_token, email, expiry, slack_user_id),
            )
        conn.commit()


def set_atas_enabled(slack_user_id: str, enabled: bool):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users SET atas_enabled = %s, updated_at = NOW()
                WHERE slack_user_id = %s
                """,
                (enabled, slack_user_id),
            )
        conn.commit()


def list_users_with_atas_enabled():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM users
                WHERE atas_enabled = TRUE
                  AND google_refresh_token IS NOT NULL
                  AND trello_token IS NOT NULL
                  AND trello_list_id IS NOT NULL
                """
            )
            return cur.fetchall()


def delete_user(slack_user_id: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE slack_user_id = %s", (slack_user_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Trello helpers
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
# Slack helpers + message extraction
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


def download_slack_file(file_meta: dict):
    url = file_meta.get("url_private_download") or file_meta.get("url_private")
    if not url:
        return None, None
    try:
        r = requests.get(
            url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}, timeout=30
        )
        r.raise_for_status()
        mime = file_meta.get("mimetype") or r.headers.get("Content-Type", "application/octet-stream")
        return r.content, mime.split(";")[0].strip()
    except Exception as e:
        logger.warning(f"Falha ao baixar arquivo Slack: {e}")
        return None, None


def extract_from_message(msg: dict):
    parts, images = [], []
    text = (msg.get("text") or "").strip()
    if text:
        parts.append(text)

    for block in msg.get("blocks") or []:
        if block.get("type") == "section":
            t = (block.get("text") or {}).get("text")
            if t and t not in text:
                parts.append(t)

    for att in msg.get("attachments") or []:
        title = att.get("title") or att.get("fallback") or ""
        title_link = att.get("title_link") or att.get("from_url") or ""
        att_text = att.get("text") or att.get("pretext") or ""
        if title or att_text or title_link:
            chunk = "[Anexo/link]"
            if title:
                chunk += f" {title}"
            if title_link:
                chunk += f" ({title_link})"
            if att_text:
                chunk += f": {att_text}"
            parts.append(chunk)

    for f in msg.get("files") or []:
        name = f.get("name") or f.get("title") or "arquivo"
        mime = (f.get("mimetype") or "").lower()
        permalink = f.get("permalink") or ""
        if mime in IMAGE_MIMES or (f.get("filetype") or "").lower() in (
            "png",
            "jpg",
            "jpeg",
            "gif",
            "webp",
            "heic",
            "heif",
        ):
            images.append(f)
            parts.append(f"[Imagem anexada: {name}] {permalink}".strip())
        else:
            parts.append(
                f"[Arquivo: {name} | tipo: {mime or f.get('filetype')}] {permalink}".strip()
            )

    return "\n".join(p for p in parts if p).strip(), images


def build_thread_context(client, channel_id: str, thread_ts: str, fallback_text: str = ""):
    messages = []
    try:
        replies = client.conversations_replies(
            channel=channel_id, ts=thread_ts, inclusive=True, limit=50
        )
        messages = replies.get("messages") or []
    except Exception as e:
        logger.warning(f"conversations_replies falhou: {e}")

    if not messages:
        return (fallback_text or "").strip(), []

    text_chunks, image_metas = [], []
    bot_id = get_bot_user_id(client)
    for msg in messages:
        chunk, imgs = extract_from_message(msg)
        if chunk:
            user = msg.get("user") or msg.get("username") or "alguém"
            text_chunks.append(f"[{user}]: {chunk}")
        image_metas.extend(imgs)

    full_text = "\n---\n".join(text_chunks).replace(f"<@{bot_id}>", "").strip()
    downloaded = []
    for meta in image_metas[:MAX_IMAGES]:
        data, mime = download_slack_file(meta)
        if data and mime:
            downloaded.append((data, mime))
    return full_text, downloaded


def analyze_with_gemini(text_content: str, images=None) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"""Você é um assistente de produtividade. Analise TODO o contexto abaixo
(mensagens de thread do Slack, links, nomes de arquivos e imagens anexadas).

Se houver imagens, leia o texto visível nelas (OCR) e use essas informações.
Preserve detalhes importantes: nomes, prazos, URLs, números, responsáveis.

Data de hoje: {today}

Contexto:
\"\"\"{text_content}\"\"\"

Responda APENAS com JSON válido (sem markdown):
{{
  "title": "Título curto e objetivo (máx 60 caracteres)",
  "description": "Resumo completo do que precisa ser feito",
  "due_date": "YYYY-MM-DD ou null",
  "priority": "alta|média|baixa",
  "labels": ["urgente", "dev", "design", "reunião"]
}}
"""
    parts = [types.Part.from_text(text=prompt)]
    for data, mime in images or []:
        try:
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        except Exception as e:
            logger.warning(f"Não foi possível anexar imagem ao Gemini: {e}")

    resp = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=parts,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    raw = (resp.text or "").strip().replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    if "title" not in data or "description" not in data:
        raise ValueError(f"Resposta incompleta do Gemini: {data}")
    return data


def login_message(slack_user_id: str) -> str:
    link = f"{BASE_URL}/login?slack_user_id={quote(slack_user_id)}"
    return (
        f"🔗 *Configure o {APP_NAME}*\n\n"
        f"1. Clique no link abaixo\n"
        f"2. Autorize o **Trello** (tarefas)\n"
        f"3. Autorize o **Google** (atas por e-mail)\n"
        f"4. Escolha a lista padrão do Trello\n\n"
        f"<{link}|Abrir configuração>\n\n"
        f"Depois use `/atas on` para ativar atas automáticas.\n"
        f"_Comando: `/primeiro-login`_"
    )


def user_ready_for_tasks(user) -> bool:
    return bool(user and user.get("trello_token") and user.get("trello_list_id"))


def user_ready_for_atas(user) -> bool:
    return bool(
        user_ready_for_tasks(user)
        and user.get("google_refresh_token")
        and user.get("atas_enabled")
    )


# ---------------------------------------------------------------------------
# Core Slack task processing
# ---------------------------------------------------------------------------
def process_task_request(client, say, channel_id, thread_ts, raw_text, slack_user_id):
    try:
        user = get_user(slack_user_id)
        if not user_ready_for_tasks(user):
            say(login_message(slack_user_id), thread_ts=thread_ts)
            return

        say("⏳ Analisando mensagem, thread e anexos…", thread_ts=thread_ts)
        full_context, images = build_thread_context(
            client, channel_id, thread_ts, fallback_text=raw_text or ""
        )
        if len(full_context) < 5 and not images:
            say(
                "⚠️ Não encontrei texto nem imagens suficientes para gerar uma tarefa.",
                thread_ts=thread_ts,
            )
            return

        permalink = ""
        try:
            permalink = client.chat_getPermalink(
                channel=channel_id, message_ts=thread_ts
            ).get("permalink", "")
        except Exception:
            pass

        task = analyze_with_gemini(full_context, images)
        card = create_trello_card_for_user(user, task, permalink)

        due = (
            f"\n📅 Prazo: {task.get('due_date')}"
            if task.get("due_date") and str(task.get("due_date")).lower() != "null"
            else ""
        )
        prio = (
            f"\n🔥 Prioridade: {task.get('priority', 'média')}"
            if task.get("priority")
            else ""
        )
        img_note = f"\n🖼️ {len(images)} imagem(ns) analisada(s)" if images else ""

        say(
            f"✅ Tarefa criada no *seu* Trello!\n"
            f"📌 *{card['name']}*{due}{prio}{img_note}\n"
            f"🔗 <{card['url']}|Abrir card>",
            thread_ts=thread_ts,
        )
    except Exception as e:
        logger.exception("process_task_request error")
        say(f"❌ Erro ao processar: {e}", thread_ts=thread_ts)


# ---------------------------------------------------------------------------
# Job de atas (esqueleto)
# ---------------------------------------------------------------------------
def process_atas_for_user(user: dict):
    """
    Esqueleto: será preenchido com Gmail API + Docs API.
    Fluxo previsto:
      1. Refresh do access_token Google
      2. Buscar e-mails recentes de notas de reunião
      3. Extrair link do Google Docs
      4. Ler texto do Doc
      5. Gemini: tarefas atribuídas a este usuário
      6. Criar cards no Trello
      7. DM no Slack: "Criei N tarefas da reunião X"
    """
    logger.info(
        "[atas] job user=%s email=%s (esqueleto — Gmail/Docs ainda não implementados)",
        user.get("slack_user_id"),
        user.get("google_email"),
    )
    # TODO: implementar leitura Gmail + Docs
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users SET atas_last_check_at = NOW(), updated_at = NOW()
                WHERE slack_user_id = %s
                """,
                (user["slack_user_id"],),
            )
        conn.commit()


def atas_worker_loop():
    logger.info("[atas] worker iniciado (intervalo=%ss)", ATAS_POLL_SECONDS)
    while True:
        try:
            users = list_users_with_atas_enabled()
            logger.info("[atas] usuários com atas on: %s", len(users))
            for u in users:
                try:
                    process_atas_for_user(u)
                except Exception:
                    logger.exception("[atas] erro user=%s", u.get("slack_user_id"))
        except Exception:
            logger.exception("[atas] erro no ciclo do worker")
        time.sleep(ATAS_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Slack handlers
# ---------------------------------------------------------------------------
@app_slack.event("app_mention")
def handle_mention(body, say, client):
    event = body.get("event", {})
    if event.get("bot_id") or already_processed(body.get("event_id")):
        return
    threading.Thread(
        target=process_task_request,
        args=(
            client,
            say,
            event.get("channel"),
            event.get("thread_ts") or event.get("ts"),
            event.get("text", ""),
            event.get("user"),
        ),
        daemon=True,
    ).start()


@app_slack.event("message")
def handle_dm(body, say, client):
    event = body.get("event", {})
    if event.get("channel_type") != "im":
        return
    subtype = event.get("subtype")
    if subtype and subtype not in ("file_share",):
        return
    if event.get("bot_id") or already_processed(body.get("event_id")):
        return
    threading.Thread(
        target=process_task_request,
        args=(
            client,
            say,
            event.get("channel"),
            event.get("thread_ts") or event.get("ts"),
            event.get("text", ""),
            event.get("user"),
        ),
        daemon=True,
    ).start()


@app_slack.command("/tarefas")
def handle_tarefas(ack, say, command):
    ack()
    user_id = command.get("user_id")
    user = get_user(user_id)
    if not user_ready_for_tasks(user):
        say(login_message(user_id))
        return
    say(list_recent_tasks_for_user(user))


@app_slack.command("/primeiro-login")
def handle_primeiro_login(ack, say, command):
    ack()
    user_id = command.get("user_id")
    ensure_user(user_id)
    say(login_message(user_id))


@app_slack.command("/desconectar")
def handle_desconectar(ack, say, command):
    ack()
    user_id = command.get("user_id")
    delete_user(user_id)
    say(
        "✅ Conta desconectada (Trello + Google + atas).\n"
        "Use `/primeiro-login` quando quiser configurar de novo."
    )


@app_slack.command("/atas")
def handle_atas(ack, say, command):
    ack()
    user_id = command.get("user_id")
    text = (command.get("text") or "").strip().lower()
    user = get_user(user_id)

    if text in ("on", "ligar", "ativar", "enable"):
        if not user_ready_for_tasks(user):
            say("⚠️ Conecte o Trello antes. Use `/primeiro-login`.")
            return
        if not user.get("google_refresh_token"):
            say(
                "⚠️ Conecte o Google antes de ativar atas.\n"
                "Use `/primeiro-login` e conclua a etapa Google."
            )
            return
        set_atas_enabled(user_id, True)
        say(
            "✅ *Atas automáticas ligadas.*\n"
            "Quando chegar uma ata por e-mail, o ResumeAI vai gerar tarefas no seu Trello.\n"
            "_Use `/atas off` para desligar._"
        )
        return

    if text in ("off", "desligar", "desativar", "disable"):
        set_atas_enabled(user_id, False)
        say("⏸ *Atas automáticas desligadas.* Use `/atas on` para reativar.")
        return

    # status (padrão)
    if not user:
        say("Você ainda não fez `/primeiro-login`.")
        return
    trello_ok = "✅" if user_ready_for_tasks(user) else "❌"
    google_ok = "✅" if user.get("google_refresh_token") else "❌"
    atas_ok = "🟢 ligadas" if user.get("atas_enabled") else "⚪ desligadas"
    last = user.get("atas_last_check_at")
    last_s = last.isoformat() if last else "nunca"
    email = user.get("google_email") or "—"
    say(
        f"*Status ResumeAI*\n"
        f"• Trello: {trello_ok}\n"
        f"• Google: {google_ok} ({email})\n"
        f"• Atas: {atas_ok}\n"
        f"• Última verificação: {last_s}\n\n"
        f"Comandos: `/atas on` · `/atas off` · `/atas status`"
    )


# ---------------------------------------------------------------------------
# HTML pages (unified login)
# ---------------------------------------------------------------------------
PAGE_CSS = """
body { font-family: system-ui, sans-serif; max-width: 520px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
.card { border: 1px solid #e5e5e5; border-radius: 12px; padding: 24px; }
h1 { font-size: 1.35rem; margin: 0 0 8px; }
p { color: #555; line-height: 1.5; }
.steps { list-style: none; padding: 0; margin: 16px 0; }
.steps li { padding: 8px 0; border-bottom: 1px solid #f0f0f0; }
.steps li.done { color: #0a0; }
.steps li.current { font-weight: 700; color: #0079BF; }
a.btn, button.btn {
  display: inline-block; background: #0079BF; color: #fff; text-decoration: none;
  padding: 12px 20px; border-radius: 8px; font-weight: 600; margin-top: 12px; border: none; cursor: pointer; font-size: 1rem;
}
a.btn:hover, button.btn:hover { background: #026aa7; }
a.btn.secondary { background: #5f6368; }
select { width: 100%; padding: 12px; font-size: 1rem; border-radius: 8px; border: 1px solid #ccc; }
label { display: block; font-weight: 600; margin-bottom: 8px; }
"""

LOGIN_HOME = """
<!DOCTYPE html><html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ app_name }} — Configuração</title><style>{{ css }}</style></head><body>
<div class="card">
  <h1>Configurar {{ app_name }}</h1>
  <p>Em poucos passos você conecta Trello e Google. Depois, no Slack, ative as atas com <code>/atas on</code>.</p>
  <ol class="steps">
    <li class="{{ 'done' if trello_ok else 'current' }}">1. Trello {% if trello_ok %}✓{% endif %}</li>
    <li class="{{ 'done' if google_ok else ('current' if trello_ok else '') }}">2. Google {% if google_ok %}✓{% endif %}</li>
    <li class="{{ 'done' if list_ok else ('current' if trello_ok and google_ok else '') }}">3. Lista do Trello {% if list_ok %}✓{% endif %}</li>
  </ol>
  {% if not trello_ok %}
    <a class="btn" href="{{ trello_url }}">Continuar: autorizar Trello</a>
  {% elif not google_ok %}
    <a class="btn" href="{{ google_url }}">Continuar: autorizar Google</a>
    {% if google_skip %}<p><a href="{{ skip_google_url }}">Pular Google por enquanto</a></p>{% endif %}
  {% elif not list_ok %}
    <a class="btn" href="{{ list_url }}">Continuar: escolher lista</a>
  {% else %}
    <p><strong>Tudo configurado!</strong> Volte ao Slack e use <code>/atas on</code>.</p>
  {% endif %}
</div></body></html>
"""

TRELLO_CONNECT_PAGE = """
<!DOCTYPE html><html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ app_name }} — Trello</title><style>{{ css }}</style></head><body>
<div class="card">
  <h1>Passo 1 · Trello</h1>
  <p>Autorize o acesso para criar cards no <strong>seu</strong> Trello.</p>
  <a class="btn" href="{{ auth_url }}">Autorizar no Trello</a>
</div></body></html>
"""

TRELLO_CALLBACK_PAGE = """
<!DOCTYPE html><html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ app_name }}</title><style>{{ css }}</style></head><body>
<div class="card"><p id="msg">Finalizando Trello…</p></div>
<script>
(function(){
  var hash = window.location.hash || "";
  var params = new URLSearchParams(hash.replace(/^#/, ""));
  var token = params.get("token");
  var err = params.get("error");
  var uid = "{{ slack_user_id }}";
  if (err || !token) {
    document.getElementById("msg").textContent = "Autorização cancelada. Volte ao Slack e tente de novo.";
    return;
  }
  fetch("/trello/save", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({slack_user_id: uid, token: token})
  }).then(r => r.json()).then(function(data){
    if (data.redirect) window.location = data.redirect;
    else document.getElementById("msg").textContent = data.error || "Erro desconhecido";
  }).catch(function(e){
    document.getElementById("msg").textContent = "Erro de rede: " + e;
  });
})();
</script></body></html>
"""

GOOGLE_CONNECT_PAGE = """
<!DOCTYPE html><html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ app_name }} — Google</title><style>{{ css }}</style></head><body>
<div class="card">
  <h1>Passo 2 · Google</h1>
  <p>Autorize leitura do Gmail e Docs para processar <strong>atas de reunião</strong> automaticamente.</p>
  {% if oauth_ready %}
    <a class="btn" href="{{ auth_url }}">Autorizar no Google</a>
  {% else %}
    <p style="color:#a60">OAuth Google ainda não configurado no servidor
    (<code>GOOGLE_CLIENT_ID</code> / <code>GOOGLE_CLIENT_SECRET</code>).</p>
    <a class="btn secondary" href="{{ skip_url }}">Continuar sem Google</a>
  {% endif %}
  <p><a href="{{ skip_url }}">Pular esta etapa</a></p>
</div></body></html>
"""

SELECT_LIST_PAGE = """
<!DOCTYPE html><html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ app_name }} — Lista</title><style>{{ css }}</style></head><body>
<div class="card">
  <h1>Passo 3 · Lista do Trello</h1>
  <p>As tarefas criadas pelo bot irão para este quadro/lista.</p>
  <form method="POST" action="/trello/select-list">
    <input type="hidden" name="slack_user_id" value="{{ slack_user_id }}">
    <label for="list_id">Quadro → Lista</label>
    <select id="list_id" name="list_id" required>
      {% for opt in options %}
        <option value="{{ opt.value }}"{% if opt.selected %} selected{% endif %}>{{ opt.label }}</option>
      {% endfor %}
    </select>
    <button class="btn" type="submit">Salvar e concluir</button>
  </form>
</div></body></html>
"""

SUCCESS_PAGE = """
<!DOCTYPE html><html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ app_name }} — Pronto</title><style>{{ css }}</style></head><body>
<div class="card" style="text-align:center">
  <h1 style="color:#0079BF">✅ Tudo pronto!</h1>
  <p>Volte ao Slack.</p>
  <p>Para atas automáticas: <code>/atas on</code></p>
  <p>Outros: <code>/tarefas</code> · <code>/atas status</code> · <code>/desconectar</code></p>
</div></body></html>
"""


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app_flask.route("/")
def health_check():
    return f"✅ {APP_NAME} está online!", 200


@app_flask.route("/login")
def login_home():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id:
        return "slack_user_id é obrigatório", 400
    ensure_user(slack_user_id)
    user = get_user(slack_user_id) or {}
    trello_ok = bool(user.get("trello_token"))
    google_ok = bool(user.get("google_refresh_token"))
    list_ok = bool(user.get("trello_list_id"))
    return render_template_string(
        LOGIN_HOME,
        app_name=APP_NAME,
        css=PAGE_CSS,
        trello_ok=trello_ok,
        google_ok=google_ok,
        list_ok=list_ok,
        trello_url=f"/trello/connect?slack_user_id={quote(slack_user_id)}",
        google_url=f"/google/connect?slack_user_id={quote(slack_user_id)}",
        list_url=f"/trello/select-list?slack_user_id={quote(slack_user_id)}",
        skip_google_url=f"/google/skip?slack_user_id={quote(slack_user_id)}",
        google_skip=True,
    )


@app_flask.route("/trello/connect")
def trello_connect():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id:
        return "slack_user_id é obrigatório", 400
    return_url = f"{BASE_URL}/trello/callback?slack_user_id={quote(slack_user_id)}"
    auth_url = (
        "https://trello.com/1/authorize"
        f"?expiration=never&scope=read,write&response_type=token"
        f"&name={quote(APP_NAME)}&key={TRELLO_API_KEY}"
        f"&return_url={quote(return_url)}&callback_method=fragment"
    )
    return render_template_string(
        TRELLO_CONNECT_PAGE, app_name=APP_NAME, css=PAGE_CSS, auth_url=auth_url
    )


@app_flask.route("/trello/callback")
def trello_callback():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id:
        return "slack_user_id é obrigatório", 400
    return render_template_string(
        TRELLO_CALLBACK_PAGE,
        app_name=APP_NAME,
        css=PAGE_CSS,
        slack_user_id=slack_user_id,
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
        save_trello(
            slack_user_id=slack_user_id,
            trello_token=token,
            trello_member_id=member.get("id"),
            slack_name=member.get("fullName") or member.get("username"),
        )
        # Próximo passo: Google (ou lista se Google já ok)
        user = get_user(slack_user_id)
        if user and user.get("google_refresh_token"):
            return {"redirect": f"/trello/select-list?slack_user_id={quote(slack_user_id)}"}
        return {"redirect": f"/google/connect?slack_user_id={quote(slack_user_id)}"}
    except Exception as e:
        logger.exception("trello_save error")
        return {"error": str(e)}, 500


@app_flask.route("/google/connect")
def google_connect():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id:
        return "slack_user_id é obrigatório", 400
    skip_url = f"/google/skip?slack_user_id={quote(slack_user_id)}"
    auth_url = "#"
    if GOOGLE_OAUTH_ENABLED:
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": f"{BASE_URL}/google/callback",
            "response_type": "code",
            "scope": GOOGLE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": slack_user_id,
        }
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return render_template_string(
        GOOGLE_CONNECT_PAGE,
        app_name=APP_NAME,
        css=PAGE_CSS,
        oauth_ready=GOOGLE_OAUTH_ENABLED,
        auth_url=auth_url,
        skip_url=skip_url,
    )


@app_flask.route("/google/callback")
def google_callback():
    """Troca code por tokens e salva no usuário."""
    code = request.args.get("code", "").strip()
    slack_user_id = request.args.get("state", "").strip()
    err = request.args.get("error")
    if err or not code or not slack_user_id:
        return f"Falha na autorização Google: {err or 'dados ausentes'}", 400
    if not GOOGLE_OAUTH_ENABLED:
        return "Google OAuth não configurado no servidor", 500
    try:
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": f"{BASE_URL}/google/callback",
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        expires_in = tokens.get("expires_in", 3600)
        expiry = datetime.now(timezone.utc).timestamp() + int(expires_in)
        expiry_dt = datetime.fromtimestamp(expiry, tz=timezone.utc)

        email = None
        try:
            ui = requests.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access}"},
                timeout=15,
            )
            if ui.ok:
                email = ui.json().get("email")
        except Exception:
            pass

        save_google(
            slack_user_id=slack_user_id,
            refresh_token=refresh,
            access_token=access,
            email=email,
            expiry=expiry_dt,
        )
        return redirect(f"/trello/select-list?slack_user_id={quote(slack_user_id)}")
    except Exception as e:
        logger.exception("google_callback error")
        return f"Erro ao salvar Google: {e}", 500


@app_flask.route("/google/skip")
def google_skip():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id:
        return "slack_user_id é obrigatório", 400
    user = get_user(slack_user_id)
    if user and user.get("trello_list_id"):
        return redirect("/login/done")
    return redirect(f"/trello/select-list?slack_user_id={quote(slack_user_id)}")


@app_flask.route("/login/done")
def login_done():
    return render_template_string(SUCCESS_PAGE, app_name=APP_NAME, css=PAGE_CSS)


@app_flask.route("/trello/select-list", methods=["GET", "POST"])
def trello_select_list():
    if request.method == "POST":
        slack_user_id = request.form.get("slack_user_id", "").strip()
        raw = request.form.get("list_id", "")
        if not slack_user_id or "|" not in raw:
            return "Dados inválidos", 400
        list_id, board_id = raw.split("|", 1)
        update_user_list(slack_user_id, list_id, board_id)
        return render_template_string(SUCCESS_PAGE, app_name=APP_NAME, css=PAGE_CSS)

    slack_user_id = request.args.get("slack_user_id", "").strip()
    user = get_user(slack_user_id)
    if not user or not user.get("trello_token"):
        return "Usuário sem Trello. Refaça /primeiro-login.", 404

    try:
        boards_raw = trello_boards(user["trello_token"])
        options, preferred = [], None
        for b in boards_raw:
            if b.get("closed"):
                continue
            lists = [
                lst
                for lst in trello_lists(user["trello_token"], b["id"])
                if not lst.get("closed")
            ]
            for lst in lists:
                label = f"{b['name']} → {lst['name']}"
                value = f"{lst['id']}|{b['id']}"
                opt = {"label": label, "value": value, "selected": False}
                name_b = (b.get("name") or "").lower()
                name_l = (lst.get("name") or "").lower()
                if preferred is None and (
                    "resumeai" in name_b
                    or name_l in ("a fazer", "to do", "todo", "inbox", "caixa de entrada")
                ):
                    preferred = len(options)
                options.append(opt)
        if not options:
            return "Nenhum quadro/lista aberto no Trello.", 400
        if preferred is not None:
            options[preferred]["selected"] = True
        else:
            options[0]["selected"] = True
        return render_template_string(
            SELECT_LIST_PAGE,
            app_name=APP_NAME,
            css=PAGE_CSS,
            slack_user_id=slack_user_id,
            options=options,
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
    threading.Thread(target=atas_worker_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port, threaded=True)
