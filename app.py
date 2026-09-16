import os
import re
import json
import logging
import threading
import time
import base64
import unicodedata
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlencode

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, redirect, render_template_string
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google import genai
from google.genai import types

from gemini_extract import analyze_ata_tasks as _analyze_ata_tasks
from gemini_extract import analyze_with_gemini as _analyze_with_gemini

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("resumeai-bot")

REQUIRED = [
    "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GEMINI_API_KEY",
    "TRELLO_API_KEY", "DATABASE_URL", "BASE_URL",
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
MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "5"))
ATAS_POLL_SECONDS = int(os.environ.get("ATAS_POLL_SECONDS", "900"))
ATAS_LOOKBACK_DAYS = int(os.environ.get("ATAS_LOOKBACK_DAYS", "3"))
THEME_MATCH_THRESHOLD = float(os.environ.get("THEME_MATCH_THRESHOLD", "0.85"))

ATAS_GMAIL_QUERY = os.environ.get(
    "ATAS_GMAIL_QUERY",
    "newer_than:{days}d from:gemini-notes@google.com subject:Anotações",
)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
GOOGLE_SCOPES = " ".join([
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "openid", "email", "profile",
])

DOC_ID_RE = re.compile(r"https?://docs\.google\.com/document/d/([a-zA-Z0-9_-]+)", re.IGNORECASE)

GENERIC_THEMES = {
    "reuniao", "reunião", "meeting", "follow-up", "follow up", "followup",
    "tarefa", "tarefas", "pendencia", "pendência", "pendencias", "pendências",
    "geral", "outros", "misc", "task", "tasks", "todo", "to do",
    "anotacao", "anotação", "anotacoes", "anotações", "notas", "notes",
    "acao", "ação", "acoes", "ações", "lembrete", "urgente", "importante",
    "trabalho", "demanda", "item", "itens",
}

app_slack = App(token=SLACK_BOT_TOKEN)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
app_flask = Flask(__name__)
_BOT_USER_ID = None
_PROCESSED = set()
_PROCESSED_LOCK = threading.Lock()
IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/heic", "image/heif"}

import runpy
runpy.run_path("runtime_a.py", init_globals=globals(), run_name="runtime_a")
runpy.run_path("runtime_b.py", init_globals=globals(), run_name=__name__)
