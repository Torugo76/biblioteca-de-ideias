# Biblioteca de Ideias

Aplicativo desktop para Fedora KDE que organiza ideias em um quadro Kanban e grava cada cartão como Markdown legível no cofre local.

## O que já funciona

- interface em português com Kirigami;
- colunas Entrada, Avaliando, Planejada, Em andamento e Arquivada;
- criação, edição e movimentação de ideias;
- quadro responsivo, contadores por coluna e cartões com data;
- editor Markdown amplo com modelo inicial, atalhos e pré-visualização passiva;
- preservação do corpo integral e de seções personalizadas;
- tela de configurações com caminho real, abertura segura e quantidade de ideias;
- gravação atômica com `QSaveFile`;
- recusa de sobrescrita quando outro programa alterou o Markdown;
- trava entre duas instâncias do aplicativo;
- UUIDs estáveis no front matter e rejeição de links simbólicos;
- nomes de arquivo legíveis derivados do título, preservando Unicode em NFC;
- índice global de UUID e detecção de colisões por NFC sem diferenciar maiúsculas;
- salvamento único e transacional de título, corpo e coluna, com recuperação XDG;
- atalho no menu de aplicativos do KDE.

O cofre fica em `~/.local/share/Torugo/Biblioteca de Ideias/cofre` (ou no equivalente definido pelo ambiente XDG). Configurações e estado do aplicativo não são gravados no cofre.

## Instalar e abrir

```bash
chmod +x packaging/install-local.sh packaging/uninstall-local.sh
./packaging/install-local.sh
```

Depois, procure por **Biblioteca de Ideias** no menu de aplicativos. Para executar durante o desenvolvimento:

```bash
PYTHONPATH=src python3 -m biblioteca_ideias.app
```

O instalador cria um lançador pequeno em `~/.local/bin`, atualiza os caches de ícones/menu quando disponíveis e usa o Python e os módulos Qt/Kirigami do Fedora. Não cria ambiente virtual nem baixa pacotes.

## Dados e Git local

O código e o cofre são repositórios Git separados. O corpo Markdown completo é canônico; o resumo exibido no cartão é derivado da seção `## Resumo`. O UUID permanece somente no front matter; o nome físico usa o título sanitizado e o status continua derivado da pasta.

UUIDs são indexados pela forma canônica, portanto caixa alta e chaves não criam identidades diferentes. A grafia original de um arquivo legado é preservada até a ideia ser salva; nesse momento o front matter passa a usar a forma canônica.

O salvamento usa um diário privado em `$XDG_STATE_HOME/Torugo/Biblioteca de Ideias` (padrão `~/.local/state/...`). Se o processo cair durante uma edição, a próxima abertura restaura a versão anterior antes de carregar o cofre.

## Testes

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Arquitetura

- `storage.py`: contrato Markdown, validação, gravação e movimentação segura;
- `backend.py`: ponte entre domínio e QML, trava e mensagens ao usuário;
- `qml/Main.qml`: quadro Kanban e diálogos;
- `packaging/`: instalação e remoção locais.

## Migração de nomes legados

O utilitário `scripts/migrate_title_filenames.py` primeiro planeja todos os nomes e valida colisões. O modo `--simulate` trabalha em uma cópia temporária; `--apply` aplica no cofre indicado e confirma contagem, UUIDs, conteúdo e timestamps sem imprimir o texto das ideias.

## Limites atuais

Os repositórios remotos existem, mas a sincronização automática ainda não faz parte do aplicativo. Também não há anexos, exclusão de cartões nem resolução visual de conflitos. A prévia não abre links nem carrega imagens remotas. O futuro inicializador de projetos e a futura orquestração Codex/Claude Code serão componentes separados e não fazem parte deste aplicativo neste momento.

