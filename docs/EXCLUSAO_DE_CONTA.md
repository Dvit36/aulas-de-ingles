# Exclusão de conta: defeito conhecido e procedimento

**Status:** resolvido no ramo que arquiva, em 9 de setembro de 2026 — sem que a
causa tenha sido encontrada. A troca do formulário pelos dois botões do
catálogo (`b857f65`) foi feita por suspeita, e funcionou: a conta `teste` foi
arquivada pela tela em 9/set 02:07 UTC, com `user_archived` na auditoria. É a
primeira linha de exclusão ou arquivamento que já existiu neste banco — o
parágrafo abaixo, que diz que nunca houve nenhuma, estava certo até então.

Continua sem confirmação o ramo que **remove** de vez: conta sem histórico, que
apaga também no Auth. Para essas, e para remoção definitiva de qualquer conta,
o procedimento pelo Supabase Auth mais abaixo segue valendo.

## O defeito

Na aba **Alunos**, confirmar a exclusão de uma conta **não produz nada** em
produção: nenhuma mensagem de sucesso, nenhuma de erro, nenhuma mudança na
lista. O banco confirma que a operação não acontece — nunca houve uma linha
`user_deleted` nem `user_archived` em `audit_logs`, e as contas seguem íntegras
em `public.profiles` e em `auth.users`.

A auditoria é escrita na **mesma transação** da exclusão (`add_audit` só faz
`session.add`, sem commit próprio). Logo: exclusão comitada implica auditoria
comitada. Não existe "excluiu e ninguém viu". A recíproca não vale — uma
tentativa que falha e sofre rollback também não deixa auditoria, então a
ausência dela não distingue "não executou" de "executou e desfez".

## O que foi descartado

Reproduzido localmente contra SQLite, em navegador real e com `AppTest`. **Os
quatro caminhos funcionam:**

| cenário | resultado local |
|---|---|
| confirmação certa, remoção no Auth OK | conta excluída |
| confirmação certa, remoção no Auth falha | erro visível, rollback |
| confirmação errada | erro visível, nada apagado |
| confirmação vazia | erro visível, nada apagado |

Descartados por medição, não por leitura:

- **"formulário dentro de expander"** — `reset_password_form` está no expander
  vizinho, tem a mesma estrutura de `st.form`, e funciona em produção.
- **chave duplicada** — os 11 `st.form` do arquivo têm chaves distintas, e não
  há `key=` explícita repetida.
- **o detector de divergências** — o `SAVEPOINT` que ele abre antes, na mesma
  transação, não afeta o submit. Medido isolando expander + form + submit, com
  e sem savepoint.

## O que foi trocado, e não resolveu

O formulário de exclusão foi substituído pelo padrão de `_confirm_activity_delete`
— dois botões com `key=` própria, sem `st.form`, confirmação em dois passos.
Verificado ponta a ponta em navegador local: pedir, confirmar, conta removida,
mensagem visível. **Em produção o comportamento não mudou.**

A única diferença estrutural conhecida entre o formulário que falhava e o que
funciona: só o de exclusão tinha um `text_input` de **rótulo dinâmico**
(`Digite "{username}" para confirmar`). Sem `key=` explícita, o rótulo é o que
dá identidade ao widget no Streamlit. É correlação, não mecanismo demonstrado.

## Dois defeitos reais achados no caminho, esses corrigidos

- A confirmação de sucesso era desenhada e destruída pelo `st.rerun()` seguinte.
  Atravessa no `session_state` e é desenhada fora do expander.
- O aviso era genérico e não dizia se **aquela** conta seria arquivada ou
  removida. Usa `count_user_references`, a mesma função que decide.

E um detalhe de Streamlit que vale para a tela inteira: **uma rerun devolve o
expander ao estado fechado**. Qualquer coisa desenhada lá dentro depois de uma
rerun pode nascer invisível — daí o `expanded=` na confirmação e a mensagem de
sucesso ir para fora.

## Procedimento: apagar pelo Supabase Auth

`public.profiles.id` referencia `auth.users(id)` **`on delete cascade`**
(`0001_schema_inicial.sql`). Então:

**Apague a conta no Supabase Auth.** Dashboard → Authentication → Users →
remover o usuário. O perfil sai por cascade, e os dois lados ficam consistentes
sem passo manual.

**Nunca apague só de `public.profiles`.** Isso deixa uma conta no Auth que ainda
autentica e não tem perfil — e o detector da aba Alunos **não enxerga** esse
caso: ele percorre `profiles` e confere o lado do Auth, então uma conta órfã no
Auth não aparece em lista nenhuma.

Depois de apagar, a aba Alunos não deve acusar divergência. Se acusar, os dois
lados saíram de passo e a correção é reconciliar pelo Auth.

## Saída de emergência: destravar a troca obrigatória de senha

Desde a migração `0010`, uma conta com `must_change_password = true` cai direto
na tela de troca e não navega até trocar. Criar conta e redefinir senha marcam
essa coluna.

Se alguém ficar preso ali — troca que não conclui, conta marcada por engano —,
destrave pelo banco:

```sql
update public.profiles set must_change_password = false where id = '<uuid>';
```

Pelo nome de usuário, se o id não estiver à mão:

```sql
update public.profiles set must_change_password = false where username = 'luiz';
```

Para conferir quem está marcado:

```sql
select id, username, must_change_password from public.profiles order by username;
```

Perfis criados antes da `0010` nasceram com `false` e não são afetados — a
migração não tem backfill, de propósito. O administrador não corre risco de se
trancar fora só por aplicar a migração.

## Se alguém retomar

O que falta é saber se o clique chega ao servidor. O caminho é o navegador, não
o log: **F12 → Network → filtro `WS` → `_stcore/stream` → Messages**, e clicar
em confirmar. Frame enviado significa que chegou. Log silencioso não prova nada:
uma rerun bem-sucedida do Streamlit não escreve nada.
