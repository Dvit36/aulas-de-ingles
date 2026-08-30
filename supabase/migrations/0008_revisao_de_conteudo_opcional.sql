-- Corrige o padrão de content_review_required, que o schema inicial inverteu.
--
-- O comportamento do produto é: uma atividade só exige avaliação humana de
-- conteúdo quando o catálogo pede isso explicitamente (é o caso de resumo em
-- português). O schema inicial declarou o padrão como `true`, o que faria
-- toda atividade nova cair na fila de revisão — inclusive Duolingo, que é
-- justamente a de aprovação automática.
--
-- Só o padrão muda. Linhas existentes ficam como estão: quem já marcou a
-- revisão como obrigatória decidiu isso, e nada aqui deve desfazer decisão de
-- administrador.
alter table public.activities
    alter column content_review_required set default false;
