# Decisões do produto

- O aplicativo e o cofre são repositórios Git locais distintos, ainda sem remotos.
- O status é derivado somente da pasta da ideia.
- O UUID é imutável e existe somente no front matter; o nome físico deriva do título.
- Nomes preservam espaços, acentos e Unicode NFC; caracteres incompatíveis com Windows são substituídos.
- Colisões são globais entre colunas por NFC + `casefold`, e UUIDs duplicados bloqueiam o cofre.
- Título, corpo, nome e coluna são salvos por uma única operação com diário privado e rollback.
- O app nunca sobrescreve silenciosamente uma edição externa.
- O corpo Markdown integral é canônico; trechos dos cartões são derivados sem regravar o arquivo.
- A prévia escapa HTML, remove imagens remotas e nunca abre links automaticamente.
- O caminho real do cofre pode ser consultado, copiado e aberto, mas não trocado neste incremento.
- O identificador freedesktop, o nome do SVG e o `desktopFileName` são `br.com.torugo.bibliotecadeideias`.
- Sincronização remota, autenticação e criação de repositórios exigem uma autorização posterior.
- O bootstrap de projetos consumirá o cofre por um contrato separado; não será embutido no MVP.

