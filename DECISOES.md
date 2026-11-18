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
- `git`, `gh` e `ssh` são resolvidos somente em `/usr/bin`, `/bin` e `/usr/local/bin`, nessa ordem, exigindo arquivo regular executável e destino de symlink ainda dentro desses diretórios. O PATH herdado nunca participa da seleção.
- Todo subprocesso capaz de criar arquivos recebe `umask=077`. Ao abrir e após gravações, o cofre reforça diretórios 0700 e arquivos sem bits de grupo/outros; Markdown e estado ficam 0600, enquanto objetos Git somente leitura podem permanecer 0400.
- Commits automáticos usam a identidade fixa de privacidade `Biblioteca de Ideias <Torugo76@users.noreply.github.com>`; autenticação HTTPS usa exclusivamente o helper do executável `gh` resolvido. SSH usa `/usr/bin/ssh -F /dev/null -o BatchMode=yes`, preservando o agente e `known_hosts`, sem herdar `~/.ssh/config` nem desativar a verificação de host.
- Falha de commit executa `restore --staged` somente nos pathspecs exatos da operação e nunca altera o worktree.
- O app nunca sobrescreve silenciosamente uma edição externa.
- O bootstrap roda antes do `MarkdownStore`: em máquina nova, o único cofre permitido é clonado do repositório privado existente para um temporário 0700, endurecido (diretórios 0700, arquivos 0600, sem links/especiais), validado e publicado por `renameat2(RENAME_NOREPLACE)`. Destinos existentes ou concorrentes são preservados; plataformas sem a primitiva falham fechado.
- Login usa `gh auth login --web` em worker cancelável; a interface nunca lê token ou `hosts.yml`. Desconectar afeta apenas a preferência local do aplicativo.
- O corpo Markdown integral é canônico; trechos dos cartões são derivados sem regravar o arquivo.
- A prévia escapa HTML, remove imagens remotas e nunca abre links automaticamente.
- O caminho real do cofre pode ser consultado, copiado e aberto, mas não trocado neste incremento.
- O identificador freedesktop, o nome do SVG e o `desktopFileName` são `br.com.torugo.bibliotecadeideias`.
- Sincronização remota, autenticação e criação de repositórios exigem uma autorização posterior.
- O bootstrap de projetos consumirá o cofre por um contrato separado; não será embutido no MVP.

# Incremento E — conflitos explícitos e recuperáveis

- Divergência cria refs de recuperação e snapshots privados no estado XDG antes de pedir qualquer decisão.
- A identidade de uma ideia é o UUID do front matter, não seu caminho; renomear e mover não quebram a comparação.
- Nenhuma versão é escolhida automaticamente. Local, GitHub, ambas e combinação editada são decisões visíveis do usuário.
- A árvore reconciliada é construída em índice temporário e validada antes de avançar o checkout; mudanças concorrentes abortam e preservam a recuperação.
- Refs e snapshots não expiram automaticamente nesta versão. A limpeza é deliberadamente manual após conferência do push.

