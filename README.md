# English Activities & Leaderboard

Aplicação interna Streamlit para receber comprovações de atividades de inglês, executar OCR local, encaminhar ambiguidades para revisão e calcular o leaderboard a partir de um ledger imutável.

O MVP foi desenhado para cerca de 15 alunos em uma única máquina. Não usa API paga, IA generativa, Redis, Celery ou serviços externos de processamento. A sincronização de relatórios com Google Sheets é opcional.

## O que está incluído

- páginas públicas **Início** e **Entrar** em uma barra de navegação superior, sem menu lateral;
- login OIDC explícito com Google e allowlist de e-mails, iniciado somente pela página **Entrar**;
- modo demo local com escolha de identidade e clique explícito em **Entrar**, bloqueado em produção;
- depois do login, rotas permitidas pelo papel e a página **Minha conta** com identidade e logout;
- papéis `student` e `admin`, validados também na camada de serviço;
- catálogo configurável com pontuação histórica preservada;
- upload multi-imagem, validação do formato real, limites, EXIF e nomes UUID;
- OCR RapidOCR/ONNX local, carregado uma vez com `st.cache_resource`;
- SHA-256 para duplicata exata e ImageHash pHash para alerta visual;
- regras conservadoras para Duolingo/BeConfident e campos estruturais;
- fila administrativa, correção de unidades, aprovação e rejeição auditadas;
- uma unidade por conclusão; `combo` nunca é interpretado como número de lições;
- grupos únicos de cinco lições que geram 5 pontos;
- reunião em inglês confirmada por administrador;
- ledger, leaderboard geral/por período, CSV, XLSX e espelho opcional no Google Sheets;
- importação idempotente da planilha legada e relatório JSON;
- SQLite WAL, uploads persistentes, backup, Docker e testes offline.

## Materiais analisados

A cópia `inputs/aulas ingles 7565.xlsx` foi lida sem modificação. A aba `Página1` contém 15 alunos, 81 lançamentos diários e 585 pontos. O catálogo da planilha tem uma regra antiga de 15 pontos para Impact; o seed usa os 10 pontos definidos no briefing.

O arquivo citado originalmente em `/Users/luizc/Downloads/aulas ingles 7565.xlsx` e os quatro screenshots específicos com `combo x40`/`x51` não estavam acessíveis. O workspace contém 35 screenshots alternativos — 21 Duolingo e 14 BeConfident — que servem para testes representativos. Veja [docs/PRD.md](docs/PRD.md) e [docs/SPEC.md](docs/SPEC.md).

## Requisitos

- Python 3.11 ou 3.12 (RapidOCR legado não suporta Python 3.13+);
- Streamlit 1.50 ou superior;
- aproximadamente 2–4 CPUs, 8 GB de RAM e SSD;
- Docker + Docker Compose, se optar por containers.

O núcleo da aplicação, OCR e testes funcionam sem API externa. Somente login OIDC e a sincronização opcional com Google Sheets exigem internet.

## Execução local em modo demo

```bash
cp .env.example .env
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
pip install --no-build-isolation -e .
english-leaderboard init-db
streamlit run streamlit_app.py
```

Abra `http://localhost:8501`. O `.env.example` ativa o modo demo e cria um aluno e um administrador locais. A aplicação começa na área pública: abra **Entrar**, escolha uma identidade e clique explicitamente em **Entrar**. Apenas selecionar um usuário não inicia uma sessão. A identidade escolhida permanece na sessão até usar **Minha conta → Sair do modo demo**.

Nunca use o modo demo em produção; o startup recusa `APP_ENV=production` junto de `DEMO_AUTH_ENABLED=true`.

## Autenticação OIDC/Google

1. Configure no Google um cliente OIDC e autorize a URI `https://SEU_HOST/oauth2callback`.
2. Copie e preencha `.streamlit/secrets.toml.example`; nunca versione `secrets.toml`.
3. No `.env`, use `APP_ENV=production`, `DEMO_AUTH_ENABLED=false`, liste todos os e-mails em `ALLOWED_EMAILS` e os administradores em `ADMIN_EMAILS`.
4. Inicie atrás de HTTPS. Na área pública, abra **Entrar** e clique em **Entrar com Google**. O botão chama `st.login("google")`, e a identidade é lida de `st.user`.

