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
| `54bc859` | `reativar` levanta o ban | **Desativado: o login recusou. Reativado: o login voltou.** |
| migração `0009` | `contas_fora_de_sincronia()` | Aplicada; responde 2 linhas sob claims de admin e 0 sob claims de aluno, pelo papel `authenticated` real |
| migração `0010` | `must_change_password` | Aplicada; os três perfis existentes ficaram `false`, lido pelo papel real |

## Ainda não exercitado

| Commit | O quê | Por quê |
|---|---|---|
| `8cdba07` | O aviso de divergência na aba Alunos | A consulta está validada; o banner nunca apareceu porque não há divergência — é o resultado certo, mas não prova o caminho de renderização |
| `24da0c0` | Guarda do `contas=None` ao renomear | Nenhum chamador atual omite `contas`; só dispara para código futuro |
| `3046389` | Troca obrigatória de senha | Deploy quebrado desde 7/set |
| `7d092ac` | Tolerância a motor de OCR ausente | Deploy quebrado |
| `convergencia-pipeline-etapa2` | Convergência dos dois fluxos de submissão | Não mesclada; o roteiro de quatro envios depende do app no ar |

## Defeito aberto

A exclusão de conta pela interface não funciona em produção, por causa
desconhecida. Ver [EXCLUSAO_DE_CONTA.md](EXCLUSAO_DE_CONTA.md).

## Como manter isto honesto

Uma linha só sai de "não exercitado" para "confirmado" quando alguém **fez a
coisa em produção e viu o efeito**. Migração aplicada não é o mesmo que
funcionalidade validada: a `0010` está aplicada e a troca obrigatória continua
por validar.

Verificar consulta contra o banco também não basta por si só — e quando for
fazê-lo, rode sob `rls_session.aplicar_identidade`, nunca como `postgres`. O
motivo está em [AGENTS.md](../AGENTS.md).
