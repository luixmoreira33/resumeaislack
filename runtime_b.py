def atas_worker_loop():
    logger.info("[atas] worker iniciado (intervalo=%ss)", ATAS_POLL_SECONDS)
    while True:
        try:
            if not GOOGLE_OAUTH_ENABLED:
                logger.warning("[atas] GOOGLE_CLIENT_ID/SECRET ausentes")
            else:
                users = list_users_with_atas_enabled()
                logger.info("[atas] usuários com atas on: %s", len(users))
                for u in users:
                    try: process_atas_for_user(u)
                    except Exception: logger.exception("[atas] erro user=%s", u.get("slack_user_id"))
        except Exception:
            logger.exception("[atas] erro no ciclo")
        time.sleep(ATAS_POLL_SECONDS)

def get_bot_user_id(client):
    global _BOT_USER_ID
    if _BOT_USER_ID is None:
        _BOT_USER_ID = client.auth_test()["user_id"]
    return _BOT_USER_ID

def already_processed(event_id):
    if not event_id: return False
    with _PROCESSED_LOCK:
        if event_id in _PROCESSED: return True
        _PROCESSED.add(event_id)
        if len(_PROCESSED) > 500: _PROCESSED.pop()
        return False

def download_slack_file(file_meta):
    url = file_meta.get("url_private_download") or file_meta.get("url_private")
    if not url: return None, None
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}, timeout=30)
        r.raise_for_status()
        mime = file_meta.get("mimetype") or r.headers.get("Content-Type", "application/octet-stream")
        return r.content, mime.split(";")[0].strip()
    except Exception as e:
        logger.warning(f"Falha ao baixar arquivo: {e}")
        return None, None

def extract_from_message(msg):
    parts, images = [], []
    text = (msg.get("text") or "").strip()
    if text: parts.append(text)
    for block in msg.get("blocks") or []:
        if block.get("type") == "section":
            t = (block.get("text") or {}).get("text")
            if t and t not in text: parts.append(t)
    for att in msg.get("attachments") or []:
        title = att.get("title") or att.get("fallback") or ""
        title_link = att.get("title_link") or att.get("from_url") or ""
        att_text = att.get("text") or att.get("pretext") or ""
        if title or att_text or title_link:
            chunk = "[Anexo/link]"
            if title: chunk += f" {title}"
            if title_link: chunk += f" ({title_link})"
            if att_text: chunk += f": {att_text}"
            parts.append(chunk)
    for f in msg.get("files") or []:
        name = f.get("name") or f.get("title") or "arquivo"
        mime = (f.get("mimetype") or "").lower()
        permalink = f.get("permalink") or ""
        if mime in IMAGE_MIMES or (f.get("filetype") or "").lower() in ("png","jpg","jpeg","gif","webp","heic","heif"):
            images.append(f)
            parts.append(f"[Imagem anexada: {name}] {permalink}".strip())
        else:
            parts.append(f"[Arquivo: {name} | tipo: {mime or f.get('filetype')}] {permalink}".strip())
    return "\n".join(p for p in parts if p).strip(), images

def build_thread_context(client, channel_id, thread_ts, fallback_text=""):
    messages = []
    try:
        messages = client.conversations_replies(channel=channel_id, ts=thread_ts, inclusive=True, limit=50).get("messages") or []
    except Exception as e:
        logger.warning(f"conversations_replies falhou: {e}")
    if not messages: return (fallback_text or "").strip(), []
    text_chunks, image_metas = [], []
    bot_id = get_bot_user_id(client)
    for msg in messages:
        chunk, imgs = extract_from_message(msg)
        if chunk:
            text_chunks.append(f"[{msg.get('user') or msg.get('username') or 'alguém'}]: {chunk}")
        image_metas.extend(imgs)
    full_text = "\n---\n".join(text_chunks).replace(f"<@{bot_id}>", "").strip()
    downloaded = []
    for meta in image_metas[:MAX_IMAGES]:
        data, mime = download_slack_file(meta)
        if data and mime: downloaded.append((data, mime))
    return full_text, downloaded