Documentação oficial: [autenticação OIDC do Streamlit](https://docs.streamlit.io/develop/concepts/connections/authentication) e [`st.login`](https://docs.streamlit.io/develop/api-reference/user/st.login).

Alunos importados recebem inicialmente e-mails locais `@local.invalid`. Antes do primeiro login OIDC, o administrador deve editar esses e-mails na tela **Alunos** para os endereços autorizados reais.

## Navegação e telas móveis

- Sem autenticação, a barra superior mostra somente **Início** e **Entrar**.
- Depois da autenticação, ela mostra as páginas autorizadas para `student` ou `admin` e acrescenta **Minha conta**.
- **Minha conta** exibe nome, e-mail e papel e concentra a saída da sessão demo ou OIDC.
- Em viewport móvel de até `768px`, os layouts com várias colunas devem ser empilhados verticalmente.
- Botões, seletores e demais controles interativos devem ter área de toque com pelo menos `44px` de altura.
- Tabelas, imagens e formulários ocupam a largura disponível sem exigir zoom; a barra superior própria quebra seus botões em novas linhas e nunca vira uma gaveta lateral em telas estreitas.

Esses critérios devem ser conferidos tanto em `768px` quanto em uma largura menor representativa de celular antes de uma entrega de interface.

## Sincronização automática com Google Sheets

Os dados continuam tendo uma única fonte de verdade: SQLite e o ledger imutável. Quando habilitado, o Google Sheets recebe um espelho completo das abas `Leaderboard` e `Ledger` depois de cada alteração confirmada. Uma falha do Google gera um aviso, mas não desfaz submissões, aprovações, reuniões ou importações.

Foi usado Google **Sheets**, e não um documento de texto do Google Docs, porque leaderboard e ledger são dados tabulares.

1. No Google Cloud, habilite a Google Sheets API.
2. Crie uma planilha vazia e copie da URL apenas o ID entre `/d/` e `/edit`.
3. Crie uma conta de serviço dedicada. Se usar uma chave JSON local, salve-a em `secrets/google-service-account.json`, nunca no repositório, e restrinja o arquivo (`chmod 600`). Em infraestrutura Google, prefira Application Default Credentials/identidade da carga, sem chave persistente.
4. Compartilhe somente essa planilha, como **Editor**, com o `client_email` da conta de serviço.
5. Configure no `.env`:

```dotenv
GOOGLE_SHEETS_AUTO_SYNC=true
GOOGLE_SHEETS_SPREADSHEET_ID=ID_DA_PLANILHA
GOOGLE_SHEETS_LEADERBOARD_TAB=Leaderboard
GOOGLE_SHEETS_LEDGER_TAB=Ledger
GOOGLE_APPLICATION_CREDENTIALS=./secrets/google-service-account.json
```

6. Reinicie a aplicação. Na página administrativa **Ledger e exportações**, use **Sincronizar agora** para o primeiro espelho e **Abrir planilha** para conferir o resultado. Depois disso, alterações feitas pela aplicação são sincronizadas automaticamente.

A credencial OIDC do login e a conta de serviço do Sheets são coisas diferentes e não devem ser reutilizadas. O cliente solicita somente o escopo `spreadsheets`; não precisa de acesso geral ao Drive. Como permissões do Google são concedidas à planilha inteira e a aba `Ledger` contém histórico administrativo, mantenha essa planilha restrita aos administradores.

Para reconciliar manualmente ou por um agendador periódico:

```bash
english-leaderboard sync-google-sheets
```

O comando substitui o snapshot, cria as duas abas se necessário e não duplica linhas. É seguro executá-lo, por exemplo, a cada cinco minutos.

## Importar a planilha legada

Inicialize o banco e execute:

```bash
english-leaderboard import-xlsx "inputs/aulas ingles 7565.xlsx" \
  --namespace aulas_ingles_7565
```

Cada célula numérica com data vira `imported_daily_score`. `x` e vazio são ignorados. Totais por aluno servem apenas para reconciliação e não são somados outra vez. Uma segunda execução com o mesmo namespace deve importar zero e marcar os 81 registros como ignorados; conflitos são relatados sem alterar o ledger.

Se uma planilha possuir apenas colunas nome + total, o importador cria uma transação `initial_balance` por aluno. Cada execução grava um relatório em `import-reports/`, salvo se `--report` indicar outro caminho.

## Testes

```bash
source .venv/bin/activate
pytest
```

A suite padrão não usa rede. Testes com os screenshots do diretório `inputs/` são executados quando os arquivos estão presentes. Para rodar a inferência OCR real explicitamente:

```bash
RUN_OCR_TESTS=1 pytest -m ocr -vv
```

Também é possível inspecionar uma imagem manualmente:

```bash
english-leaderboard analyze-image \
  "inputs/WhatsApp Image 2026-08-13 at 23.21.52.jpeg"
```

## Streamlit Community Cloud

Ao criar a aplicação no Community Cloud, selecione **Python 3.12** em
**Advanced settings**. O arquivo `packages.txt` instala as bibliotecas nativas
de OpenCV/ONNX (`libgl1`, `libglib2.0-0t64` e `libgomp1`) exigidas pelo
RapidOCR no ambiente Linux. Para uma demo descartável, use
`APP_ENV=development` e `DEMO_AUTH_ENABLED=true` nos Secrets da aplicação.

O SQLite e os uploads locais não têm persistência garantida no Community Cloud;
para uso contínuo, prefira o Docker Compose com o volume `/data` descrito abaixo.

## Docker Compose

```bash
cp .env.example .env
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
mkdir -p backups
docker compose up --build -d
docker compose ps
curl --fail http://localhost:8501/_stcore/health
```

Com uma chave JSON para Google Sheets, use o overlay que monta a credencial como secret somente leitura:

```bash
mkdir -p secrets
chmod 700 secrets
chmod 600 secrets/google-service-account.json
docker compose -f docker-compose.yml -f docker-compose.google.yml up --build -d
```

Defina `GOOGLE_SERVICE_ACCOUNT_FILE` se o arquivo no host tiver outro caminho. O `.env` ainda precisa de `GOOGLE_SHEETS_AUTO_SYNC=true` e do ID da planilha; o overlay define o caminho correto da credencial dentro do container.

Para o primeiro teste local via Docker, mantenha `APP_ENV=development` e `DEMO_AUTH_ENABLED=true`. Para produção, configure OIDC/allowlist e desative o demo antes do `up`.

Volumes:

- `app_db` → `/data/db/app.db`;
- `app_uploads` → `/data/uploads`;
- `./backups` → `/backups`.

Recriar o container não apaga os volumes. `docker compose down -v` apaga os volumes e, portanto, é destrutivo.

## Implantação em VPS

1. Instale Docker Engine/Compose e copie apenas o projeto, `.env` e `secrets.toml` preenchidos.
2. Restrinja a porta 8501 ao host/rede privada; não a exponha diretamente à internet.
3. Prefira uma destas opções:
   - Tailscale/VPN, mantendo o serviço privado; ou
   - Caddy/Nginx como proxy reverso com certificado HTTPS e redirecionamento HTTP→HTTPS.
4. Configure DNS e a redirect URI OIDC com o hostname definitivo.
5. Rode `docker compose up --build -d` e valide `/_stcore/health`.
6. Agende backups, copie-os para outro disco/host e teste restauração.

O endpoint e o padrão de health check seguem a [documentação oficial do Streamlit para Docker](https://docs.streamlit.io/deploy/tutorials/docker). TLS deve terminar no proxy reverso ou VPN, não no servidor de desenvolvimento do Streamlit.

## Backup e restauração

Local:

```bash
english-leaderboard backup --destination backups
english-leaderboard verify-backup backups/manifest-AAAAMMDDTHHMMSSZ.json
```

Container:

```bash
docker compose exec app english-leaderboard backup --destination /backups
docker compose exec app english-leaderboard verify-backup /backups/manifest-AAAAMMDDTHHMMSSZ.json
```

O comando usa a API de backup online do SQLite, importante quando WAL está ativo, e cria snapshot do banco, `uploads-*.tar.gz` e manifesto com SHA-256.

Para restaurar:

1. valide o manifesto;
2. pare a aplicação com `docker compose stop app`;
3. preserve uma cópia dos volumes atuais;
4. substitua `/data/db/app.db` pelo snapshot e extraia o arquivo de uploads em `/data`;
5. confira dono/permissões e inicie novamente;
6. valide health check, ledger e algumas imagens.

Faça primeiro um ensaio em ambiente separado. Não restaure sobre uma instância ativa.

## Estrutura principal

```text
streamlit_app.py                 interface aluno/admin
english_leaderboard/models.py   modelo e restrições
english_leaderboard/services.py casos de uso e auditoria
english_leaderboard/rules.py    motor configurável/conservador
english_leaderboard/ocr.py      adaptador RapidOCR local
english_leaderboard/image_processing.py validação, OpenCV e hashes
english_leaderboard/scoring.py  ledger e grupos de cinco
english_leaderboard/importer.py importação idempotente
english_leaderboard/exporter.py downloads CSV/XLSX
english_leaderboard/google_sheets.py espelho idempotente via Sheets API
docs/                           PRD, SPEC e plano verificável
tests/                          suite offline
```

## Limitações conhecidas

- O login identifica o remetente, não prova a autoria da atividade mostrada no print.
- pHash encontra semelhança visual, mas telas legítimas do mesmo aplicativo são parecidas; por isso nunca rejeita sozinho.
- Regras simples não avaliam qualidade/veracidade de resumos; essas entregas vão para revisão.
- OCR pode falhar em imagens comprimidas ou textos pequenos; baixa confiança vai para revisão.
- Estados aprovados são terminais no MVP. Uma correção posterior deve usar transação compensatória auditada, ainda não exposta na UI.
- SQLite é adequado ao volume atual; uma futura migração pode reutilizar `DATABASE_URL`, mas exige migrações formais e testes no novo dialeto.
- Google Sheets é um espelho eventualmente consistente e administrativo; indisponibilidade externa não altera o ledger local, e o comando de reconciliação refaz o snapshot completo.
