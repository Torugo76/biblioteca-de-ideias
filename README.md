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
- resolução visual de divergências por ideia, com escolhas explícitas para manter a versão local, a versão do GitHub, ambas ou uma edição combinada;
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
- subprocessos Git/`gh` usam `umask=077`; o armazenamento reaplica 0700 aos diretórios e remove bits de grupo/outros de todos os arquivos. Markdown, journals, configuração, refs e índice ficam normalmente 0600; objetos Git imutáveis podem ficar 0400;
- executáveis `git`, `gh` e `ssh` são escolhidos apenas em diretórios de sistema fixos; o PATH do processo não pode substituí-los por wrappers;
- o avanço revalida status/index e locks do índice/referência, então usa `merge --ff-only` com `merge.autoStash=false`; esse comando apenas move a referência em fast-forward, nunca cria commit de merge nem resolve divergências;
- divergências nunca são decididas automaticamente: primeiro são criadas refs locais e cópias privadas de recuperação, e o conteúdo é comparado por UUID diretamente nas árvores Git, inclusive após renomear ou mover uma ideia;
- somente depois de escolhas explícitas para todas as ideias o aplicativo monta um commit de reconciliação em índice temporário, com os dois ancestrais; rebase, reset, checkout destrutivo e force-push são proibidos;
- antes de aplicar e enviar a reconciliação, o aplicativo confirma que checkout e GitHub continuam nos commits analisados; uma mudança concorrente interrompe a operação e preserva escolhas e backups;
- o Markdown resultante é serializado pelo mesmo contrato do armazenamento e validado integralmente antes de criar objetos ou commits; o limite de 2 MB inclui front matter e corpo;
- qualquer caminho `.md` da árvore precisa estar diretamente em uma coluna válida e ter nome compatível com seu título; Markdown na raiz, aninhado ou em coluna desconhecida interrompe a reconciliação sem ser apagado;
- imediatamente antes de aplicar e novamente antes de enviar, HEAD, `origin/main`, status completo (incluindo staged, unstaged e não rastreados), locks, configuração e o snapshot do remoto são revalidados;
- retomadas reutilizam o mesmo commit, UUID e timestamp guardados. Se o GitHub já confirmou esse commit, o manifesto é apenas marcado como resolvido, sem novo commit ou push;
- falta de rede mantém o commit local como envio pendente;
- falhas de Git não desfazem o Markdown salvo localmente;
- falha de commit remove do índice somente os caminhos adicionados pela operação, sem tocar no worktree ou em staged externo;
- numa instalação nova, o app clona em diretório temporário privado, rejeita links e arquivos especiais, normaliza diretórios para 0700 e arquivos para 0600, valida branch, remoto, privacidade, estrutura e Markdown e publica com `renameat2(RENAME_NOREPLACE)`; sem suporte do kernel, falha fechado;
- uma pasta de destino já existente nunca é substituída; login cancelado, conta errada ou repositório inacessível preservam o conteúdo local;
- “Desconectar do aplicativo” desativa somente a integração local e não executa `gh auth logout`;
- o `gh` é usado somente para `auth status`, login oficial `auth login --web`, consulta `repo view` de proprietário/privacidade e o helper controlado `gh auth git-credential`; `gh auth token` e `hosts.yml` nunca são lidos. A clonagem assistida usa o executável Git com URL fixa do repositório existente.

O botão **Sincronizar** executa a atualização manual. A barra informa: Sincronizado, Sincronizando, Alterações locais, Envio pendente, Offline, Login necessário, Conflito precisa de atenção ou Erro dados locais salvos.

### Recuperação de conflitos

Ao detectar históricos divergentes, o aplicativo não altera o checkout. Ele cria referências em `refs/recovery/` e exporta snapshots Markdown por UUID para a pasta privada de estado XDG. YAML inválido, UUID repetido ou nomes incompatíveis interrompem a análise como erro de integridade.

As referências, o manifesto e os snapshots são mantidos mesmo depois de uma resolução bem-sucedida para permitir auditoria e recuperação manual. Nesta versão não há limpeza automática por prazo: remova uma recuperação antiga somente após confirmar o conteúdo e o envio ao GitHub. Se a rede falhar depois da aplicação local, repetir a última escolha tenta apenas concluir o envio, sem recriar ou perder a reconciliação.

## Testes

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Arquitetura

- `storage.py`: contrato Markdown, validação, gravação e movimentação segura;
- `conflicts.py`: snapshots privados, comparação por UUID e reconciliação explícita;
- `backend.py`: ponte entre domínio e QML, trava e mensagens ao usuário;
- `qml/Main.qml`: quadro Kanban e diálogos;
- `packaging/`: instalação e remoção locais.

## Migração de nomes legados

O utilitário `scripts/migrate_title_filenames.py` primeiro planeja todos os nomes e valida colisões. O modo `--simulate` trabalha em uma cópia temporária; `--apply` aplica no cofre indicado e confirma contagem, UUIDs, conteúdo e timestamps sem imprimir o texto das ideias.

## Limites atuais

Não há anexos nem exclusão de cartões pela interface. A prévia não abre links nem carrega imagens remotas. A limpeza das recuperações antigas é manual. O futuro inicializador de projetos e a futura orquestração Codex/Claude Code serão componentes separados e não fazem parte deste aplicativo neste momento.