def analyze_with_gemini(text_content, images=None):
    return _analyze_with_gemini(gemini_client, text_content, images)

def login_message(slack_user_id):
    link = f"{BASE_URL}/login?slack_user_id={quote(slack_user_id)}"
    return (f"🔗 *Configure o {APP_NAME}*\n\n1. Clique no link\n2. Autorize **Trello**\n3. Autorize **Google** (atas)\n"
            f"4. Escolha a lista\n\n<{link}|Abrir configuração>\n\nDepois `/atas-on`.\n_Comando: `/primeiro-login`_")

def user_ready_for_tasks(user):
    return bool(user and user.get("trello_token") and user.get("trello_list_id"))

def process_task_request(client, say, channel_id, thread_ts, raw_text, slack_user_id):
    try:
        user = get_user(slack_user_id)
        if not user_ready_for_tasks(user):
            say(login_message(slack_user_id), thread_ts=thread_ts)
            return
        say("⏳ Analisando mensagem, thread e anexos…", thread_ts=thread_ts)
        full_context, images = build_thread_context(client, channel_id, thread_ts, fallback_text=raw_text or "")
        if len(full_context) < 5 and not images:
            say("⚠️ Texto/imagens insuficientes.", thread_ts=thread_ts)
            return
        permalink = ""
        try:
            permalink = client.chat_getPermalink(channel=channel_id, message_ts=thread_ts).get("permalink", "")
        except Exception:
            pass
        analysis = analyze_with_gemini(full_context, images)
        result = upsert_theme_card(user, theme=analysis.get("theme") or "Tarefa",
            items=analysis.get("items") or [], description=analysis.get("description") or "",
            source_link=permalink or None)
        card, n, skipped = result["card"], len(result["items_added"]), len(result["items_skipped"])
        head = f"✅ Card *{result['theme']}* criado com checklist" if result["created_new"] else f"✅ Itens adicionados ao card *{result['theme']}*"
        body = "\n".join(f"• {t}" for t in result["items_added"][:8]) if n else "_(nenhum item novo)_"
        skip_note = f"\n_({skipped} já existiam)_" if skipped else ""
        img_note = f"\n🖼️ {len(images)} imagem(ns)" if images else ""
        say(f"{head}\n{body}{skip_note}{img_note}\n🔗 <{card.get('url')}|Abrir card>", thread_ts=thread_ts)
    except Exception as e:
        logger.exception("process_task_request error")
        say(f"❌ Erro: {e}", thread_ts=thread_ts)

@app_slack.event("app_mention")
def handle_mention(body, say, client):
    event = body.get("event", {})
    if event.get("bot_id") or already_processed(body.get("event_id")): return
    threading.Thread(target=process_task_request, args=(client, say, event.get("channel"),
        event.get("thread_ts") or event.get("ts"), event.get("text", ""), event.get("user")), daemon=True).start()

@app_slack.event("message")
def handle_dm(body, say, client):
    event = body.get("event", {})
    if event.get("channel_type") != "im": return
    subtype = event.get("subtype")
    if subtype and subtype not in ("file_share",): return
    if event.get("bot_id") or already_processed(body.get("event_id")): return
    threading.Thread(target=process_task_request, args=(client, say, event.get("channel"),
        event.get("thread_ts") or event.get("ts"), event.get("text", ""), event.get("user")), daemon=True).start()

@app_slack.command("/tarefas")
def handle_tarefas(ack, say, command):
    ack()
    user = get_user(command.get("user_id"))
    if not user_ready_for_tasks(user):
        say(login_message(command.get("user_id"))); return
    say(list_recent_tasks_for_user(user))

@app_slack.command("/primeiro-login")
def handle_primeiro_login(ack, say, command):
    ack()
    ensure_user(command.get("user_id"))
    say(login_message(command.get("user_id")))

