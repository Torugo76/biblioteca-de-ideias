# Decisões do produto

- O aplicativo e o cofre são repositórios Git locais distintos, ainda sem remotos.
- O status é derivado somente da pasta da ideia.
- O nome físico é um UUID imutável; mudar o título não renomeia o arquivo.
- O app nunca sobrescreve silenciosamente uma edição externa.
- O corpo Markdown integral é canônico; trechos dos cartões são derivados sem regravar o arquivo.
- A prévia escapa HTML, remove imagens remotas e nunca abre links automaticamente.
- O caminho real do cofre pode ser consultado, copiado e aberto, mas não trocado neste incremento.
- O identificador freedesktop, o nome do SVG e o `desktopFileName` são `br.com.torugo.bibliotecadeideias`.
- Sincronização remota, autenticação e criação de repositórios exigem uma autorização posterior.
- O bootstrap de projetos consumirá o cofre por um contrato separado; não será embutido no MVP.

