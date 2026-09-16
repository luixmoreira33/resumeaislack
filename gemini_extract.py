"""Extração de tarefas com Gemini 3.5 Flash-Lite + Embedding 2."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from google.genai import types

logger = logging.getLogger("resumeai-bot")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_EMBEDDING_MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2")
ATA_MAX_CHARS = int(os.environ.get("ATA_MAX_CHARS", "24000"))
EMBED_TOP_K = int(os.environ.get("EMBED_TOP_K", "8"))


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed_texts(client, texts):
    if not texts:
        return []
    result = client.models.embed_content(
        model=GEMINI_EMBEDDING_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT", output_dimensionality=768),
    )
    out = []
    for emb in result.embeddings or []:
        vals = getattr(emb, "values", None)
        out.append(list(vals) if vals else [])
    return out


def _split_chunks(text, size=900):
    raw = [p.strip() for p in re.split(r"\n{2,}", text or "") if p.strip()]
    chunks, buf = [], ""
    for p in raw:
        if len(buf) + len(p) + 1 <= size:
            buf = f"{buf}\n{p}".strip() if buf else p
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    if not chunks and (text or "").strip():
        t = text.strip()
        chunks = [t[i:i + size] for i in range(0, len(t), size)]
    return chunks


def person_tokens(person_name, person_email):
    tokens = set()
    if person_name:
        n = person_name.strip()
        tokens.add(n.lower())
        parts = [p for p in re.split(r"\s+", n) if len(p) >= 3]
        tokens.update(p.lower() for p in parts)
    if person_email:
        local = person_email.split("@")[0]
        tokens.add(person_email.lower())
        tokens.add(local.lower())
        tokens.update(p.lower() for p in re.split(r"[._+\-]", local) if len(p) >= 3)
    return {t for t in tokens if t}


def select_ata_passages(client, doc_text, person_name, person_email):
    text = (doc_text or "").strip()
    if len(text) <= ATA_MAX_CHARS:
        return text
    chunks = _split_chunks(text)
    tokens = person_tokens(person_name, person_email)
    keyword_hits, others = [], []
    for ch in chunks:
        low = ch.lower()
        if tokens and any(t in low for t in tokens):
            keyword_hits.append(ch)
        else:
            others.append(ch)
    selected = list(keyword_hits)
    try:
        query = (
            f"Tarefas e ações atribuídas explicitamente a {person_name or 'o usuário'} "
            f"({person_email or 'sem e-mail'}). Responsável, dono da ação, assignee."
        )
        q_emb = _embed_texts(client, [query])
        pool = others or chunks
        if q_emb and pool:
            c_embs = _embed_texts(client, pool)
            scored = sorted(
                ((_cosine(q_emb[0], e), ch) for e, ch in zip(c_embs, pool)),
                reverse=True,
            )
            for score, ch in scored[:EMBED_TOP_K]:
                if ch not in selected and score >= 0.25:
                    selected.append(ch)
        logger.info(
            "[atas] embedding recorte: chunks=%s keyword=%s selected=%s",
            len(chunks), len(keyword_hits), len(selected),
        )
    except Exception:
        logger.exception("[atas] embedding falhou; usando menções")
        if not selected:
            selected = chunks[:EMBED_TOP_K]
    out = "\n\n".join(selected).strip()
    return (out[:ATA_MAX_CHARS] if out else text[:ATA_MAX_CHARS])


def parse_json_response(resp):
    raw = (resp.text or "").strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def gemini_json(client, system_instruction, user_parts):
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        temperature=0.1,
    )
    try:
        config.thinking_config = types.ThinkingConfig(thinking_budget=0)
    except Exception:
        pass
    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_parts,
        config=config,
    )


def filter_tasks_for_person(tasks, person_name, person_email):
    tokens = person_tokens(person_name, person_email)
    kept = []
    for t in tasks or []:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        assignee = (t.get("assignee") or t.get("owner") or "").strip().lower()
        if assignee and tokens and not any(tok in assignee for tok in tokens):
            if assignee not in ("eu", "mim", "usuario", "usuário", "user"):
                logger.info("[atas] descartou tarefa de outro: %s (%s)", title, assignee)
                continue
        kept.append(t)
    return kept


def analyze_ata_tasks(client, doc_text, meeting_title, person_name, person_email):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = (person_name or "").strip() or "usuário logado"
    email = (person_email or "").strip() or "n/a"
    focused = select_ata_passages(client, doc_text, name, email)
    system = (
        "Você é um extrator de tarefas RÍGIDO e AUTORITÁRIO.\n"
        "REGRA ABSOLUTA — leia duas vezes antes de responder:\n"
        f"ATENÇÃO: Extraia APENAS as tarefas atribuídas EXPLICITAMENTE a [{name}] "
        f"(e-mail [{email}]).\n"
        "Ignore COMPLETAMENTE tarefas de outras pessoas, mesmo que estejam na mesma lista, "
        "mesmo parágrafo ou mesma tabela.\n"
        "Não invente. Não atribua tarefa coletiva ao usuário, a menos que o texto diga "
        f"claramente que [{name}] é responsável.\n"
        f"Sinais válidos: o nome, o e-mail, 'você', ou 'responsável: {name}'.\n"
        "Se não houver tarefa explícita dessa pessoa, devolva tasks=[].\n"
        "Theme deve ser ESPECÍFICO (ex. Kick-off SmartDev), nunca genérico.\n"
        "Responda somente JSON."
    )
    user = (
        f"Pessoa-alvo (ÚNICA): {name}\nE-mail-alvo: {email}\n"
        f"Reunião: {meeting_title}\nHoje: {today}\n\n"
        "Trechos da ata já filtrados por relevância a essa pessoa:\n"
        f'\"\"\"{focused}\"\"\"\n\n'
        'JSON: {"theme":"...","meeting_title":"...","tasks":'
        '[{"title":"...","assignee":"nome","due_date":"YYYY-MM-DD ou null"}]}'
    )
    resp = gemini_json(client, system, user)
    data = parse_json_response(resp)
    tasks = filter_tasks_for_person(data.get("tasks") or [], name, email)
    theme = (data.get("theme") or data.get("meeting_title") or meeting_title or "Reunião").strip()
    return theme, data.get("meeting_title") or meeting_title, tasks


def analyze_with_gemini(client, text_content, images=None):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system = (
        "Você extrai tarefas do contexto Slack com precisão.\n"
        "Crie somente ações concretas pedidas ou assumidas pelo autor.\n"
        "Não invente demandas. Theme específico, nunca genérico."
    )
    prompt = (
        f"Hoje: {today}\nContexto:\n\"\"\"{text_content}\"\"\"\n"
        'JSON: {"theme":"...","description":"...","items":'
        '[{"title":"...","due_date":"YYYY-MM-DD ou null","priority":"alta|média|baixa"}]}'
    )
    parts = [types.Part.from_text(text=prompt)]
    for data, mime in images or []:
        try:
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        except Exception as e:
            logger.warning("imagem: %s", e)
    resp = gemini_json(client, system, parts)
    data = parse_json_response(resp)
    if "items" not in data and data.get("title"):
        data["items"] = [{
            "title": data["title"],
            "due_date": data.get("due_date"),
            "priority": data.get("priority"),
        }]
        data.setdefault("theme", data["title"][:60])
    if not data.get("items"):
        raise ValueError(f"Resposta sem items: {data}")
    data.setdefault("theme", (data["items"][0].get("title") or "Tarefa")[:60])
    data.setdefault("description", "")
    return data
