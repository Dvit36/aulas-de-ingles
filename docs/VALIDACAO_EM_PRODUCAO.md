# O que já foi exercitado contra o ambiente real

**Atualizado:** 9 de setembro de 2026.

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
| `3046389` + `a6f66e5` | Troca obrigatória de senha, fim a fim | A conta `teste` gravou `user_password_changed` **como ela mesma** em 9/set 02:05 UTC, e `must_change_password` voltou a `false`. Essa linha só existe por `public.concluir_troca_de_senha()`: a marca caiu pelo caminho previsto, sob RLS |
| `b857f65` | Exclusão pela tela, no ramo que arquiva | `user_archived` na auditoria em 9/set 02:07 UTC e `profiles.archived_at` da conta `teste` no mesmo instante — a primeira linha dessas que já existiu em produção |
| migração `0012` + `fa135af` | Lançamento manual, estorno e leitura pelo aluno | Exercitados contra o banco real em transação desfeita, sob `authenticated`: o insert passa com claims de admin; o estorno passa; o segundo estorno é recusado pela aplicação e, por fora dela, pelo índice único; o aluno lê o par com os dois motivos; e o insert cru sob o papel do aluno é recusado pela RLS |
| migração `0013` + `394558e` | Ledger fechado ao dono, ranking pela função | Ensaiada em transação desfeita com claims reais: **antes** o aluno lia o motivo de outro; **depois** não lê, nem pelo `select` amplo nem perguntando pelo `student_id` alheio, que devolve zero linhas. Aplicada e conferida: `ledger_leitura` não existe mais, `public.ranking()` tem `prosecdef`, `search_path` fixo e execute só para `authenticated`. `leaderboard_rows` foi exercitada sob os dois papéis reais e devolveu `student_id` como texto |
| migração `0013`, com **duas contas reais** | O ranking mostra os dois, e o motivo do colega não vaza | Com `teste` e `teste2`, cada um com o próprio JWT: os dois veem os dois nomes, na ordem certa, e o `next_rival` do segundo voltou a existir ("faltam 5 pontos para alcançar teste"). Pedindo o `reason` do colega pelo **id do lançamento**, pelo `student_id`, pela soma e pelo `select` amplo: zero linhas nas quatro formas, nos dois sentidos. O próprio motivo continua legível, e o administrador lê os dois |

## Ainda não exercitado

| Commit | O quê | Por quê |
|---|---|---|
| `8cdba07` | O aviso de divergência na aba Alunos | A consulta está validada; o banner nunca apareceu porque não há divergência — é o resultado certo, mas não prova o caminho de renderização |
| `a30f424` | Troca de senha e revogação de sessão no logout | Corrigido a partir do diagnóstico, mas ainda não exercitado pela tela |
| `24da0c0` | Guarda do `contas=None` ao renomear | Nenhum chamador atual omite `contas`; só dispara para código futuro |
| `7d092ac` | Tolerância a motor de OCR ausente | Deploy quebrado |
| `convergencia-pipeline-etapa2` | Convergência dos dois fluxos de submissão | Não mesclada; o roteiro de quatro envios depende do app no ar |
| `7fa0ffb` + `7a4a7f1` | As duas telas do lançamento manual | Os serviços estão exercitados contra o banco real, as telas não: ninguém abriu a aba Pontos em produção. Dá para exercitar agora — `teste` e `teste2` estão desativados, e o seletor esconde só os **arquivados**, de propósito: conta desativada pode ter estorno pendente |

## Estado de operação: sem OCR, por tempo indeterminado

O build do Streamlit Community Cloud falha desde 7 de setembro de 2026 com
`Release file ... is expired`: a imagem base deles serve metadados apt
vencidos, e **qualquer aplicativo com um `packages.txt` aborta no build**. O
`apt-get update` falha antes de olhar o conteúdo do arquivo, então não importa
quais pacotes estão listados.

Nada nosso mudou: `packages.txt` e `requirements.txt` não eram tocados desde
agosto quando isso começou.

A contingência (`bce1ef0`) tira `rapidocr-onnxruntime` do `requirements.txt` e
remove o `packages.txt`. Sem esse arquivo o passo do apt não acontece e o build
passa. É **o estado de operação atual**, e vale até eles consertarem a imagem.

