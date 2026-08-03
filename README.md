# ResumeAI — Slack + Gemini + Trello (+ atas Google)

Bot de produtividade para Slack que transforma mensagens, threads e **atas de reunião (Google Docs)** em tarefas no **Trello**, usando **Gemini**.

Multi-usuário: cada pessoa conecta o próprio Trello e Google.

Tarefas do **mesmo tema** são agrupadas em **um card** com checklist (match conservador para evitar misturar assuntos).

---

## Comandos Slack

| Comando | Descrição |
|---|---|
| `/primeiro-login` | Abre o navegador: Trello → Google → lista padrão |
| `/desconectar` | Remove Trello + Google + desliga atas |
| `/atas-on` | Liga leitura automática de atas |
| `/atas-off` | Desliga atas |
| `/atas` | Mostra status da conta |
| `/tarefas` | Lista as últimas tarefas no Trello |

---

## Fluxo do usuário

1. Abrir o **chat direto** com o app ResumeAI no Slack  
2. `/primeiro-login` → autoriza **Trello** e **Google** → escolhe lista  
3. `/atas-on` → atas automáticas (e-mails de `gemini-notes@google.com`)  
4. No dia a dia: `@resumeai` em canais (com o bot no canal), DM com o bot, ou atas sozinhas  

**Onde funciona:** canais públicos/privados (bot incluído), DM com o bot, novas conversas em que o bot entra na criação.  
**Onde não funciona:** canais sem o bot, DMs 1:1 já existentes sem o bot, conversa consigo mesmo.

---

## Variáveis de ambiente

Veja `.env.example` para a lista completa.

| Variável | Obrigatória | Descrição |
|---|---|---|
| `SLACK_BOT_TOKEN` | Sim | `xoxb-...` |
| `SLACK_APP_TOKEN` | Sim | `xapp-...` |
| `GEMINI_API_KEY` | Sim | Google AI Studio |
| `TRELLO_API_KEY` | Sim | API Key do app Trello |
| `DATABASE_URL` | Sim | PostgreSQL |
| `BASE_URL` | Sim | URL pública (sem `/` no final) |
| `GOOGLE_CLIENT_ID` | Para atas | OAuth Google Cloud |
| `GOOGLE_CLIENT_SECRET` | Para atas | OAuth Google Cloud |
| `GEMINI_MODEL` | Não | Modelo Gemini |
| `ATAS_POLL_SECONDS` | Não | Intervalo do job (padrão 900) |
| `THEME_MATCH_THRESHOLD` | Não | Match de tema no Trello (padrão 0.85) |

Redirect Google OAuth: `{BASE_URL}/google/callback`

---

## Docker (recomendado para local / VPS)

### Pré-requisitos

- Docker + Docker Compose v2  
- Conta Slack App, Trello API Key, Gemini API Key  
- (Opcional) Google Cloud OAuth para atas  

### Subir

```bash
cp .env.example .env
# edite .env com seus tokens

docker compose up -d --build
```

Serviços:

| Serviço | Porta padrão | Função |
|---|---|---|
| `app` | `10000` | Flask + Slack Socket Mode + job de atas |
| `db` | `5432` | PostgreSQL 16 |

Health check: `GET http://localhost:10000/`

Logs:

```bash
docker compose logs -f app
```

Parar:

```bash
docker compose down
# dados do Postgres ficam no volume resumeai_pgdata
# para apagar tudo: docker compose down -v
```

### Só a imagem (sem compose)

```bash
docker build -t resumeai .
docker run --rm -p 10000:10000 --env-file .env \
  -e DATABASE_URL=postgresql://user:pass@host:5432/resumeai \
  resumeai
```

### URL pública em local

O Slack (OAuth Trello/Google no browser) precisa de `BASE_URL` acessível na internet.

Exemplo com [ngrok](https://ngrok.com):

```bash
ngrok http 10000
# coloque a URL https://....ngrok-free.app em BASE_URL no .env
# e o mesmo path /google/callback no Google Cloud Console
docker compose up -d
```

---

## Deploy no Render (sem Docker)

1. Web Service apontando para este repo  
2. PostgreSQL no Render → `DATABASE_URL`  
3. Start command: `python app.py`  
4. Variáveis do `.env.example` (exceto `POSTGRES_*` do compose)  
5. Health: `/`  

Ou use **Docker** no Render: selecione o Dockerfile na raiz.

---

## Slack: scopes e comandos

Scopes sugeridos: `app_mentions:read`, `channels:history`, `groups:history`, `im:history`, `im:read`, `im:write`, `chat:write`, `commands`, `files:read`.

Slash commands (nomes **sem espaço**): `/primeiro-login`, `/desconectar`, `/tarefas`, `/atas-on`, `/atas-off`, `/atas`.

App Home → Messages Tab habilitada (para DM com o bot).

---

## Schema (resumo)

**users:** credenciais Trello + Google + `atas_enabled`  
**processed_atas:** evita reprocessar o mesmo e-mail de ata  

Criados/migrados automaticamente no boot.

---

## Licença

Uso interno / adaptação pela empresa.
