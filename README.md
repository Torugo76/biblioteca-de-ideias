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
- sincronização Git assíncrona ao abrir, manualmente e após cada salvamento;
- instalação em outro computador por login oficial no navegador e clone validado do cofre privado existente;
- estados claros para offline, autenticação, envio pendente e divergência;
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

## Sincronização segura

O aplicativo usa uma fila FIFO única em uma thread dedicada; a interface não espera por rede. Salvamentos nunca são agrupados. Pedidos repetidos de atualização podem ser combinados enquanto um já está pendente.

- Git é chamado sem shell, aliases, prompts ou hooks;
- somente os caminhos exatos de cada operação são adicionados;
- mudanças já staged por outro programa bloqueiam o commit automático;
- atualização remota ocorre somente por fast-forward;
- antes de rede, a URL de fetch e o único destino efetivo de push de `origin` precisam apontar exatamente para o cofre privado oficial na branch `main`, confirmados por metadados do GitHub CLI; a operação usa diretamente um snapshot imutável dessas URLs aprovadas, nunca o nome mutável do remoto;
- configurações Git system/global são isoladas; regras locais `insteadOf`, `pushInsteadOf` e includes são recusadas, e a configuração local permanece bloqueada durante a operação para impedir redirecionamentos concorrentes feitos pelas ferramentas normais do Git;
- Git e GitHub CLI rodam com ambiente mínimo, sem comandos SSH/askpass/proxy/editor/diff/filtro herdados; autenticação HTTPS usa somente o helper do `gh` resolvido e SSH usa `/usr/bin/ssh -F /dev/null -o BatchMode=yes` com o socket do agente e `known_hosts` normais, ignorando `~/.ssh/config` sem desativar a verificação de host;
- executáveis `git`, `gh` e `ssh` são escolhidos apenas em diretórios de sistema fixos; o PATH do processo não pode substituí-los por wrappers;
- o avanço revalida status/index e locks do índice/referência, então usa `merge --ff-only` com `merge.autoStash=false`; esse comando apenas move a referência em fast-forward, nunca cria commit de merge nem resolve divergências;
- divergências nunca são combinadas: o único `merge` permitido é `--ff-only`, sem commit de merge, autostash ou resolução automática; rebase, reset e force-push são proibidos;
- falta de rede mantém o commit local como envio pendente;
- falhas de Git não desfazem o Markdown salvo localmente;
- falha de commit remove do índice somente os caminhos adicionados pela operação, sem tocar no worktree ou em staged externo;
- numa instalação nova, o app clona em diretório temporário privado, rejeita links e arquivos especiais, normaliza diretórios para 0700 e arquivos para 0600, valida branch, remoto, privacidade, estrutura e Markdown e publica com `renameat2(RENAME_NOREPLACE)`; sem suporte do kernel, falha fechado;
- uma pasta de destino já existente nunca é substituída; login cancelado, conta errada ou repositório inacessível preservam o conteúdo local;
- “Desconectar do aplicativo” desativa somente a integração local e não executa `gh auth logout`;
- o `gh` é usado somente para `auth status`, login oficial `auth login --web`, consulta `repo view` de proprietário/privacidade e o helper controlado `gh auth git-credential`; `gh auth token` e `hosts.yml` nunca são lidos. A clonagem assistida usa o executável Git com URL fixa do repositório existente.

O botão **Sincronizar** executa a atualização manual. A barra informa: Sincronizado, Sincronizando, Alterações locais, Envio pendente, Offline, Login necessário, Conflito precisa de atenção ou Erro dados locais salvos.

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

Não há anexos, exclusão de cartões nem resolução visual de conflitos. A prévia não abre links nem carrega imagens remotas. O futuro inicializador de projetos e a futura orquestração Codex/Claude Code serão componentes separados e não fazem parte deste aplicativo neste momento.