### Reverter a contingência derruba o aplicativo

Já aconteceu, em 8 de setembro: por informação de que o problema estava
resolvido, o OCR foi restaurado (`8efd679`), o `packages.txt` voltou junto, o
app foi recriado e o build falhou. Foi preciso reaplicar a contingência
(`ac2f4a8`).

**Deletar e recriar o aplicativo não ajuda** — o app novo constrói a partir da
mesma imagem base e falha igual.

O caminho de volta está no topo do `requirements.txt`: descomentar a linha do
RapidOCR e recriar o `packages.txt` com `libgl1`, `libglib2.0-0t64` e
`libgomp1`. **Só faça isso com evidência de que o build passa** — um deploy de
teste, ou anúncio deles. Informação de terceiros sobre "estar resolvido" já
custou uma queda.

### O que o aplicativo perde nesse estado

Medido, mesmo envio nos dois modos: com OCR aprova sozinho com confiança 0,97;
sem OCR vai para `needs_review` com 0,57, texto vazio e zero unidades
reconhecidas. O arquivo chega ao bucket nos dois casos.

Continuam funcionando login, envio de imagem e documento, upload ao Storage,
antifraude por checksum e pHash, leaderboard, pontuação e gestão de contas.
Param a aprovação automática, a extração de texto, a detecção de plataforma
pelo texto e a contagem automática de unidades — **o administrador decide cada
envio à mão.**

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

Em 9/set/2026 a conta `teste`
(`53c13db1-7ce1-459c-aa67-fff69372ad60`) foi **desarquivada**, para servir de
cobaia do lançamento manual antes de entrarem alunos de verdade. Ela havia
sido arquivada — e não removida — porque tinha histórico: exatamente uma linha
de auditoria, a `user_password_changed` que ela própria gravou ao trocar a
senha. Desarquivar são dois lados, na ordem segura: primeiro o perfil
(`active`, `archived_at`), comitado, e só depois o ban levantado no Auth. Se a
segunda etapa falhar sobra um perfil ativo que não entra — sintoma visível na
primeira tentativa de login. A ordem inversa deixaria uma conta que autentica
e que a aplicação considera inexistente.

Em 9/set/2026, para a verificação acima, foi criado o aluno `teste2`
(`849c5f3c-ee41-4441-970e-afac1b270c20`) e um lançamento manual em cada um dos
dois. **As duas contas foram desativadas em seguida**, pelo caminho da
aplicação (`save_user` com `active=False`, que bane no Auth antes do commit):
`teste` e `teste2` são alunos, e alunos ativos aparecem no ranking que os sete
de verdade vão ver. Desativar é o que os tira de lá — estornar os deixaria
visíveis com zero.

Os dois lançamentos ficam no ledger, e ficam para sempre: o ledger é imutável,
e por causa deles essas duas contas não podem mais ser apagadas, só arquivadas.

Não há endpoint administrativo de logout no GoTrue (`POST
admin/users/{id}/logout` responde `404 page not found`); a revogação foi por
SQL nas tabelas de sessão do schema `auth`, em transação conferida antes do
commit. É exceção consciente ao hábito de não tocar schema gerenciado por SQL,
e o schema `storage` continua fora de alcance, como o AGENTS.md exige.

## Apagar deixou de ser opção, e o banco é quem decide

Medido em produção em 9/set/2026, como dono do banco, em transação desfeita:

```
BARRADO  delete em ledger_transactions  ->  ledger transactions are immutable
BARRADO  delete em profiles             ->  viola ledger_transactions_student_id
```

`ledger_transactions.student_id` é `RESTRICT`, e o ledger é à prova de `delete`
por gatilho — inclusive para o `postgres`. **A partir do primeiro ponto de um
aluno, arquivar é a única saída.** Não por decisão nossa: apagar pelo Supabase
Auth cascateia até `profiles` e é barrado ali, e apagar o lançamento antes
esbarra no gatilho.

