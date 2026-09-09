# O que já foi exercitado contra o ambiente real

**Atualizado:** 8 de setembro de 2026.

A suíte roda offline, em SQLite. Vários defeitos desta semana só apareceram em
produção — chave de coluna renomeada, regex de token, `.value` sobre StrEnum
vindo do banco, promoção de booleano do TOML, papel `authenticated` sem acesso
ao schema `auth`. Nenhum foi pego por teste, e nenhum poderia ter sido.

Por isso "passa na suíte" e "funciona em produção" são duas afirmações
diferentes, e esta tabela existe para não confundir as duas nem redescobrir a
resposta a cada conversa.

## Confirmado em produção

| Commit | O quê | Como se sabe |
|---|---|---|
| `77edf51` | `PUT` nas atualizações de conta no Auth | Redefinir senha do `enzo.souza` funcionou pela tela; desativar/reativar também usa `PUT` e funcionou |
| `437e2ff` | Renomear chega ao Auth | Sete `user_updated` na auditoria, `profiles.username` e `auth.users.email` em sincronia, e a operação segue sendo feita pela tela com o nome novo |
| `54bc859` | `reativar` levanta o ban | **Desativado: o login recusou. Reativado: o login voltou.** Exercitado no `enzo.souza`, que foi apagado depois na limpeza das contas de teste — a confirmação vale, a conta não existe mais |
| migração `0009` | `contas_fora_de_sincronia()` | Aplicada; responde 2 linhas sob claims de admin e 0 sob claims de aluno, pelo papel `authenticated` real |
| migração `0010` | `must_change_password` | Aplicada; os três perfis existentes ficaram `false`, lido pelo papel real |

## Ainda não exercitado

| Commit | O quê | Por quê |
|---|---|---|
| `8cdba07` | O aviso de divergência na aba Alunos | A consulta está validada; o banner nunca apareceu porque não há divergência — é o resultado certo, mas não prova o caminho de renderização |
| `a30f424` | Troca de senha e revogação de sessão no logout | Corrigido a partir do diagnóstico, mas ainda não exercitado pela tela |
| `24da0c0` | Guarda do `contas=None` ao renomear | Nenhum chamador atual omite `contas`; só dispara para código futuro |
| `3046389` | Troca obrigatória de senha | Deploy quebrado desde 7/set |
| `7d092ac` | Tolerância a motor de OCR ausente | Deploy quebrado |
| `convergencia-pipeline-etapa2` | Convergência dos dois fluxos de submissão | Não mesclada; o roteiro de quatro envios depende do app no ar |

## Defeitos que só a validação em produção encontrou

Vale a lista, porque ela é o argumento deste documento — nenhum destes seria
pego por teste, e todos foram achados usando o aplicativo:

- chave de coluna renomeada; regex de token de sessão; `.value` sobre StrEnum
  vinda do banco; promoção de booleano do TOML;
- perfil renomeado sem mover a conta no Auth, e reativação que não levantava o
  ban;
- o papel `authenticated` sem acesso ao schema `auth`, que derrubou a aba
  Alunos inteira;
- **o `apikey` levando o token do usuário** (`a30f424`), que impedia a troca de
  senha e fazia o logout nunca revogar sessão nenhuma.

## Estado das contas

Em 8/set/2026 as contas de teste foram apagadas pelo Supabase Auth — o cascade
de `profiles.id` levou os perfis. Restou só `luiz.brito`
(`e8e0b934-5c10-4fe6-9c32-3318eb2a8df1`), administrador.

Na mesma ocasião, todas as sessões e refresh tokens foram revogados: 18 sessões
e 34 tokens apagados de `auth.sessions`, `auth.refresh_tokens` e
`auth.mfa_amr_claims`. O motivo é o defeito acima — como o logout nunca
revogou nada, havia sessões vivas emitidas ao longo de semanas, e preservar
qualquer uma delas seria preservar exatamente o que a correção veio matar.

Não há endpoint administrativo de logout no GoTrue (`POST
admin/users/{id}/logout` responde `404 page not found`); a revogação foi por
SQL nas tabelas de sessão do schema `auth`, em transação conferida antes do
commit. É exceção consciente ao hábito de não tocar schema gerenciado por SQL,
e o schema `storage` continua fora de alcance, como o AGENTS.md exige.

## Defeito aberto

A exclusão de conta pela interface não funciona em produção, por causa
desconhecida. Ver [EXCLUSAO_DE_CONTA.md](EXCLUSAO_DE_CONTA.md).

## Regra: escrita sob o papel do aluno se verifica em produção

**Qualquer operação que escreva em tabela com RLS, feita sob o papel do aluno,
precisa ser exercitada contra o PostgreSQL antes de ser considerada pronta. A
suíte nunca vai pegar.**

O motivo é estrutural, não descuido: a suíte roda em SQLite, que não tem RLS.
Toda escrita passa lá. Em produção a mesma escrita corre como `authenticated`,
sob políticas, e pode ser recusada.

Já mordeu três vezes:

1. o papel `authenticated` sem acesso ao schema `auth`, que derrubou a aba
   Alunos inteira;
2. `insert into duplicate_matches` sem política para aluno — que só não
   quebrou porque a conexão do app é dona e ignora RLS;
3. `concluir_troca_de_senha` inserindo em `audit_logs`, que tem política só de
   administrador. O insert era recusado, a transação caía, e o rollback levava
   junto a limpeza da marca — o aluno trocava a senha e continuava trancado.

A saída usada nos casos 1 e 3 é a mesma: função `SECURITY DEFINER` sem
parâmetro, agindo sobre `auth.uid()`, com `revoke` de `public` e `grant` a
`authenticated`. O aluno não ganha permissão; quem escreve é o banco.

**Ao verificar, cuidado com o que a leitura devolve.** Sob RLS, o que você não
pode ver aparece como zero linhas ou `NULL` — não como erro. Contar
`audit_logs` sob o papel do aluno devolve `0` mesmo com as linhas gravadas, e
ler o perfil alheio devolve `None` mesmo com ele intacto. Faça a escrita sob o
papel do aluno e a **conferência** como dono, senão a medição mente.

## Duas armadilhas que já custaram tempo

**Estado velho no Cloud.** Erro que não corresponde ao código quase sempre é
processo não reiniciado, não defeito. Reboote e reproduza antes de investigar —
está em [AGENTS.md](../AGENTS.md).

**Cobertura que para na porta.** A suíte chamava `trocar_senha` **zero vezes em
291 testes**: os duplos de `st` nunca apertavam o botão, então os testes
provavam a trava e não o fluxo. Dois defeitos passaram por aí. Hoje
`tests/test_troca_de_senha_fluxo.py` percorre o submit inteiro, e um guarda em
`tests/conftest.py` reprova a suíte completa que volte a não exercitar essa
chamada.

Vale a pergunta sempre que um teste "cobre uma tela": ele chega a executar a
operação, ou só monta a tela e verifica o que ficou desenhado?

## Como manter isto honesto## Como manter isto honesto

Uma linha só sai de "não exercitado" para "confirmado" quando alguém **fez a
coisa em produção e viu o efeito**. Migração aplicada não é o mesmo que
funcionalidade validada: a `0010` está aplicada e a troca obrigatória continua
por validar.

Verificar consulta contra o banco também não basta por si só — e quando for
fazê-lo, rode sob `rls_session.aplicar_identidade`, nunca como `postgres`. O
motivo está em [AGENTS.md](../AGENTS.md).
