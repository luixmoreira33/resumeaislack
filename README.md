# ResumeAI

Bot de produtividade para **Slack** que transforma mensagens, threads, anexos e **atas de reunião do Google** em tarefas no **Trello**, usando **Gemini**.

Cada usuário conecta o **próprio** Trello e Google. Ideal para atividades **individuais** .

---

## O que faz

| Capacidade | Detalhe |
|---|---|
| Menção `@resumeai` | Lê a thread + anexos/imagens (OCR) e cria tarefas |
| DM com o bot | Canal de anotações pessoais → tarefas |
| Atas automáticas | E-mails de `gemini-notes@google.com` → lê o Google Doc → só tarefas **suas** |
| Agrupamento por tema | Mesmo projeto/reunião → **um card** com checklist *Pendências* |
| Multi-usuário | Credenciais por pessoa no PostgreSQL |

---

## Arquitetura

```
Slack (Socket Mode)
    │  @mention / DM / slash commands
    ▼
ResumeAI (Flask + Bolt)
    ├── Gemini  → extrai tema + ações
    ├── Trello  → card por tema + checklist
    ├── Gmail/Docs (OAuth) → job periódico de atas
    └── PostgreSQL → users + processed_atas
```

- **Socket Mode:** não precisa de endpoint público para eventos Slack (só para OAuth web).
- **Job de atas:** a cada `ATAS_POLL_SECONDS` (padrão 15 min) consulta Gmail de quem está com `/atas-on`.

---

## Comandos Slack

> Nomes **sem espaço** (limitação do Slack).

| Comando | Descrição |
|---|---|
| `/primeiro-login` | Fluxo web: Trello → Google → escolha da lista padrão |
| `/desconectar` | Remove Trello + Google e desliga atas |
| `/atas-on` | Liga leitura automática de atas |
| `/atas-off` | Desliga atas |
| `/atas` | Status (Trello / Google / atas / última verificação) |
| `/tarefas` | Últimos cards na lista padrão do Trello |

---

## Como o usuário final usa

### Preparação

1. Crie um **board no Trello** só para você (ex.: `ResumeAI` ou `Minhas tarefas`) com listas:
   - **A fazer**
   - **Em andamento**
   - **Concluído**
2. No Slack, abra o **chat direto** com o app ResumeAI.
3. Rode `/primeiro-login`, autorize Trello e (opcional) Google, escolha a lista **A fazer**.
4. Se quiser atas: `/atas-on`.

### No dia a dia

- Marque `@resumeai` em uma mensagem ou thread.
- Ou envie texto/anexo no **DM** do bot.
- Atas do Meet chegam por e-mail → o bot cria/atualiza o card e avisa no DM: *“Criei N item(ns) em Tema X”*.

### Onde funciona / não funciona

| Funciona | Não funciona |
|---|---|
| Canais públicos (bot incluído) | Canais sem o bot |
| Canais privados (bot incluído) | Conversas em grupo **já existentes** sem o bot |
| DM com o bot | Conversa **consigo mesmo** |
| Novas conversas com usuários **com o bot na criação** | — |

---

## Agrupamento por tema (checklist)

- O Gemini define um **tema específico** (ex.: `Kick-off SmartDev`).
- O bot busca cards **abertos na lista padrão**.
- Se o tema for parecido o bastante (threshold configurável), **reutiliza o card** e adiciona itens no checklist **Pendências**.
- Temas genéricos (`Reunião`, `Tarefa`, `Follow-up`, etc.) **nunca** agrupam — evita misturar assuntos.
- Itens já existentes no checklist não são duplicados.
- Prazos vão no item do checklist (e o menor prazo no card, se for card novo).

---

## Atas automáticas

1. Filtro Gmail (padrão):
   ```text
   newer_than:3d from:gemini-notes@google.com subject:Anotações
   ```
2. Extrai link do Google Docs no e-mail.
3. Lê o documento.
4. Gemini extrai só ações atribuídas à pessoa (nome/e-mail).
5. Cria/atualiza card no Trello + DM no Slack.
6. Registra o e-mail em `processed_atas` (não reprocessa).

Exemplos de assunto que batem:

- `Anotações: "Monthly CSIP" em 29 de jul. de 2026`
- `Anotações: "Kick-off SmartDev" em 20 de jul. de 2026`

---

## Variáveis de ambiente

Modelo completo: **`.env.example`**.

| Variável | Obrigatória | Descrição |
|---|---|---|
| `SLACK_BOT_TOKEN` | Sim | `xoxb-...` |
| `SLACK_APP_TOKEN` | Sim | `xapp-...` (Socket Mode) |
| `GEMINI_API_KEY` | Sim | Google AI Studio / Vertex |
| `TRELLO_API_KEY` | Sim | API Key do app Trello |
| `DATABASE_URL` | Sim | PostgreSQL (`postgresql://...`) |
| `BASE_URL` | Sim | URL pública **sem** `/` no final |
| `GOOGLE_CLIENT_ID` | Atas | OAuth Web (Google Cloud) |
| `GOOGLE_CLIENT_SECRET` | Atas | OAuth Web |
| `GEMINI_MODEL` | Não | Padrão conforme `.env.example` |
| `APP_NAME` | Não | Padrão `ResumeAI` |
| `PORT` | Não | Padrão `10000` |
| `ATAS_POLL_SECONDS` | Não | Padrão `900` (15 min) |
| `ATAS_LOOKBACK_DAYS` | Não | Padrão `3` |
| `ATAS_GMAIL_QUERY` | Não | Query Gmail (use `{days}`) |
| `THEME_MATCH_THRESHOLD` | Não | Padrão `0.85` |
| `MAX_IMAGES` | Não | Imagens por thread (padrão `5`) |

