def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                slack_user_id TEXT PRIMARY KEY, slack_name TEXT, trello_token TEXT,
                trello_member_id TEXT, trello_list_id TEXT, trello_board_id TEXT,
                google_email TEXT, google_refresh_token TEXT, google_access_token TEXT,
                google_token_expiry TIMESTAMPTZ, atas_enabled BOOLEAN DEFAULT FALSE,
                atas_last_check_at TIMESTAMPTZ, atas_last_doc_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW());""")
            for col, typedef in [("google_email","TEXT"),("google_refresh_token","TEXT"),("google_access_token","TEXT"),
                ("google_token_expiry","TIMESTAMPTZ"),("atas_enabled","BOOLEAN DEFAULT FALSE"),
                ("atas_last_check_at","TIMESTAMPTZ"),("atas_last_doc_id","TEXT")]:
                cur.execute(f"DO $$ BEGIN ALTER TABLE users ADD COLUMN {col} {typedef}; EXCEPTION WHEN duplicate_column THEN NULL; END $$;")
            cur.execute("""CREATE TABLE IF NOT EXISTS processed_atas (
                slack_user_id TEXT NOT NULL, gmail_msg_id TEXT NOT NULL, doc_id TEXT,
                meeting_title TEXT, tasks_created INT DEFAULT 0, processed_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (slack_user_id, gmail_msg_id));""")
            try:
                cur.execute("ALTER TABLE users ALTER COLUMN trello_token DROP NOT NULL")
            except Exception:
                conn.rollback()
        conn.commit()
    logger.info("Database initialized")

def get_user(slack_user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE slack_user_id = %s", (slack_user_id,))
            return cur.fetchone()

def ensure_user(slack_user_id, slack_name=None):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO users (slack_user_id, slack_name, updated_at) VALUES (%s, %s, NOW())
                ON CONFLICT (slack_user_id) DO UPDATE SET slack_name = COALESCE(EXCLUDED.slack_name, users.slack_name), updated_at = NOW()""",
                (slack_user_id, slack_name))
        conn.commit()

def save_trello(slack_user_id, trello_token, trello_member_id=None, slack_name=None):
    ensure_user(slack_user_id, slack_name)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE users SET trello_token=%s, trello_member_id=COALESCE(%s,trello_member_id),
                slack_name=COALESCE(%s,slack_name), updated_at=NOW() WHERE slack_user_id=%s""",
                (trello_token, trello_member_id, slack_name, slack_user_id))
        conn.commit()

def update_user_list(slack_user_id, list_id, board_id=None):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE users SET trello_list_id=%s, trello_board_id=COALESCE(%s,trello_board_id), updated_at=NOW() WHERE slack_user_id=%s""",
                (list_id, board_id, slack_user_id))
        conn.commit()

def save_google(slack_user_id, refresh_token=None, access_token=None, email=None, expiry=None):
    ensure_user(slack_user_id)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE users SET google_refresh_token=COALESCE(%s,google_refresh_token),
                google_access_token=COALESCE(%s,google_access_token), google_email=COALESCE(%s,google_email),
                google_token_expiry=COALESCE(%s,google_token_expiry), updated_at=NOW() WHERE slack_user_id=%s""",
                (refresh_token, access_token, email, expiry, slack_user_id))
        conn.commit()

def set_atas_enabled(slack_user_id, enabled):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET atas_enabled=%s, updated_at=NOW() WHERE slack_user_id=%s", (enabled, slack_user_id))
        conn.commit()

def list_users_with_atas_enabled():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT * FROM users WHERE atas_enabled=TRUE AND google_refresh_token IS NOT NULL
                AND trello_token IS NOT NULL AND trello_list_id IS NOT NULL""")
            return cur.fetchall()

