# ResumeAI — Slack + Gemini + Trello

Bot de produtividade para Slack que transforma mensagens e threads em **tarefas no Trello**, usando **Google Gemini** para extrair título, descrição, prazo e prioridade.

Suporta **uso multi-usuário** (cada pessoa conecta o próprio Trello) e é pensado para ser instalado por qualquer empresa.

---

## O que o bot faz

| Ação no Slack | Resultado |
|---|---|
| Marcar `@bot` em canal/thread | Lê o contexto e cria um card no **Trello do usuário** |
| Enviar mensagem no DM do bot | Cria tarefa a partir da anotação |
| `/tarefas` | Lista as últimas tarefas da lista padrão do usuário |
| `/conectar` | Inicia o fluxo OAuth do Trello (por usuário) |
| `/desconectar` | Remove as credenciais Trello daquele usuário |

Cada usuário autoriza o **próprio** Trello. O bot usa uma API Key central do Trello + Gemini da empresa, mas o **token e a lista** são individuais.

---

## Arquitetura

```
Slack (Socket Mode)
        │
        ▼
  ResumeAI (Flask + Bolt)
        │
        ├── Gemini API  (1 chave central da empresa)
        ├── PostgreSQL  (tokens Trello por slack_user_id)
        └── Trello API  (token + lista de cada usuário)
```

- **Slack**: 1 App instalado no workspace (Bot Token + App-Level Token).
- **Gemini**: 1 API Key (conta Google da empresa).
- **Trello**: 1 API Key da aplicação + **token por usuário** (OAuth simples).
- **PostgreSQL**: armazena `slack_user_id → trello_token / list_id / member_id`.

---

## Pré-requisitos

