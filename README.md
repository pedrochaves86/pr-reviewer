# PR Auto-Reviewer 🤖

Bot que processa URLs de Pull Request GitHub e submete reviews automáticas com análise de código via `gh models`.

---

## Arquitetura

```
GITHUB_PR_URL (.env)
     │
     ▼
  PRProcessor
     │
  ┌────┼───────┐
  ▼    ▼       ▼
GitHub Sonar Checkmarx
Client Client Client
  │
  ▼
AIAnalyzer (GitHub Models)
  │
  ▼
GitHub PR Review
```

---

## Pré-requisitos

- Python 3.11+
- Token GitHub com acesso a `repo` e `pull_requests`
- GitHub CLI (`gh`) instalado
- Extensão `github/gh-models` instalada e autenticada com `gh auth login`

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

# 4. Instalar e autenticar GitHub CLI
gh extension install github/gh-models
gh auth login

# 5. Configurar variáveis de ambiente
cp .env.example .env
nano .env   # preenche todos os campos
```

---

## Configuração GitHub

1. [Gera um Personal Access Token](https://github.com/settings/tokens/new)
   - Scopes: `repo` (inclui `pull_requests:write`)
2. Copia para `GITHUB_TOKEN` no `.env`
3. Define `GITHUB_REVIEWER_LOGIN` com o teu username

### Modelo de IA (`gh models`)

- O projeto usa a extensão `gh models` para falar com GitHub Models através do GitHub CLI
- Modelo default configurado: `openai/gpt-4.1`
- Variáveis relevantes no `.env`:
   - `GITHUB_MODELS_MODEL` (ex.: `openai/gpt-4.1`, `openai/gpt-4o-mini`)
   - `GITHUB_MODELS_ORG` (opcional; atribui consumo a uma organização)

Antes de correr o bot, confirma que o CLI está operacional:

```bash
gh models list
```

> ⚠️ A review aparecerá como sendo feita por ti. O token tem de pertencer à tua conta.

---

## Execução

### Manual (teste)
```bash
python main.py
```

### Modo Direto
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

---

## Sonar & Checkmarx

Se tiveres acesso via API, preenche as variáveis no `.env`. O bot tentará:
- **SonarQube**: inferir o `project_key` como `owner_repo` — ajusta em `sonar_client.py → derive_project_key()` se necessário
- **Checkmarx**: com `CHECKMARX_URL` e `CHECKMARX_TENANT`, o projeto regista o contexto do tenant, mas o login SSO da extensão VS Code não é reutilizado automaticamente por este processo Python

Se as APIs não estiverem disponíveis, o modelo de GitHub Models usado via `gh models` analisa os padrões de Sonar/Checkmarx diretamente no diff (qualidade de código, vulnerabilidades comuns, secrets expostos, etc.).

---

## Personalização

| Ficheiro | O que podes ajustar |
|---|---|
| `core/ai_analyzer.py` | Prompt do sistema, temperatura, modelo |
| `core/review_formatter.py` | Formato Markdown da review |
| `config/settings.py` | Novos parâmetros de configuração |
| `integrations/sonar_client.py` | Lógica de project key, filtros de issues |
| `integrations/checkmarx_client.py` | Versão da API CxSAST/CxOne |
