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
- Operações usam argumentos fixos, sem shell, prompts ou hooks, e nunca fazem merge, rebase, reset ou force-push.
- Fast-forward revalida árvore/index e locks relevantes antes de usar `merge --ff-only --no-edit` com `merge.autoStash=false`; apesar do nome do comando, somente avanço linear é aceito, sem commit de merge, autostash ou resolução de divergência.
- Cada salvamento local enfileira origem e destino exatos; staged externo bloqueia automação.
- Todo fetch/push exige URL oficial exata, branch `main` e confirmação em sessão de que `Torugo76/biblioteca-de-ideias-cofre` é privado; troca posterior do remoto invalida a autorização.
- Falha de commit executa `restore --staged` somente nos pathspecs exatos da operação e nunca altera o worktree.
- O app nunca sobrescreve silenciosamente uma edição externa.
- O corpo Markdown integral é canônico; trechos dos cartões são derivados sem regravar o arquivo.
- A prévia escapa HTML, remove imagens remotas e nunca abre links automaticamente.
- O caminho real do cofre pode ser consultado, copiado e aberto, mas não trocado neste incremento.
- O identificador freedesktop, o nome do SVG e o `desktopFileName` são `br.com.torugo.bibliotecadeideias`.
- Sincronização remota, autenticação e criação de repositórios exigem uma autorização posterior.
- O bootstrap de projetos consumirá o cofre por um contrato separado; não será embutido no MVP.

