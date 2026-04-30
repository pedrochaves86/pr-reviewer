# PR Auto-Reviewer 🤖

Bot que monitoriza um canal Microsoft Teams, deteta links de Pull Request GitHub e submete reviews automáticas com análise de código via Claude AI.

---

## Arquitetura

```
Canal Teams
    │ (Microsoft Graph API — polling)
    ▼
TeamsMonitor  ──deteta URLs de PR──►  PRProcessor
                                           │
                      ┌────────────────────┼────────────────────┐
                      ▼                    ▼                    ▼
                 GitHubClient         SonarClient        CheckmarxClient
                 (diff + info)        (issues API)        (scan results)
                      │                    │                    │
                      └────────────────────┴────────────────────┘
                                           │
                                           ▼
                                      AIAnalyzer
                                   (Claude Sonnet)
                                           │
                                           ▼
                                   ReviewFormatter
                                           │
                                           ▼
                                  GitHub PR Review
                              (em teu nome via API)
```

---

## Pré-requisitos

- Python 3.11+
- Conta Azure com permissões para registar uma App
- Token GitHub com acesso a `repo` e `pull_requests`
- API Key Anthropic

---

## Instalação

```bash
# 1. Clonar / extrair o projeto
cd pr-reviewer

# 2. Criar ambiente virtual
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows

# 3. Instalar dependências
pip install -r requirements.txt

# 4. Configurar variáveis de ambiente
cp .env.example .env
nano .env   # preenche todos os campos
```

---

## Configuração Azure (Teams)

> Se preferires executar sem Teams, usa `GITHUB_PR_URL` no `.env` e podes saltar esta secção.

O bot usa a **Microsoft Graph API** para ler mensagens de:
- canal de Team (`teams/{teamId}/channels/{channelId}`)
- chat (`chats/{chatId}`)

1. Vai ao [Azure Portal](https://portal.azure.com) → **App Registrations** → **New registration**
2. Nome: `pr-reviewer-bot` | Tipo: *Single tenant*
3. Em **API permissions**, adiciona:
   - `ChannelMessage.Read.All` (Application)
   - `Team.ReadBasic.All` (Application)
4. **Grant admin consent**
5. Em **Certificates & secrets** → **New client secret** — copia o valor
6. Copia **Application (client) ID** e **Directory (tenant) ID** para o `.env`

### Obter Team ID e Channel ID

```bash
# Instala az CLI ou usa o Graph Explorer
# https://developer.microsoft.com/graph/graph-explorer

GET https://graph.microsoft.com/v1.0/me/joinedTeams
GET https://graph.microsoft.com/v1.0/teams/{teamId}/channels
```

### Configurar modo Canal vs modo Chat

- Modo Canal:
   - `TEAMS_TEAM_ID=<GUID do Team>`
   - `TEAMS_CHANNEL_ID=<id do canal (ex.: 19:...@thread.tacv2)>`
- Modo Chat:
   - `TEAMS_TEAM_ID=` (vazio)
   - `TEAMS_CHANNEL_ID=<chat id (ex.: 19:...@thread.v2)>`
   - também podes usar o link completo do Teams chat em `TEAMS_CHANNEL_ID`

> Nota: para chat, o bot usa `GET /chats/{chatId}/messages`.

---

## Configuração GitHub

1. [Gera um Personal Access Token](https://github.com/settings/tokens/new)
   - Scopes: `repo` (inclui `pull_requests:write`)
2. Copia para `GITHUB_TOKEN` no `.env`
3. Define `GITHUB_REVIEWER_LOGIN` com o teu username

> ⚠️ A review aparecerá como sendo feita por ti. O token tem de pertencer à tua conta.

---

## Execução

### Manual (teste)
```bash
python main.py
```

### Modo Direto (sem Teams)
No `.env`, define:
```env
GITHUB_PR_URL=https://github.com/owner/repo/pull/123
```

Também podes indicar vários PRs separados por vírgula:
```env
GITHUB_PR_URL=https://github.com/owner/repo/pull/123,https://github.com/owner2/repo2/pull/45
```

Neste modo, o bot processa o(s) PR(s), submete review no GitHub e termina.

### Cron (recomendado)
```bash
# Abre o crontab
crontab -e

# Corre de 5 em 5 minutos
*/5 * * * * /caminho/para/pr-reviewer/.venv/bin/python /caminho/para/pr-reviewer/main.py >> /var/log/pr-reviewer.log 2>&1
```

> Se usares `POLL_INTERVAL_MINUTES=5` no `.env` **e** cron de 5 min, o script faz uma passagem por execução. Podes também usar `POLL_INTERVAL_MINUTES=1` com cron `@reboot` para execução contínua.

### Serviço systemd (alternativa ao cron)
```ini
# /etc/systemd/system/pr-reviewer.service
[Unit]
Description=PR Auto-Reviewer Bot
After=network.target

[Service]
Type=simple
User=teu_utilizador
WorkingDirectory=/caminho/para/pr-reviewer
ExecStart=/caminho/para/pr-reviewer/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable pr-reviewer
sudo systemctl start pr-reviewer
sudo journalctl -u pr-reviewer -f
```

---

## Logs

O bot escreve logs em:
- `stdout` (visível no terminal / cron / systemd)
- `pr_reviewer.log` (ficheiro rotativo)

PRs já processados são guardados em `processed_prs.json` para evitar reviews duplicadas.

---

## Sonar & Checkmarx

Se tiveres acesso via API, preenche as variáveis no `.env`. O bot tentará:
- **SonarQube**: inferir o `project_key` como `owner_repo` — ajusta em `sonar_client.py → derive_project_key()` se necessário
- **Checkmarx**: com `CHECKMARX_URL` e `CHECKMARX_TENANT`, o projeto regista o contexto do tenant, mas o login SSO da extensão VS Code não é reutilizado automaticamente por este processo Python

Se as APIs não estiverem disponíveis, Claude analisa os padrões de Sonar/Checkmarx diretamente no diff (qualidade de código, vulnerabilidades comuns, secrets expostos, etc.).

---

## Personalização

| Ficheiro | O que podes ajustar |
|---|---|
| `core/ai_analyzer.py` | Prompt do sistema, temperatura, modelo |
| `core/review_formatter.py` | Formato Markdown da review |
| `config/settings.py` | Novos parâmetros de configuração |
| `integrations/sonar_client.py` | Lógica de project key, filtros de issues |
| `integrations/checkmarx_client.py` | Versão da API CxSAST/CxOne |
