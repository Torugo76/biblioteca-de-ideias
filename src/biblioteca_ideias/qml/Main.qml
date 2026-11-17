import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.ApplicationWindow {
    id: root
    width: 1500
    height: 860
    minimumWidth: 760
    minimumHeight: 560
    visible: true
    title: "Biblioteca de Ideias"

    property var selectedIdea: ({})

    function ideasFor(status) {
        return backend.ideas.filter(function(item) { return item.status === status })
    }

    function formatDate(value) {
        var date = new Date(value)
        if (isNaN(date.getTime())) return value
        return date.toLocaleDateString(Qt.locale("pt_BR"), Locale.ShortFormat)
    }

    function openEditor(idea) {
        editorDialog.loading = true
        selectedIdea = idea || ({})
        editorTitle.text = idea && idea.id ? idea.titulo : ""
        editorBody.text = idea && idea.id ? idea.corpo : backend.initialBody
        statusCombo.currentIndex = idea && idea.id
            ? backend.columns.findIndex(function(c) { return c.chave === idea.status }) : 0
        editorTabs.currentIndex = 0
        editorDialog.open()
        Qt.callLater(function() {
            editorDialog.dirty = false
            editorDialog.loading = false
            editorTitle.forceActiveFocus()
        })
    }

    function requestEditorClose() {
        if (editorDialog.dirty) discardDialog.open()
        else editorDialog.close()
    }

    onClosing: function(close) {
        if (editorDialog.visible && editorDialog.dirty) {
            close.accepted = false
            discardDialog.open()
        }
    }

    header: Controls.ToolBar {
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Kirigami.Units.largeSpacing
            anchors.rightMargin: Kirigami.Units.largeSpacing
            spacing: Kirigami.Units.smallSpacing
            Controls.Label { text: "Biblioteca de Ideias"; font.bold: true; font.pixelSize: 20 }
            Item { Layout.fillWidth: true }
            Controls.Label {
                text: backend.syncStatus
                color: backend.syncStatus === "Sincronizado"
                       ? Kirigami.Theme.positiveTextColor
                       : (backend.syncStatus === "Conflito precisa de atenção"
                          || backend.syncStatus === "Erro dados locais salvos")
                         ? Kirigami.Theme.negativeTextColor
                         : Kirigami.Theme.neutralTextColor
            }
            Controls.Button {
                text: "Sincronizar"
                enabled: backend.syncStatus !== "Sincronizando"
                onClicked: backend.syncNow()
            }
            Controls.Button { text: "Configurações"; onClicked: settingsDialog.open() }
            Controls.Button { text: "Recarregar"; onClicked: backend.reload() }
            Controls.Button { text: "Nova ideia"; highlighted: true; onClicked: root.openEditor(null) }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Kirigami.Units.largeSpacing
        spacing: Kirigami.Units.largeSpacing

        Controls.Label {
            Layout.fillWidth: true
            text: backend.notice
            wrapMode: Text.WordWrap
            color: Kirigami.Theme.disabledTextColor
        }

        Controls.ScrollView {
            id: boardScroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: board.width
            contentHeight: availableHeight
            clip: true

            Row {
                id: board
                height: boardScroll.availableHeight
                spacing: Kirigami.Units.largeSpacing
                width: Math.max(boardScroll.availableWidth, 5 * 270 + 4 * spacing)

                Repeater {
                    model: backend.columns
                    delegate: Kirigami.Card {
                        id: columnCard
                        required property var modelData
                        property var columnIdeas: root.ideasFor(modelData.chave)
                        width: (board.width - 4 * board.spacing) / 5
                        height: board.height

                        contentItem: ColumnLayout {
                            spacing: Kirigami.Units.smallSpacing
                            RowLayout {
                                Layout.fillWidth: true
                                Controls.Label {
                                    text: columnCard.modelData.titulo
                                    font.bold: true
                                    font.pixelSize: 17
                                    Layout.fillWidth: true
                                }
                                Controls.Label {
                                    text: columnCard.columnIdeas.length
                                    horizontalAlignment: Text.AlignHCenter
                                    Layout.minimumWidth: 24
                                    color: Kirigami.Theme.disabledTextColor
                                }
                            }

                            Controls.ScrollView {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                ListView {
                                    id: ideaList
                                    spacing: Kirigami.Units.smallSpacing
                                    model: columnCard.columnIdeas
                                    Controls.Label {
                                        anchors.centerIn: parent
                                        visible: ideaList.count === 0
                                        text: "Nenhuma ideia"
                                        color: Kirigami.Theme.disabledTextColor
                                    }
                                    delegate: Kirigami.AbstractCard {
                                        required property var modelData
                                        width: ideaList.width - Kirigami.Units.smallSpacing
                                        contentItem: ColumnLayout {
                                            spacing: Kirigami.Units.smallSpacing
                                            Controls.Label {
                                                text: modelData.titulo
                                                font.bold: true
                                                font.pixelSize: 15
                                                Layout.fillWidth: true
                                                wrapMode: Text.WordWrap
                                            }
                                            Controls.Label {
                                                text: modelData.resumo || "Sem resumo"
                                                Layout.fillWidth: true
                                                maximumLineCount: 4
                                                elide: Text.ElideRight
                                                wrapMode: Text.WordWrap
                                                opacity: 0.82
                                            }
                                            Controls.Label {
                                                text: "Atualizada em " + root.formatDate(modelData.atualizada_em)
                                                Layout.fillWidth: true
                                                font.pixelSize: 11
                                                color: Kirigami.Theme.disabledTextColor
                                            }
                                        }
                                        onClicked: root.openEditor(modelData)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Controls.Dialog {
        id: editorDialog
        property bool dirty: false
        property bool loading: false
        title: root.selectedIdea.id ? "Editar ideia" : "Nova ideia"
        modal: true
        closePolicy: Controls.Popup.NoAutoClose
        anchors.centerIn: parent
        width: Math.max(680, root.width * 0.78)
        height: Math.max(500, root.height * 0.78)

        contentItem: ColumnLayout {
            spacing: Kirigami.Units.smallSpacing
            Controls.TextField {
                id: editorTitle
                Layout.fillWidth: true
                placeholderText: "Título da ideia"
                font.pixelSize: 20
                font.bold: true
                onTextChanged: if (editorDialog.visible && !editorDialog.loading) editorDialog.dirty = true
            }
            RowLayout {
                Layout.fillWidth: true
                Controls.ToolButton { text: "Título"; onClicked: editorBody.insert(editorBody.cursorPosition, "\n## ") }
                Controls.ToolButton { text: "Lista"; onClicked: editorBody.insert(editorBody.cursorPosition, "\n- ") }
                Controls.ToolButton { text: "Checklist"; onClicked: editorBody.insert(editorBody.cursorPosition, "\n- [ ] ") }
                Controls.ToolButton {
                    text: "Negrito"
                    onClicked: {
                        var start = editorBody.selectionStart
                        var end = editorBody.selectionEnd
                        var selected = editorBody.selectedText
                        editorBody.remove(start, end)
                        editorBody.insert(start, "**" + selected + "**")
                        editorBody.select(start + 2, start + 2 + selected.length)
                    }
                }
                Item { Layout.fillWidth: true }
                Controls.Label { text: "Coluna" }
                Controls.ComboBox {
                    id: statusCombo
                    Layout.preferredWidth: 180
                    model: backend.columns
                    textRole: "titulo"
                    enabled: root.selectedIdea.id !== undefined
                    onCurrentIndexChanged: if (editorDialog.visible && !editorDialog.loading) editorDialog.dirty = true
                }
            }
            Controls.TabBar {
                id: editorTabs
                Layout.fillWidth: true
                Controls.TabButton { text: "Escrever" }
                Controls.TabButton { text: "Pré-visualizar" }
            }
            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: editorTabs.currentIndex
                Controls.ScrollView {
                    Controls.TextArea {
                        id: editorBody
                        wrapMode: TextEdit.Wrap
                        selectByMouse: true
                        placeholderText: "Escreva em Markdown..."
                        onTextChanged: if (editorDialog.visible && !editorDialog.loading) editorDialog.dirty = true
                    }
                }
                Controls.ScrollView {
                    TextEdit {
                        width: parent.width
                        text: backend.safePreviewMarkdown(editorBody.text)
                        textFormat: TextEdit.MarkdownText
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.Wrap
                        color: Kirigami.Theme.textColor
                        onLinkActivated: function(link) { /* prévia passiva */ }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Controls.Label {
                    text: editorDialog.dirty ? "Alterações não salvas" : ""
                    color: Kirigami.Theme.neutralTextColor
                    Layout.fillWidth: true
                }
                Controls.Button { text: "Cancelar"; onClicked: root.requestEditorClose() }
                Controls.Button {
                    text: "Salvar"
                    highlighted: true
                    onClicked: {
                        var saved = false
                        if (root.selectedIdea.id) {
                            var newStatus = backend.columns[statusCombo.currentIndex].chave
                            saved = backend.saveIdea(root.selectedIdea.id, editorTitle.text,
                                                     editorBody.text, newStatus,
                                                     root.selectedIdea.hash)
                        } else {
                            saved = backend.createIdea(editorTitle.text, editorBody.text)
                        }
                        if (saved) {
                            editorDialog.dirty = false
                            editorDialog.close()
                        }
                    }
                }
            }
        }
    }

    Controls.Dialog {
        id: discardDialog
        title: "Descartar alterações?"
        modal: true
        anchors.centerIn: parent
        standardButtons: Controls.Dialog.Discard | Controls.Dialog.Cancel
        onDiscarded: {
            editorDialog.dirty = false
            editorDialog.close()
        }
        Controls.Label { text: "Há alterações que ainda não foram salvas." }
    }

    Controls.Dialog {
        id: settingsDialog
        title: "Configurações"
        modal: true
        anchors.centerIn: parent
        width: Math.min(680, root.width - 48)
        ColumnLayout {
            anchors.fill: parent
            spacing: Kirigami.Units.largeSpacing
            Controls.Label { text: "Cofre de ideias"; font.bold: true; font.pixelSize: 18 }
            Controls.TextField {
                Layout.fillWidth: true
                text: backend.vaultPath
                readOnly: true
                selectByMouse: true
            }
            RowLayout {
                Controls.Button { text: "Copiar caminho"; onClicked: backend.copyVaultPath() }
                Controls.Button { text: "Abrir pasta"; onClicked: backend.openVault() }
            }
            Controls.Label { text: "Quantidade de ideias: " + backend.ideaCount }
            Controls.Label { text: backend.syncStatus; color: Kirigami.Theme.disabledTextColor }
            Controls.Label { text: backend.authStatus; color: Kirigami.Theme.disabledTextColor }
            Controls.Label {
                Layout.fillWidth: true
                text: "A localização do cofre é fixa neste incremento."
                wrapMode: Text.WordWrap
                color: Kirigami.Theme.disabledTextColor
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Controls.Button { text: "Fechar"; onClicked: settingsDialog.close() }
            }
        }
    }
}