1. Conta Slack com permissão para criar apps
2. Conta Google (Gemini API) — preferencialmente da empresa
3. Conta Trello (Power-Up / API Key)
4. Conta no [Render](https://render.com) (ou similar) com PostgreSQL
5. Repositório Git (este)

---

## 1. Criar o Slack App

1. Acesse [https://api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch.
2. **Socket Mode** → Enable → crie um **App-Level Token** com scope `connections:write` → copie o `xapp-...` (`SLACK_APP_TOKEN`).
3. **OAuth & Permissions** → Bot Token Scopes:

   | Scope |
   |---|
   | `app_mentions:read` |
   | `channels:history` |
   | `groups:history` |
   | `im:history` |
   | `im:read` |
   | `im:write` |
   | `chat:write` |
   | `commands` |

4. Instale o app no workspace → copie o **Bot User OAuth Token** `xoxb-...` (`SLACK_BOT_TOKEN`).
5. **Event Subscriptions** → Enable → Subscribe to bot events:
   - `app_mention`
   - `message.im`
6. **App Home** → ative **Messages Tab** e a opção *Allow users to send slash commands and messages from the messages tab*.
7. **Slash Commands** → crie:

   | Command | Description |
   |---|---|
   | `/tarefas` | Lista as últimas tarefas do seu Trello |
   | `/conectar` | Conecta / reconecta seu Trello |
   | `/desconectar` | Remove a conexão com o Trello |

   (Com Socket Mode **não** é necessário Request URL.)

8. Reinstale o app no workspace após qualquer mudança de scope.

---

## 2. Gemini API Key

1. Acesse [Google AI Studio](https://aistudio.google.com) com a conta da empresa.
2. Crie uma API Key.
3. Modelo padrão usado: `gemini-3.6-flash` (configurável via `GEMINI_MODEL`).

---

## 3. Trello API Key (central)

1. Acesse [https://trello.com/power-ups/admin](https://trello.com/power-ups/admin) → New → crie um Power-Up (só para obter a chave).
2. Em **API Key** copie a **Chave de API** → `TRELLO_API_KEY`.
3. Em **Origens permitidas (Allowed origins)** adicione a URL pública do seu serviço, por exemplo:
   - `https://seu-servico.onrender.com`
   - `http://localhost:10000` (desenvolvimento)
4. O **Segredo** só é necessário se você implementar OAuth 1.0 completo. Nesta versão usamos o fluxo simples `response_type=token` + `callback_method=fragment`, então o segredo **não é obrigatório**.

> Cada usuário gera o **próprio token** no fluxo `/conectar`. A API Key é compartilhada pela aplicação.

---

## 4. Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `SLACK_BOT_TOKEN` | Sim | Token `xoxb-...` do bot |
| `SLACK_APP_TOKEN` | Sim | Token `xapp-...` (Socket Mode) |
| `GEMINI_API_KEY` | Sim | Chave do Google AI Studio |
| `TRELLO_API_KEY` | Sim | Chave da aplicação Trello |
| `DATABASE_URL` | Sim | URL do PostgreSQL (Render fornece automaticamente) |
| `BASE_URL` | Sim | URL pública do serviço, ex: `https://resumeai.onrender.com` |
| `PORT` | Não | Porta HTTP (Render injeta automaticamente) |
| `APP_NAME` | Não | Nome exibido nas páginas (padrão: `ResumeAI`) |
| `GEMINI_MODEL` | Não | Modelo Gemini (padrão: `gemini-3.6-flash`) |

### Exemplo (Render)

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
GEMINI_API_KEY=AIza...
TRELLO_API_KEY=bde9cf9b...
DATABASE_URL=postgresql://user:pass@host:5432/dbname
BASE_URL=https://resumeai-xxxx.onrender.com
APP_NAME=ResumeAI
GEMINI_MODEL=gemini-3.6-flash
```

> No Render, ao criar um **PostgreSQL** e vincular ao Web Service, a variável `DATABASE_URL` é preenchida automaticamente. O código já converte `postgres://` → `postgresql://`.

---

## 5. Deploy no Render

1. Crie um **PostgreSQL** (New → PostgreSQL).
2. Crie um **Web Service** a partir deste repositório.
3. Configurações:
   - **Runtime**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python app.py`
   - **Health Check Path**: `/`
4. Em **Environment**, adicione todas as variáveis da tabela acima.
5. Vincule o banco (ou copie o `DATABASE_URL` manualmente).
6. Deploy.

Após o primeiro boot, a tabela `users` é criada automaticamente.

### Manter o serviço acordado (plano free)

Se usar o plano gratuito do Render (que hiberna), configure um monitor no [UptimeRobot](https://uptimerobot.com) pingando `https://seu-servico.onrender.com/` a cada 5 minutos.

Para uso corporativo com 300 usuários, use plano **pago** (always-on).

---

## 6. Fluxo do usuário final

1. No Slack, digite `/conectar` (ou marque o bot sem estar conectado).
2. Clique no link **Conectar meu Trello**.
3. Autorize o app no Trello.
4. Escolha a **lista padrão** (ex.: “A fazer”).
5. Pronto. A partir daí:
   - Marque `@bot` em threads → card no Trello dele
   - Envie anotações no DM do bot → card no Trello dele
   - `/tarefas` → vê as pendências

---

## 7. Schema do banco

```sql
CREATE TABLE users (
    slack_user_id    TEXT PRIMARY KEY,
    slack_name       TEXT,
    trello_token     TEXT NOT NULL,
    trello_member_id TEXT,
    trello_list_id   TEXT,
    trello_board_id  TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);
```

A tabela é criada automaticamente no startup (`init_db()`).

---

## 8. Comandos Slack

| Comando | Descrição |
|---|---|
| `/conectar` | Gera o link de autorização do Trello para o usuário |
| `/desconectar` | Apaga o token/lista do usuário no banco |
| `/tarefas` | Lista até 10 cards da lista padrão do usuário |

---

## 9. Segurança e boas práticas

- **Tokens Trello** são sensíveis: fiquem apenas no PostgreSQL, nunca em logs.
- Use `BASE_URL` HTTPS em produção.
- Configure **Allowed origins** no Trello com o domínio real do app.
- Gemini: use conta da empresa e monitore rate limits (`gemini-3.6-flash` é adequado para volume médio).
- Slack: mantenha o bot apenas nos canais necessários; o código só processa `app_mention` e DMs (`im`).
- Para revogar: o usuário usa `/desconectar` ou revoga o app em [trello.com/u/me/account](https://trello.com/u/me/account).

---

## 10. Desenvolvimento local

```bash
git clone https://github.com/luixmoreira33/resumeaislack.git
cd resumeaislack
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export SLACK_BOT_TOKEN=...
export SLACK_APP_TOKEN=...
export GEMINI_API_KEY=...
export TRELLO_API_KEY=...
export DATABASE_URL=postgresql://user:pass@localhost:5432/resumeai
export BASE_URL=http://localhost:10000

python app.py
```

Para o redirect do Trello funcionar localmente, adicione `http://localhost:10000` nas **Allowed origins** da API Key do Trello.

---

## 11. Troubleshooting

| Sintoma | Causa provável | Solução |
|---|---|---|
| Bot não responde no DM | Messages Tab desativada | App Home → ativar Messages Tab + reinstall |
| `404 model not found` (Gemini) | Nome do modelo antigo | Ajuste `GEMINI_MODEL` (ex.: `gemini-3.6-flash`) |
| Redirect Trello bloqueado | Origin não permitida | Adicione `BASE_URL` em Allowed origins |
| “Texto muito curto” | Menção sem contexto | Envie texto com pelo menos ~10 caracteres úteis |
| Card não aparece | Lista errada / token revogado | `/desconectar` + `/conectar` de novo |
| Erro de banco no boot | `DATABASE_URL` inválida | Confira formato `postgresql://...` |

---


## Licença

Uso interno / livre para adaptação pela sua empresa. Ajuste conforme a política do seu time.

---

## Suporte

Abra uma issue neste repositório ou fale com o mantenedor do workspace Slack.
