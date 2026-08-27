# Arquitetura oficial

**Status:** decisão técnica oficial, vigente desde 26 de agosto de 2026.

Este documento substitui qualquer proposta anterior de guardar dados de usuários
no GitHub, no filesystem permanente do Streamlit ou como binários no banco.

## Visão geral

```text
                    GitHub
                       │
              código e assets fixos
                       │
                       ▼
                Streamlit Cloud
          ┌─────────────────────────┐
          │ UI · Python · OCR       │
          │ validação · orquestração│
          └────────────┬────────────┘
                       │
           ┌───────────┴───────────┐
           ▼                       ▼
   Supabase Auth/PostgreSQL   Supabase Storage
   identidade, sessões,       arquivos privados
   dados e metadados          e binários
```

## Responsabilidades

### GitHub

Armazena somente código-fonte, configurações sem credenciais, dependências,
documentação e assets fixos da interface. Imagens, PDFs, exports ou backups com
dados de alunos não podem ser versionados, nem mesmo em repositório privado.

### Streamlit Cloud

Executa a interface e a lógica Python, valida uploads, realiza OCR e conversa com
Supabase (Auth, PostgreSQL e Storage). Seu disco é efêmero. Código não pode depender da existência de um
arquivo local depois do fim da requisição, reinício, hibernação ou deploy.

Quando uma biblioteca exigir um path, usar arquivo/diretório temporário e limpeza
garantida com context manager ou `try/finally`. Sempre que possível, processar em
memória.

### Supabase Auth

É a única autoridade para cadastro, login, logout, sessão e recuperação de
acesso. A aplicação não guarda hashes de senha nem emite sessão própria.

O UUID de `auth.users.id` é o identificador estável. Uma tabela pública de perfil
pode usar o mesmo UUID:

```sql
create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  name text not null,
  class_name text,
  role text not null check (role in ('student', 'admin')),
  created_at timestamptz not null default now()
);
```

Papéis administrativos não devem ser aceitos a partir de campos editáveis pelo
cliente. Usar claim confiável ou tabela protegida por políticas administrativas.

### Supabase PostgreSQL

Armazena entidades estruturadas, ledger, progresso, histórico, resultados de OCR
e metadados de arquivo. Uma representação mínima de arquivo é:

```text
id: UUID do registro/arquivo
student_id: UUID igual ao usuário do Supabase Auth
filename: nome original para exibição
storage_provider: supabase
storage_bucket: student-files
storage_key: students/{student_id}/activities/{id}.jpg
content_type: image/jpeg
file_size: 1834021
checksum_sha256: ...
created_at: ...
ocr_text: ...
```

O banco não armazena o conteúdo binário, base64 nem URL assinada. A tripla
`storage_provider` + `storage_bucket` + `storage_key` tem restrição única. Tabelas pertencentes ao aluno precisam de RLS e índice no
campo de proprietário.

Política conceitual para leitura do próprio registro:

```sql
using (student_id = auth.uid())
```

Operações administrativas exigem uma política separada e papel confiável. RLS é
defesa adicional; a camada de serviço também valida o ator e o recurso.

### Supabase Storage

Bucket privado `student-files`. É o armazenamento permanente de imagens, PDFs, documentos, screenshots, arquivos
de OCR e resultados binários. Buckets são privados por padrão.

Estrutura recomendada:

```text
students/{student_id}/uploads/{file_id}.{ext}
students/{student_id}/activities/{file_id}.{ext}
students/{student_id}/documents/{file_id}.{ext}
students/{student_id}/processed/{file_id}.{ext}
```

O nome original permanece nos metadados, não na chave física. Isso evita colisão,
exposição de dados pessoais e dependência de nomes mutáveis.

Upload e download de aluno usam o **JWT dele**, para que as políticas de
`storage.objects` sejam avaliadas. A chave secreta do Supabase ignora RLS e
criaria objeto sem proprietário, por isso não participa dessas operações.
Movimentação e exclusão passam pelas APIs do Storage; registros do schema
`storage` nunca são alterados por SQL direto.

As políticas conferem o segundo segmento do path contra `auth.uid()`:

```sql
bucket_id = 'student-files'
and (storage.foldername(name))[1] = 'students'
and (storage.foldername(name))[2] = auth.uid()::text
```

## Upload transacional

```text
Aluno autenticado
  -> Streamlit obtém auth.uid()
  -> valida tipo, MIME, tamanho e limites
  -> processa/OCR temporariamente
  -> envia objeto privado ao Storage
  -> grava metadados e storage_key no Supabase
```

Uma falha após o upload ao Storage deve disparar exclusão compensatória. Se a exclusão
falhar, registrar uma pendência de reconciliação. Repetições devem usar um UUID ou
chave idempotente para não criar cópias desnecessárias.

## Leitura e download

```text
Streamlit valida a sessão
  -> consulta registro sob RLS
  -> confirma propriedade/permissão
  -> obtém storage_key
  -> baixa para processamento ou assina URL curta no Storage
```

Nunca aceitar uma `storage_key` arbitrária enviada pela interface. URLs assinadas devem
expirar rapidamente e só podem ser geradas após autorização.

## Configuração e segredos

Em produção, credenciais ficam nos Secrets do Streamlit ou em variáveis de
ambiente. Os nomes exatos serão consolidados durante a implementação, mas devem
cobrir:

- URL e chave pública/servidor apropriada do Supabase;
- bucket privado do Supabase Storage;
- endpoint, região e bucket do Storage.

Chaves de serviço do Supabase e credenciais do Supabase nunca são enviadas ao navegador,
registradas em log ou incluídas em mensagens de erro.

## Matriz de decisão

| Informação nova | Destino |
|---|---|
| Código, configuração de exemplo, documentação | GitHub |
| Perfil, turma, exercício, resultado, progresso, OCR textual | Supabase PostgreSQL |
| Cadastro, login, sessão e recuperação | Supabase Auth |
| Imagem, PDF, DOCX, screenshot ou export persistente | Supabase Storage |
| Buffer, renderização ou arquivo intermediário | Streamlit, temporariamente |

## Migração do legado

A implementação atual ainda contém SQLite, autenticação local, uploads persistidos
em disco e backup no GitHub. A nova arquitetura é o destino oficial, mas não deve
ser descrita como implementada até que os itens de migração em `TASKS.md` estejam
concluídos e testados. Dados existentes precisam de migração explícita e
verificável; não devem ser apagados nem enviados automaticamente a serviços
externos durante a mudança.