@app_slack.command("/desconectar")
def handle_desconectar(ack, say, command):
    ack()
    delete_user(command.get("user_id"))
    say("✅ Conta desconectada. Use `/primeiro-login` para reconfigurar.")

def _atas_status_text(user):
    if not user: return "Você ainda não fez `/primeiro-login`."
    trello_ok = "✅" if user_ready_for_tasks(user) else "❌"
    google_ok = "✅" if user.get("google_refresh_token") else "❌"
    atas_ok = "🟢 ligadas" if user.get("atas_enabled") else "⚪ desligadas"
    last = user.get("atas_last_check_at")
    return (f"*Status ResumeAI*\n• Trello: {trello_ok}\n• Google: {google_ok} ({user.get('google_email') or '—'})\n"
            f"• Atas: {atas_ok}\n• Última verificação: {last.isoformat() if last else 'nunca'}\n\n"
            f"Comandos: `/atas-on` · `/atas-off` · `/atas`")

@app_slack.command("/atas-on")
def handle_atas_on(ack, say, command):
    ack()
    user = get_user(command.get("user_id"))
    if not user_ready_for_tasks(user):
        say("⚠️ Conecte o Trello. `/primeiro-login`"); return
    if not user.get("google_refresh_token"):
        say("⚠️ Conecte o Google. `/primeiro-login`"); return
    set_atas_enabled(command.get("user_id"), True)
    say("✅ *Atas ligadas.* Use `/atas-off` para desligar.")

@app_slack.command("/atas-off")
def handle_atas_off(ack, say, command):
    ack()
    set_atas_enabled(command.get("user_id"), False)
    say("⏸ *Atas desligadas.* Use `/atas-on` para reativar.")

@app_slack.command("/atas")
def handle_atas_status(ack, say, command):
    ack()
    say(_atas_status_text(get_user(command.get("user_id"))))