**Redirect OAuth Google (obrigatório no Console):**

```text
{BASE_URL}/google/callback
```

---

## Configuração do Slack App

1. [api.slack.com/apps](https://api.slack.com/apps) → criar app.
2. **Socket Mode** ligado → gerar `SLACK_APP_TOKEN` (`xapp-...`).
3. **OAuth & Permissions** — Bot Token Scopes sugeridos:
   - `app_mentions:read`
   - `channels:history`, `groups:history`
   - `im:history`, `im:read`, `im:write`
   - `chat:write`
   - `commands`
   - `files:read`
4. **Slash Commands:** `/primeiro-login`, `/desconectar`, `/tarefas`, `/atas-on`, `/atas-off`, `/atas`.
5. **Event Subscriptions** (Socket Mode): `app_mention`, `message.im`.
6. **App Home** → habilitar **Messages Tab** (DM com o bot).
7. Instalar no workspace → copiar `SLACK_BOT_TOKEN` (`xoxb-...`).

---

## Google Cloud (atas)

1. Projeto no [Google Cloud Console](https://console.cloud.google.com).
2. Ativar APIs: **Gmail**, **Google Docs**, **Google Drive**.
3. **OAuth consent screen** (Internal no Workspace, ou External + test users).
4. Scopes:
   - `gmail.readonly`
   - `documents.readonly`
   - `drive.readonly`
   - `openid`, `email`, `profile`
5. **Credentials** → OAuth Client ID → tipo **Web application**.
6. Authorized redirect URI: `https://SEU-DOMINIO/google/callback`.
7. Copiar Client ID e Secret → `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

---

## Docker (local / VPS)

### Pré-requisitos

- Docker + Docker Compose v2
- Tokens Slack, Trello, Gemini
- (Opcional) Google OAuth para atas

### Subir

```bash
cp .env.example .env
# edite .env

docker compose up -d --build
```

| Serviço | Porta | Função |
|---|---|---|
| `app` | `10000` | Flask + Slack + job de atas |
| `db` | `5432` | PostgreSQL 16 |

```bash
# health
curl http://localhost:10000/

# logs
docker compose logs -f app

# parar (mantém dados)
docker compose down

# parar e apagar volume do Postgres
docker compose down -v
```

O compose sobrescreve `DATABASE_URL` do app para o host `db`. Ajuste `POSTGRES_USER` / `PASSWORD` / `DB` no `.env` se quiser.

### Só a imagem

```bash
docker build -t resumeai .
docker run --rm -p 10000:10000 --env-file .env \
  -e DATABASE_URL=postgresql://user:pass@host:5432/resumeai \
  resumeai
```

### URL pública em desenvolvimento

OAuth no browser exige `BASE_URL` acessível na internet (ex.: [ngrok](https://ngrok.com)):

```bash
ngrok http 10000
# BASE_URL=https://xxxx.ngrok-free.app
# mesmo redirect no Google Cloud: .../google/callback
```

---

## Deploy no Render

### Opção A — Python nativo

1. **Web Service** ligado a este repositório.
2. **PostgreSQL** no Render → `DATABASE_URL`.
3. Start: `python app.py`.
4. Variáveis de `.env.example` (não precisa de `POSTGRES_*` do compose).
5. Health check path: `/`.

### Opção B — Docker

1. Runtime: **Docker**.
2. Dockerfile na raiz (já incluso).
3. Mesmas variáveis + banco gerenciado.

Mantenha o serviço acordado (plano pago ou [UptimeRobot](https://uptimerobot.com) no free) para o job de atas e o Socket Mode não hibernarem.

---

## Banco de dados

Criado/migrado no boot (`init_db`).

**users**

| Grupo | Campos |
|---|---|
| Slack | `slack_user_id`, `slack_name` |
| Trello | `trello_token`, `trello_member_id`, `trello_list_id`, `trello_board_id` |
| Google | `google_email`, `google_refresh_token`, `google_access_token`, `google_token_expiry` |
| Atas | `atas_enabled`, `atas_last_check_at`, `atas_last_doc_id` |

**processed_atas** — `(slack_user_id, gmail_msg_id)` para não reprocessar e-mails.

---

## Estrutura do repositório

```text
.
├── app.py                 # aplicação (Slack + Flask + job)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
└── README.md
```

---

## Segurança

- Token Trello e refresh token Google são **por usuário**, não compartilhados.
- Não versionar `.env`.
- Preferir OAuth consent **Internal** em Google Workspace corporativo.
- Rodar o container como usuário não-root (já configurado no Dockerfile).

---

## Troubleshooting

| Sintoma | O que checar |
|---|---|
| Bot não responde em canal | Bot foi **convidado** para o canal? |
| “Envio de mensagens desativado” no DM | App Home → Messages Tab |
| OAuth Google falha | `BASE_URL` e redirect URI idênticos |
| Atas não processam | `/atas` → Google ✅ e Atas 🟢; logs `[atas]`; `ATAS_POLL_SECONDS` |
| Cards no quadro errado | Refazer escolha de lista no `/primeiro-login` |
| Muitos cards do mesmo tema | `THEME_MATCH_THRESHOLD` (ex.: `0.9`) e nomes de card específicos |
| Render “dormindo” | Keep-alive / plano que não hiberna |

---

## Licença

Uso interno / adaptação pela empresa.
