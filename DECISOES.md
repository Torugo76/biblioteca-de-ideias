# Decisões do produto

- O aplicativo e o cofre são repositórios Git distintos, com remotos separados no GitHub (código público e cofre privado).
- O status é derivado somente da pasta da ideia.
- O UUID é imutável e existe somente no front matter; o nome físico deriva do título.
- Nomes preservam espaços, acentos e Unicode NFC; caracteres incompatíveis com Windows são substituídos.
- Colisões são globais entre colunas por NFC + `casefold`, e UUIDs duplicados bloqueiam o cofre.
- Título, corpo, nome e coluna são salvos por uma única operação com diário privado e rollback.
- UUIDs são comparados na forma canônica; grafias legadas ficam intactas até o próximo salvamento, que normaliza o front matter.
- A migração de nomes possui diário privado próprio e qualquer interrupção provoca rollback idempotente para os caminhos originais.
- Git roda em uma thread dedicada com fila FIFO; somente fetch repetido pode ser agrupado.
- Operações usam argumentos fixos, sem shell, prompts ou hooks. O único `merge` permitido é `--ff-only`, que não cria commit de merge nem resolve divergências; rebase, reset e force-push são proibidos.
- Fast-forward revalida árvore/index e locks relevantes antes de usar `merge --ff-only --no-edit` com `merge.autoStash=false`; apesar do nome do comando, somente avanço linear é aceito, sem commit de merge, autostash ou resolução de divergência.
- Cada salvamento local enfileira origem e destino exatos; staged externo bloqueia automação.
- Todo fetch/push exige URL de fetch oficial, exatamente um destino de push oficial, branch `main` e confirmação em sessão de que `Torugo76/biblioteca-de-ideias-cofre` é privado. As URLs aprovadas são capturadas num snapshot imutável e passadas diretamente ao Git; uma troca posterior de `origin` não altera a operação em curso.
- Operações de rede ignoram configurações Git system/global, recusam regras locais `insteadOf`/`pushInsteadOf` e includes, e mantêm `config.lock`/`config.worktree.lock` durante a captura e o subprocesso. Overrides de identidade da URL aprovada são aplicados como defesa adicional. Escritas diretas que deliberadamente ignorem os locks do Git ficam fora do modelo de concorrência suportado.
- Subprocessos Git/GitHub recebem um ambiente mínimo: sem `GIT_SSH_COMMAND`, askpass, proxy, editor, diff externo, `LD_PRELOAD`, `PYTHONPATH` ou outras variáveis herdadas capazes de executar helpers. Permanecem apenas HOME/XDG necessários à sessão do `gh`, socket do agente SSH e caminhos fixos de executáveis do sistema. Configurações locais equivalentes (`core.sshCommand`, helpers, filtros, proxies e diffs externos) bloqueiam a automação.
- Commits automáticos usam a identidade fixa de privacidade `Biblioteca de Ideias <Torugo76@users.noreply.github.com>`; autenticação HTTPS usa exclusivamente o helper do executável `gh` resolvido, e SSH usa `/usr/bin/ssh` com o agente existente.
- Falha de commit executa `restore --staged` somente nos pathspecs exatos da operação e nunca altera o worktree.
- O app nunca sobrescreve silenciosamente uma edição externa.
- O corpo Markdown integral é canônico; trechos dos cartões são derivados sem regravar o arquivo.
- A prévia escapa HTML, remove imagens remotas e nunca abre links automaticamente.
- O caminho real do cofre pode ser consultado, copiado e aberto, mas não trocado neste incremento.
- O identificador freedesktop, o nome do SVG e o `desktopFileName` são `br.com.torugo.bibliotecadeideias`.
- Sincronização remota, autenticação e criação de repositórios exigem uma autorização posterior.
- O bootstrap de projetos consumirá o cofre por um contrato separado; não será embutido no MVP.