Isso muda o peso do defeito da seção seguinte. O ramo que **remove** de vez só
se aplica a conta que nunca pontuou — cadastro errado, desfeito no mesmo dia.
Para aluno com histórico, que é o caso normal a partir do primeiro envio
aprovado, a resposta certa é arquivar, e arquivar funciona. O caminho que
continua sem confirmação é o que quase nunca vai ser percorrido.

Vale notar de onde veio a descoberta: da pergunta "posso deixar estes dois
lançamentos de teste?". A resposta honesta exigia saber o que eles custam, e o
que custam é que as contas deixaram de poder ser apagadas. **Pergunta sobre
limpeza que se responde sem medir vira regra errada no documento.**

## Defeito que se resolveu sozinho — e como se soube

A exclusão de conta pela interface não fazia nada em produção. A investigação
foi encerrada em 7/set sem causa encontrada, e a troca do formulário pelos dois
botões do catálogo (`b857f65`) foi feita por suspeita, não por diagnóstico.

Ela funcionou. Em 9/set 02:07 UTC a conta `teste` foi arquivada pela tela, com
`user_archived` na auditoria — a primeira linha de exclusão ou arquivamento que
já existiu neste banco. O documento antigo afirmava, corretamente para a época,
que nunca houvera nenhuma.

Fica um resto por confirmar: o ramo que **remove** de vez, para conta sem
histórico, que apaga também no Auth. Nenhuma conta passou por ele em produção.
Ver [EXCLUSAO_DE_CONTA.md](EXCLUSAO_DE_CONTA.md).

Vale o método, mais do que o caso: ninguém percebeu que tinha funcionado. A
prova estava na auditoria havia horas, e só apareceu porque uma consulta feita
para outro fim passou por ali. **Defeito que se corrige por suspeita precisa de
uma volta ao banco para saber se a suspeita estava certa.**

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

**Ao verificar, cuidado com o SAVEPOINT.** `session.begin_nested()` dispara o
evento que reaplica a identidade declarada na sessão: dentro dele, uma troca de
papel feita à mão volta atrás sem avisar. Isto produziu um falso "FALHA DE
SEGURANÇA" na verificação do lançamento manual — o insert que deveria ser
barrado passou porque, dentro do SAVEPOINT, quem inseria era o administrador.
Remedido com a sessão inteira declarada como do aluno: a RLS barra, com
`new row violates row-level security policy`.

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

## Como manter isto honesto

Uma linha só sai de "não exercitado" para "confirmado" quando alguém **fez a
coisa em produção e viu o efeito**. Migração aplicada não é o mesmo que
funcionalidade validada: a `0010` está aplicada e a troca obrigatória continua
por validar.

Verificar consulta contra o banco também não basta por si só — e quando for
fazê-lo, rode sob `rls_session.aplicar_identidade`, nunca como `postgres`. O
motivo está em [AGENTS.md](../AGENTS.md).

## O ledger fechou — e a justificativa para deixá-lo aberto era falsa

`ledger_leitura` deixava **qualquer autenticado ler o ledger inteiro**
(`auth.uid() is not null`). Enquanto a tabela guardava pontos e chaves de
origem, custava pouco. Depois da 0012 ela guarda `reason`: texto escrito
**sobre** uma pessoa — "estava atrasado nas lições e compensou". Isso passou a
ser legível por todos os colegas com o próprio JWT.

Eu registrei aqui, antes, que estreitar a política derrubaria o leaderboard.
**Não derrubaria, e a medição mostrou por quê:** `profiles` já é restrito ao
próprio perfil por `perfil_proprio_leitura`, e `leaderboard_rows` faz join com
ele. Sob o papel do aluno o ranking já devolvia uma linha só. A leitura ampla
do ledger não sustentava o leaderboard — ele já estava quebrado, por outro
motivo, e o ledger aberto só vazava.

Ou seja: **havia um defeito de privacidade e um defeito de funcionalidade, e a
crença de que um pagava o outro impediu de ver os dois.** A 0013 fecha o
ledger ao dono e ao administrador, e devolve o ranking por
`public.ranking(inicio, fim)` — três colunas, `SECURITY DEFINER`, sem
`student_id` como parâmetro.

Nada disso apareceria com um aluno só cadastrado. Vale a pergunta ao ler uma
justificativa herdada: **ela foi medida alguma vez, ou só repetida?**