def delete_user(slack_user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE slack_user_id=%s", (slack_user_id,))
            cur.execute("DELETE FROM processed_atas WHERE slack_user_id=%s", (slack_user_id,))
        conn.commit()

def is_ata_processed(slack_user_id, gmail_msg_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_atas WHERE slack_user_id=%s AND gmail_msg_id=%s", (slack_user_id, gmail_msg_id))
            return cur.fetchone() is not None

def mark_ata_processed(slack_user_id, gmail_msg_id, doc_id=None, meeting_title=None, tasks_created=0):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO processed_atas (slack_user_id,gmail_msg_id,doc_id,meeting_title,tasks_created)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (slack_user_id,gmail_msg_id) DO NOTHING""",
                (slack_user_id, gmail_msg_id, doc_id, meeting_title, tasks_created))
            cur.execute("UPDATE users SET atas_last_check_at=NOW(), atas_last_doc_id=COALESCE(%s,atas_last_doc_id), updated_at=NOW() WHERE slack_user_id=%s",
                (doc_id, slack_user_id))
        conn.commit()

def trello_get(path, token, params=None):
    p = {"key": TRELLO_API_KEY, "token": token}
    if params: p.update(params)
    r = requests.get(f"https://api.trello.com/1{path}", params=p, timeout=15)
    r.raise_for_status()
    return r.json()

def trello_post(path, token, params=None):
    p = {"key": TRELLO_API_KEY, "token": token}
    if params: p.update(params)
    r = requests.post(f"https://api.trello.com/1{path}", params=p, timeout=15)
    r.raise_for_status()
    return r.json()

def trello_member(token):
    return trello_get("/members/me", token)

def trello_boards(token):
    return trello_get("/members/me/boards", token, {"fields": "name,id,closed", "filter": "open"})

def trello_lists(token, board_id):
    return trello_get(f"/boards/{board_id}/lists", token, {"fields": "name,id,closed"})

def normalize_theme(s):
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def is_generic_theme(theme):
    n = normalize_theme(theme)
    if len(n) < 4: return True
    if n in GENERIC_THEMES: return True
    words = n.split()
    if len(words) == 1 and words[0] in GENERIC_THEMES: return True
    if words and all(w in GENERIC_THEMES for w in words): return True
    return False

def theme_match_score(theme, card_name):
    a, b = normalize_theme(theme), normalize_theme(card_name)
    if not a or not b: return 0.0
    if a == b: return 1.0
    if len(a) >= 6 and len(b) >= 6 and (a in b or b in a): return 0.92
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb: return 0.0
    inter = len(ta & tb)
    if inter == 0: return 0.0
    j = inter / len(ta | tb)
    if inter >= 2 and j >= 0.6: return j
    if inter == 1 and j >= 0.8 and len(next(iter(ta & tb))) >= 6: return j
    return 0.0

def find_matching_card(cards, theme):
    if is_generic_theme(theme):
        logger.info("[theme] tema genérico, não agrupa: %s", theme)
        return None
    best, best_score = None, 0.0
    for c in cards or []:
        if c.get("closed"): continue
        score = theme_match_score(theme, c.get("name") or "")
        if score > best_score:
            best, best_score = c, score
    if best and best_score >= THEME_MATCH_THRESHOLD:
        logger.info("[theme] match score=%.2f card=%s theme=%s", best_score, best.get("name"), theme)
        return best
    logger.info("[theme] sem match forte (best=%.2f) theme=%s", best_score, theme)
    return None

def list_open_cards_in_user_list(user):
    return trello_get(f"/lists/{user['trello_list_id']}/cards", user["trello_token"],
                      {"fields": "id,name,url,desc,closed", "limit": 100})

def get_or_create_checklist(token, card_id, name="Pendências"):
    for cl in trello_get(f"/cards/{card_id}/checklists", token) or []:
        if normalize_theme(cl.get("name") or "") == normalize_theme(name):
            return cl
    return trello_post(f"/cards/{card_id}/checklists", token, {"name": name})

def existing_checkitem_names(token, checklist_id):
    items = trello_get(f"/checklists/{checklist_id}/checkItems", token) or []
    return {normalize_theme(i.get("name") or "") for i in items}

def add_checkitem(token, checklist_id, name, due=None):
    params = {"name": name[:512], "checked": "false"}
    if due and str(due).lower() not in ("null", "none", ""):
        params["due"] = str(due)[:10]
    return trello_post(f"/checklists/{checklist_id}/checkItems", token, params)

def upsert_theme_card(user, theme, items, description="", source_link=None):
    theme = (theme or "Tarefa").strip()[:60]
    token = user["trello_token"]
    items = [i for i in (items or []) if (i.get("title") or "").strip()]
    if not items:
        raise ValueError("Nenhum item de tarefa para gravar")
    cards = list_open_cards_in_user_list(user)
    card = find_matching_card(cards, theme)
    created_new = False
    if not card:
        desc = description or f"Tema: {theme}"
        if source_link:
            desc += f"\n\n🔗 {source_link}"
        params = {"idList": user["trello_list_id"], "name": theme[:60], "desc": desc[:16384]}
        if user.get("trello_member_id"):
            params["idMembers"] = user["trello_member_id"]
        dues = [i.get("due_date") for i in items if i.get("due_date") and str(i.get("due_date")).lower() not in ("null","none","")]
        if dues:
            params["due"] = sorted(str(d)[:10] for d in dues)[0]
        card = trello_post("/cards", token, params)
        created_new = True
    elif source_link and source_link not in (card.get("desc") or ""):
        try:
            new_desc = ((card.get("desc") or "") + f"\n🔗 {source_link}").strip()
            requests.put(f"https://api.trello.com/1/cards/{card['id']}",
                         params={"key": TRELLO_API_KEY, "token": token, "desc": new_desc[:16384]}, timeout=15).raise_for_status()
        except Exception:
            logger.warning("[theme] não atualizou desc")
    checklist = get_or_create_checklist(token, card["id"], "Pendências")
    existing = existing_checkitem_names(token, checklist["id"])
    added, skipped = [], []
    for it in items:
        title = it["title"].strip()
        key = normalize_theme(title)
        if key in existing:
            skipped.append(title)
            continue
        add_checkitem(token, checklist["id"], title, due=it.get("due_date"))
        existing.add(key)
        added.append(title)
    if not card.get("url"):
        card = trello_get(f"/cards/{card['id']}", token, {"fields": "id,name,url"})
    return {"card": card, "created_new": created_new, "items_added": added, "items_skipped": skipped, "theme": theme}

def list_recent_tasks_for_user(user):
    if not user.get("trello_list_id"):
        return "⚠️ Você ainda não escolheu uma lista padrão. Use `/primeiro-login` novamente."
    try:
        cards = trello_get(f"/lists/{user['trello_list_id']}/cards", user["trello_token"], {"limit": 10})
        if not cards: return "Nenhuma tarefa pendente no momento."
        msg = "📋 *Últimas tarefas (cards):*\n"
        for c in cards:
            due = f" | 📅 {c.get('due')[:10]}" if c.get("due") else ""
            msg += f"• <{c['url']}|{c['name']}>{due}\n"
        return msg
    except Exception as e:
        logger.exception("list tasks error")
        return f"❌ Erro ao buscar tarefas: {e}"

def refresh_google_access_token(user):
    refresh = user.get("google_refresh_token")
    if not refresh or not GOOGLE_OAUTH_ENABLED: return None
    expiry, access = user.get("google_token_expiry"), user.get("google_access_token")
    if access and expiry:
        if getattr(expiry, "tzinfo", None) is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry > datetime.now(timezone.utc) + timedelta(seconds=60):
            return access
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=20)
        r.raise_for_status()
        data = r.json()
        access = data.get("access_token")
        expiry_dt = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600)))
        save_google(user["slack_user_id"], access_token=access, expiry=expiry_dt)
        return access
    except Exception:
        logger.exception("[atas] falha refresh token user=%s", user.get("slack_user_id"))
        return None

def gmail_headers(access_token):
    return {"Authorization": f"Bearer {access_token}"}

def gmail_list_ata_messages(access_token):
    q = ATAS_GMAIL_QUERY.format(days=ATAS_LOOKBACK_DAYS)
    logger.info("[atas] gmail query: %s", q)
    r = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                     headers=gmail_headers(access_token), params={"q": q, "maxResults": 15}, timeout=30)
    r.raise_for_status()
    return r.json().get("messages") or []

def gmail_get_message(access_token, msg_id):
    r = requests.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                     headers=gmail_headers(access_token), params={"format": "full"}, timeout=30)
    r.raise_for_status()
    return r.json()

def _walk_parts(payload, out_text):
    mime = (payload.get("mimeType") or "").lower()
    data = (payload.get("body") or {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        try:
            out_text.append(base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace"))
        except Exception:
            pass
    for part in payload.get("parts") or []:
        _walk_parts(part, out_text)

def extract_email_text_and_subject(msg):
    headers = {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers") or []}
    subject = headers.get("subject", "(sem assunto)")
    texts = []
    _walk_parts(msg.get("payload") or {}, texts)
    return subject, f"{subject}\n{msg.get('snippet') or ''}\n" + "\n".join(texts)

def find_doc_ids(text):
    return list(dict.fromkeys(DOC_ID_RE.findall(text or "")))

def docs_read_text(access_token, doc_id):
    r = requests.get(f"https://docs.googleapis.com/v1/documents/{doc_id}", headers=gmail_headers(access_token), timeout=30)
    r.raise_for_status()
    doc = r.json()
    title = doc.get("title") or "Reunião"
    chunks = []
    def walk(elements):
        for el in elements or []:
            if "paragraph" in el:
                for pe in el["paragraph"].get("elements") or []:
                    tr = pe.get("textRun")
                    if tr and tr.get("content"): chunks.append(tr["content"])
            if "table" in el:
                for row in el["table"].get("tableRows") or []:
                    for cell in row.get("tableCells") or []:
                        walk(cell.get("content"))
            if "tableOfContents" in el:
                walk(el["tableOfContents"].get("content"))
    walk(doc.get("body", {}).get("content"))
    return title, "".join(chunks).strip()

def analyze_ata_tasks(doc_text, meeting_title, person_name, person_email):
    return _analyze_ata_tasks(gemini_client, doc_text, meeting_title, person_name, person_email)

def notify_slack_dm(slack_user_id, text):
    try:
        opened = app_slack.client.conversations_open(users=slack_user_id)
        app_slack.client.chat_postMessage(channel=opened["channel"]["id"], text=text)
    except Exception:
        logger.exception("[atas] falha DM user=%s", slack_user_id)

def process_atas_for_user(user):
    uid = user["slack_user_id"]
    logger.info("[atas] processando user=%s email=%s", uid, user.get("google_email"))
    access = refresh_google_access_token(user)
    if not access:
        logger.warning("[atas] sem access_token user=%s", uid)
        return
    try:
        messages = gmail_list_ata_messages(access)
    except Exception:
        logger.exception("[atas] gmail list falhou user=%s", uid)
        return
    person_name, person_email = user.get("slack_name") or "", user.get("google_email") or ""
    for m in messages:
        msg_id = m.get("id")
        if not msg_id or is_ata_processed(uid, msg_id): continue
        try:
            full = gmail_get_message(access, msg_id)
            subject, body_text = extract_email_text_and_subject(full)
            doc_ids = find_doc_ids(body_text)
            if not doc_ids:
                mark_ata_processed(uid, msg_id, meeting_title=subject, tasks_created=0)
                continue
            doc_id = doc_ids[0]
            try:
                meeting_title, doc_text = docs_read_text(access, doc_id)
            except Exception:
                logger.exception("[atas] docs read falhou doc=%s", doc_id)
                mark_ata_processed(uid, msg_id, doc_id=doc_id, meeting_title=subject, tasks_created=0)
                continue
            if len(doc_text) < 40:
                mark_ata_processed(uid, msg_id, doc_id=doc_id, meeting_title=meeting_title, tasks_created=0)
                continue
            theme, meeting_title, tasks = analyze_ata_tasks(doc_text, meeting_title or subject, person_name, person_email)
            doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
            if not tasks:
                mark_ata_processed(uid, msg_id, doc_id=doc_id, meeting_title=meeting_title, tasks_created=0)
                continue
            result = upsert_theme_card(user, theme=theme or meeting_title,
                items=[{"title": t["title"], "due_date": t.get("due_date")} for t in tasks if t.get("title")],
                description=f"Ata: {meeting_title}", source_link=doc_url)
            n_added = len(result["items_added"])
            mark_ata_processed(uid, msg_id, doc_id=doc_id, meeting_title=meeting_title, tasks_created=n_added)
            card = result["card"]
            if n_added:
                verb = "Criei card e" if result["created_new"] else "Atualizei card e"
                lines = "\n".join(f"• {t}" for t in result["items_added"][:8])
                notify_slack_dm(uid, f"✅ {verb} adicionei *{n_added}* item(ns) em *{result['theme']}*\n{lines}\n🔗 <{card.get('url')}|Abrir card>\n📄 <{doc_url}|Abrir ata>")
            else:
                notify_slack_dm(uid, f"ℹ️ Ata *{meeting_title}* já tinha esses itens no card <{card.get('url')}|{result['theme']}>.")
        except Exception:
            logger.exception("[atas] erro msg=%s user=%s", msg_id, uid)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET atas_last_check_at=NOW(), updated_at=NOW() WHERE slack_user_id=%s", (uid,))
        conn.commit()
