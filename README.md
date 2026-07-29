# ResumeAI — Slack + Gemini + Trello (+ atas Google)

Bot de produtividade para Slack que transforma mensagens, threads e **atas de reunião (Google Docs)** em tarefas no **Trello**, usando **Gemini**.

Multi-usuário: cada pessoa conecta o próprio Trello e Google.

---

## Comandos Slack

| Comando | Descrição |
|---|---|
| `/primeiro-login` | Abre o navegador: Trello → Google → lista padrão |
| `/desconectar` | Remove Trello + Google + desliga atas |
| `/atas on` | Liga leitura automática de atas |
| `/atas off` | Desliga atas |
| `/atas` ou `/atas status` | Mostra status da conta |
| `/tarefas` | Lista as últimas tarefas no Trello |

---

## Fluxo do usuário

1. `/primeiro-login` → autoriza **Trello** e **Google** no navegador → escolhe lista  
2. `/atas on` → atas automáticas  
3. No dia a dia: marca o bot, manda DM, ou deixa as atas criarem tarefas sozinhas  

DM opcional futura: *“Criei 2 tarefas da reunião X”*.

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `SLACK_BOT_TOKEN` | Sim | `xoxb-...` |
| `SLACK_APP_TOKEN` | Sim | `xapp-...` |
| `GEMINI_API_KEY` | Sim | Google AI Studio |
| `TRELLO_API_KEY` | Sim | API Key do Power-Up |
| `DATABASE_URL` | Sim | PostgreSQL |
| `BASE_URL` | Sim | URL pública do serviço |
| `GOOGLE_CLIENT_ID` | Para atas | OAuth Google Cloud |
| `GOOGLE_CLIENT_SECRET` | Para atas | OAuth Google Cloud |
| `GEMINI_MODEL` | Não | Padrão `gemini-3.6-flash` |
| `ATAS_POLL_SECONDS` | Não | Intervalo do job (padrão 900) |
| `APP_NAME` | Não | Padrão `ResumeAI` |

Redirect Google OAuth: `{BASE_URL}/google/callback`

---

## Schema `users` (resumo)

- Trello: `trello_token`, `trello_member_id`, `trello_list_id`, `trello_board_id`
- Google: `google_email`, `google_refresh_token`, `google_access_token`, `google_token_expiry`
- Atas: `atas_enabled`, `atas_last_check_at`, `atas_last_doc_id`

Criado/migrado automaticamente no boot.

---

## Status da feature de atas

| Parte | Status |
|---|---|
| Tabela + flags | ✅ |
| `/primeiro-login` unificado | ✅ |
| OAuth Google (rotas) | ✅ (precisa Client ID/Secret) |
| `/atas on\|off\|status` | ✅ |
| Job periódico (esqueleto) | ✅ |
| Gmail + Docs + multi-tarefa | 🔜 próximo passo |

---

## Slack: scopes e comandos

Scopes sugeridos: `app_mentions:read`, `channels:history`, `groups:history`, `im:history`, `im:read`, `im:write`, `chat:write`, `commands`, `files:read`.

Slash commands: `/primeiro-login`, `/desconectar`, `/tarefas`, `/atas`.

---

## Deploy

Render: Web Service + PostgreSQL. Start: `python app.py`. Health: `/`.

---

## Licença

Uso interno / adaptação pela empresa.