PAGE_CSS = "body{font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;padding:0 16px}.card{border:1px solid #e5e5e5;border-radius:12px;padding:24px}a.btn,button.btn{display:inline-block;background:#0079BF;color:#fff;text-decoration:none;padding:12px 20px;border-radius:8px;border:none;cursor:pointer;font-weight:600;margin-top:12px}select{width:100%;padding:12px;font-size:1rem;border-radius:8px;border:1px solid #ccc}"
LOGIN_HOME = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><title>{{ app_name }}</title><style>{{ css }}</style></head><body><div class="card"><h1>Configurar {{ app_name }}</h1>
<p>Trello ✓={{ trello_ok }} Google ✓={{ google_ok }} Lista ✓={{ list_ok }}</p>
{% if not trello_ok %}<a class="btn" href="{{ trello_url }}">Autorizar Trello</a>
{% elif not google_ok %}<a class="btn" href="{{ google_url }}">Autorizar Google</a><p><a href="{{ skip_google_url }}">Pular</a></p>
{% elif not list_ok %}<a class="btn" href="{{ list_url }}">Escolher lista</a>
{% else %}<p><strong>Pronto!</strong> Use <code>/atas-on</code>.</p>{% endif %}</div></body></html>"""
TRELLO_CONNECT_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>{{ css }}</style></head><body><div class="card"><h1>Trello</h1><a class="btn" href="{{ auth_url }}">Autorizar</a></div></body></html>"""
TRELLO_CALLBACK_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>{{ css }}</style></head><body><div class="card"><p id="msg">Finalizando…</p></div>
<script>(function(){var p=new URLSearchParams((location.hash||'').replace(/^#/,''));var t=p.get('token');var u='{{ slack_user_id }}';
if(!t){document.getElementById('msg').textContent='Cancelado';return;}
fetch('/trello/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slack_user_id:u,token:t})})
.then(r=>r.json()).then(d=>{if(d.redirect)location=d.redirect;else document.getElementById('msg').textContent=d.error||'Erro';});})();</script></body></html>"""
GOOGLE_CONNECT_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>{{ css }}</style></head><body><div class="card"><h1>Google</h1>
{% if oauth_ready %}<a class="btn" href="{{ auth_url }}">Autorizar Google</a>{% else %}<p>OAuth não configurado</p><a class="btn" href="{{ skip_url }}">Continuar sem Google</a>{% endif %}
<p><a href="{{ skip_url }}">Pular</a></p></div></body></html>"""
SELECT_LIST_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>{{ css }}</style></head><body><div class="card"><h1>Lista do Trello</h1>
<form method="POST" action="/trello/select-list"><input type="hidden" name="slack_user_id" value="{{ slack_user_id }}">
<select name="list_id" required>{% for opt in options %}<option value="{{ opt.value }}"{% if opt.selected %} selected{% endif %}>{{ opt.label }}</option>{% endfor %}</select>
<button class="btn" type="submit">Salvar</button></form></div></body></html>"""
SUCCESS_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>{{ css }}</style></head><body><div class="card" style="text-align:center"><h1>✅ Pronto!</h1><p>Volte ao Slack. <code>/atas-on</code></p></div></body></html>"""

@app_flask.route("/")
def health_check():
    return f"✅ {APP_NAME} está online!", 200

@app_flask.route("/login")
def login_home():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id: return "slack_user_id obrigatório", 400
    ensure_user(slack_user_id)
    user = get_user(slack_user_id) or {}
    return render_template_string(LOGIN_HOME, app_name=APP_NAME, css=PAGE_CSS,
        trello_ok=bool(user.get("trello_token")), google_ok=bool(user.get("google_refresh_token")),
        list_ok=bool(user.get("trello_list_id")),
        trello_url=f"/trello/connect?slack_user_id={quote(slack_user_id)}",
        google_url=f"/google/connect?slack_user_id={quote(slack_user_id)}",
        list_url=f"/trello/select-list?slack_user_id={quote(slack_user_id)}",
        skip_google_url=f"/google/skip?slack_user_id={quote(slack_user_id)}")

@app_flask.route("/trello/connect")
def trello_connect():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id: return "slack_user_id obrigatório", 400
    return_url = f"{BASE_URL}/trello/callback?slack_user_id={quote(slack_user_id)}"
    auth_url = ("https://trello.com/1/authorize?expiration=never&scope=read,write&response_type=token"
                f"&name={quote(APP_NAME)}&key={TRELLO_API_KEY}&return_url={quote(return_url)}&callback_method=fragment")
    return render_template_string(TRELLO_CONNECT_PAGE, css=PAGE_CSS, auth_url=auth_url)

@app_flask.route("/trello/callback")
def trello_callback():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id: return "slack_user_id obrigatório", 400
    return render_template_string(TRELLO_CALLBACK_PAGE, css=PAGE_CSS, slack_user_id=slack_user_id)

@app_flask.route("/trello/save", methods=["POST"])
def trello_save():
    data = request.get_json(force=True, silent=True) or {}
    slack_user_id, token = (data.get("slack_user_id") or "").strip(), (data.get("token") or "").strip()
    if not slack_user_id or not token: return {"error": "dados obrigatórios"}, 400
    try:
        member = trello_member(token)
        save_trello(slack_user_id, token, member.get("id"), member.get("fullName") or member.get("username"))
        user = get_user(slack_user_id)
        if user and user.get("google_refresh_token"):
            return {"redirect": f"/trello/select-list?slack_user_id={quote(slack_user_id)}"}
        return {"redirect": f"/google/connect?slack_user_id={quote(slack_user_id)}"}
    except Exception as e:
        logger.exception("trello_save")
        return {"error": str(e)}, 500

@app_flask.route("/google/connect")
def google_connect():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id: return "slack_user_id obrigatório", 400
    skip_url = f"/google/skip?slack_user_id={quote(slack_user_id)}"
    auth_url = "#"
    if GOOGLE_OAUTH_ENABLED:
        params = {"client_id": GOOGLE_CLIENT_ID, "redirect_uri": f"{BASE_URL}/google/callback",
                  "response_type": "code", "scope": GOOGLE_SCOPES, "access_type": "offline",
                  "prompt": "consent", "state": slack_user_id}
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return render_template_string(GOOGLE_CONNECT_PAGE, css=PAGE_CSS, oauth_ready=GOOGLE_OAUTH_ENABLED,
                                  auth_url=auth_url, skip_url=skip_url)

@app_flask.route("/google/callback")
def google_callback():
    code, slack_user_id, err = request.args.get("code", "").strip(), request.args.get("state", "").strip(), request.args.get("error")
    if err or not code or not slack_user_id: return f"Falha Google: {err or 'dados ausentes'}", 400
    if not GOOGLE_OAUTH_ENABLED: return "Google OAuth não configurado", 500
    try:
        token_resp = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": f"{BASE_URL}/google/callback", "grant_type": "authorization_code"}, timeout=20)
        token_resp.raise_for_status()
        tokens = token_resp.json()
        access, refresh = tokens.get("access_token"), tokens.get("refresh_token")
        expiry_dt = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + int(tokens.get("expires_in", 3600)), tz=timezone.utc)
        email = None
        try:
            ui = requests.get("https://www.googleapis.com/oauth2/v2/userinfo", headers={"Authorization": f"Bearer {access}"}, timeout=15)
            if ui.ok: email = ui.json().get("email")
        except Exception: pass
        save_google(slack_user_id, refresh, access, email, expiry_dt)
        return redirect(f"/trello/select-list?slack_user_id={quote(slack_user_id)}")
    except Exception as e:
        logger.exception("google_callback")
        return f"Erro Google: {e}", 500

@app_flask.route("/google/skip")
def google_skip():
    slack_user_id = request.args.get("slack_user_id", "").strip()
    if not slack_user_id: return "slack_user_id obrigatório", 400
    user = get_user(slack_user_id)
    if user and user.get("trello_list_id"): return redirect("/login/done")
    return redirect(f"/trello/select-list?slack_user_id={quote(slack_user_id)}")

@app_flask.route("/login/done")
def login_done():
    return render_template_string(SUCCESS_PAGE, css=PAGE_CSS)

@app_flask.route("/trello/select-list", methods=["GET", "POST"])
def trello_select_list():
    if request.method == "POST":
        slack_user_id = request.form.get("slack_user_id", "").strip()
        raw = request.form.get("list_id", "")
        if not slack_user_id or "|" not in raw: return "Dados inválidos", 400
        list_id, board_id = raw.split("|", 1)
        update_user_list(slack_user_id, list_id, board_id)
        return render_template_string(SUCCESS_PAGE, css=PAGE_CSS)
    slack_user_id = request.args.get("slack_user_id", "").strip()
    user = get_user(slack_user_id)
    if not user or not user.get("trello_token"): return "Sem Trello. /primeiro-login", 404
    try:
        options, preferred = [], None
        for b in trello_boards(user["trello_token"]):
            if b.get("closed"): continue
            for lst in trello_lists(user["trello_token"], b["id"]):
                if lst.get("closed"): continue
                opt = {"label": f"{b['name']} → {lst['name']}", "value": f"{lst['id']}|{b['id']}", "selected": False}
                if preferred is None and ("resumeai" in (b.get("name") or "").lower() or (lst.get("name") or "").lower() in ("a fazer","to do","todo","inbox")):
                    preferred = len(options)
                options.append(opt)
        if not options: return "Nenhum quadro/lista aberto.", 400
        options[preferred if preferred is not None else 0]["selected"] = True
        return render_template_string(SELECT_LIST_PAGE, css=PAGE_CSS, slack_user_id=slack_user_id, options=options)
    except Exception as e:
        logger.exception("select-list")
        return f"Erro: {e}", 500

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: SocketModeHandler(app_slack, SLACK_APP_TOKEN).start(), daemon=True).start()
    threading.Thread(target=atas_worker_loop, daemon=True).start()
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)
