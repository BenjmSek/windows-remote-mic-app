import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import OvbSettings 1.0

Item {
    id: root
    property var tokens
    property var backTabTarget: null
    property var tabTarget: null
    readonly property var firstFocusItem: openBluetoothSettingsButton
    readonly property var lastFocusItem: closeBehaviorSwitch
    readonly property int deviceStateColumnWidth: 96
    readonly property int deviceActionColumnWidth: tokens.buttonWidth4Chars
    signal openButtonsRequested()
    signal enableHidHelperRequested()
    signal removeHidHelperRequested()

    function restoreLogExportFocus() {
        Qt.callLater(function() {
            if (root.Window.window)
                root.Window.window.requestActivate()
            if (deviceOpenLogButton.enabled)
                deviceOpenLogButton.forceActiveFocus()
            else
                diagnosticTraceSwitch.forceActiveFocus()
        })
    }

    FileDialog {
        id: logExportDialog
        objectName: "logExportDialog"
        title: qsTr("导出日志")
        fileMode: FileDialog.SaveFile
        nameFilters: [qsTr("ZIP 压缩文件 (*.zip)")]
        defaultSuffix: "zip"
        onAccepted: {
            SettingsController.exportLogs(selectedFile)
            root.restoreLogExportFocus()
        }
        onRejected: root.restoreLogExportFocus()
    }

    function currentDeviceStateCode() {
        if (!SettingsController.activeRemoteKey)
            return "unselected"
        if (!SettingsController.activeRemoteReady)
            return "pending"
        if (SettingsController.bridgeConnected)
            return "paired"
        return SettingsController.activeRemotePairingState
    }

    function currentDeviceStateText() {
        switch (currentDeviceStateCode()) {
        case "unselected": return qsTr("请选择设备")
        case "pending": return qsTr("接收尚未接入")
        case "selected_missing": return qsTr("所选设备未连接")
        case "checking": return qsTr("正在检查")
        case "paired": return qsTr("已配对")
        default: return qsTr("未检查")
        }
    }

    function currentDeviceDetailText() {
        return SettingsController.activeRemoteLabel
    }

    function currentDeviceStateColor() {
        const code = currentDeviceStateCode()
        if (code === "paired")
            return tokens.successColor
        if (code === "checking")
            return tokens.voiceAccent
        if (code === "unchecked" || code === "pending")
            return tokens.disabledText
        return tokens.errorColor
    }

    function valueIn(value, choices) {
        return choices.indexOf(String(value)) >= 0
    }


    function buttonReceiverStateCode() {
        if (!SettingsController.activeRemoteKey)
            return "unselected"
        if (!SettingsController.activeRemoteReady)
            return "pending"
        if (SettingsController.remoteSelectionBusy)
            return "checking"
        if (SettingsController.chromecastSetupError)
            return "setup_failed"
        if (valueIn(SettingsController.rawInputState, [
                "chromecast_sensitive_logging_disabled", "chromecast_sensitive_logging_enable_failed"]))
            return "setup_failed"
        if (SettingsController.rawInputState === "chromecast_input_payload_unavailable")
            return "input_incomplete"
        if (SettingsController.rawInputState === "chromecast_permission_cancelled")
            return "authorization_cancelled"
        if (!SettingsController.bridgeRunning)
            return "service_stopped"
        return SettingsController.buttonReceiverUsable
            ? "ready" : (SettingsController.rawInputState === "starting" ? "checking" : "error")
    }

    function buttonReceiverStateText() {
        switch (buttonReceiverStateCode()) {
        case "unselected": return qsTr("请先选择设备")
        case "pending": return qsTr("接收尚未接入")
        case "setup_failed": return qsTr("启用未完成")
        case "authorization_cancelled": return qsTr("授权已取消")
        case "checking": return qsTr("正在检查")
        case "input_incomplete": return qsTr("按键数据不完整")
        case "ready": return qsTr("正常")
        case "service_stopped": return qsTr("等待服务启动")
        case "error": return SettingsController.bridgeRunning
            ? qsTr("点击“重启服务”") : qsTr("点击“重新检查”")
        default: return qsTr("未检查")
        }
    }

    function buttonReceiverStateColor() {
        const code = buttonReceiverStateCode()
        if (code === "ready")
            return tokens.successColor
        if (code === "checking")
            return tokens.voiceAccent
        if (code === "pending" || code === "service_stopped")
            return tokens.disabledText
        return tokens.errorColor
    }

    function bridgeStateText() {
        if (SettingsController.activeRemoteKey && !SettingsController.activeRemoteReady)
            return qsTr("接收尚未接入")
        if (SettingsController.bridgeReconnectBusy)
            return qsTr("连接中")
        if (SettingsController.bridgeLaunchBusy)
            return qsTr("启动中")
        if (valueIn(buttonReceiverStateCode(), ["setup_failed", "authorization_cancelled"]))
            return qsTr("请先完成启用")
        if (bridgeNeedsRestartAction())
            return qsTr("点击“重启服务”")
        const receiverCode = buttonReceiverStateCode()
        if (SettingsController.bridgeConnected)
            return qsTr("已连接")
        if (SettingsController.bridgeRunning) {
            const connectionCode = String(SettingsController.bridgeConnectionState)
            if (connectionCode === "retry_wait")
                return qsTr("点击“重新连接”")
            if (connectionCode === "connecting")
                return qsTr("正在连接遥控器")
            if (bridgeReconnectActionAvailable())
                return qsTr("点击“重新连接”")
            return qsTr("正在等待连接")
        }
        if (SettingsController.bridgeLaunchPhase === "unknown"
                || SettingsController.bridgeLaunchPhase === "failed")
            return qsTr("启动失败")
        return qsTr("点击“启动服务”")
    }

    function bridgeReconnectActionAvailable() {
        if (!SettingsController.bridgeReconnectAvailable)
            return false
        const connectionCode = String(SettingsController.bridgeConnectionState)
        if (connectionCode === "retry_wait")
            return true
        if (connectionCode !== "waiting_for_device")
            return false
        return SettingsController.buttonReceiverUsable
    }

    function bridgeNeedsRestartAction() {
        if (valueIn(buttonReceiverStateCode(), ["setup_failed", "authorization_cancelled"]))
            return false
        if (!SettingsController.bridgeRunning)
            return false
        if (SettingsController.hidTapState === "restart_required")
            return false
        if (SettingsController.bridgeRestartRecommended)
            return true
        return buttonReceiverStateCode() === "error"
    }

    function bridgeActionVisible() {
        if (!SettingsController.bridgeRunning)
            return true
        if (SettingsController.bridgeLaunchBusy
                || SettingsController.bridgeReconnectBusy)
            return true
        if (bridgeNeedsRestartAction())
            return true
        if (SettingsController.bridgeConnected)
            return false
        return bridgeReconnectActionAvailable()
    }

    function bridgeActionText() {
        if (SettingsController.bridgeReconnectBusy)
            return qsTr("连接中…")
        if (SettingsController.bridgeLaunchBusy)
            return qsTr("启动中…")
        if (!SettingsController.bridgeRunning)
            return qsTr("启动服务")
        if (bridgeNeedsRestartAction())
            return qsTr("重启服务")
        return qsTr("重新连接")
    }

    function bridgeStateColor() {
        if (bridgeNeedsRestartAction())
            return tokens.errorColor
        if (SettingsController.bridgeConnectionState === "retry_wait")
            return tokens.errorColor
        if (SettingsController.bridgeConnected)
            return tokens.successColor
        const receiverCode = buttonReceiverStateCode()
        if (receiverCode === "setup_failed" || receiverCode === "authorization_cancelled")
            return tokens.errorColor
        if (SettingsController.bridgeLaunchPhase === "unknown"
                || SettingsController.bridgeLaunchPhase === "failed")
            return tokens.errorColor
        if (SettingsController.bridgeReconnectBusy
                || SettingsController.bridgeRunning
                || SettingsController.bridgeLaunchBusy
                || !SettingsController.bridgeRunning)
            return tokens.voiceAccent
        return tokens.disabledText
    }


    RemoteDeviceDialog {
        id: remoteDeviceDialog
        tokens: root.tokens
        anchors.centerIn: parent
    }

    ScrollView {
        id: deviceScroll
        objectName: "deviceScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            id: devicePageContent
            objectName: "devicePageContent"
            width: Math.max(0, deviceScroll.availableWidth
                - tokens.pageHorizontalPadding * 2)
            x: tokens.pageHorizontalPadding
            y: tokens.pageVerticalPadding
            spacing: tokens.spacingMedium

            SectionFrame {
                objectName: "devicePrerequisiteSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                SettingsSectionTitle {
                    objectName: "devicePrerequisiteSectionTitle"
                    tokens: root.tokens
                    text: qsTr("设备状态")
                }

                InlineSettingsRow {
                    objectName: "currentDeviceRow"
                    tokens: root.tokens
                    stateColumnWidth: root.deviceStateColumnWidth
                    actionColumnWidth: root.deviceActionColumnWidth
                    editorColumnWidth: root.deviceActionColumnWidth * 2 + tokens.spacingSmall
                    titleText: qsTr("当前设备")
                    descriptionText: root.currentDeviceDetailText()
                    descriptionNeverElide: false
                    stateText: root.currentDeviceStateText()
                    stateColor: root.currentDeviceStateColor()

                    editorData: [CompactButton {
                        id: openBluetoothSettingsButton
                        objectName: "openBluetoothSettingsButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("蓝牙设置")
                        onClicked: SettingsController.openBluetoothSettings()
                        KeyNavigation.backtab: root.backTabTarget
                        KeyNavigation.tab: selectRemoteButton
                    }, CompactButton {
                        id: selectRemoteButton
                        objectName: "selectRemoteButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("选择设备")
                        onClicked: remoteDeviceDialog.open()
                        KeyNavigation.backtab: openBluetoothSettingsButton
                        KeyNavigation.tab: refreshDeviceChecksButton
                    }]

                    CompactButton {
                        objectName: "refreshDeviceChecksButton"
                        id: refreshDeviceChecksButton
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: DiagnosticsController.isRefreshing
                            ? qsTr("检查中…") : qsTr("重新检查")
                        enabled: !DiagnosticsController.isRefreshing
                            && !DiagnosticsController.vbCableTestRunning
                        onClicked: {
                            DiagnosticsController.refreshDiagnostics()
                            SettingsController.refreshRemoteDevices()
                        }
                        KeyNavigation.backtab: selectRemoteButton
                    }
                }

                InlineSettingsRow {
                    objectName: "buttonReceiverRow"
                    tokens: root.tokens
                    stateColumnWidth: root.deviceStateColumnWidth
                    actionColumnWidth: root.deviceActionColumnWidth
                    titleText: qsTr("按键接收")
                    editorColumnVisible: SettingsController.hidHelperRepairVisible
                        || SettingsController.hidHelperRemovalVisible
                    editorColumnWidth: root.deviceActionColumnWidth
                    descriptionText: SettingsController.rawInputState
                            === "chromecast_input_payload_unavailable"
                        ? qsTr("Windows 没有提供完整按键数据；可尝试重启电脑一次，仍失败请导出日志")
                        : root.buttonReceiverStateCode() === "setup_failed"
                        ? (SettingsController.chromecastSetupError
                            || qsTr("设备准备失败，请在“选择设备”中重新使用此设备"))
                        : root.buttonReceiverStateCode() === "authorization_cancelled"
                        ? qsTr("管理员授权已取消，请在“选择设备”中重新使用此设备")
                        : qsTr("让遥控器按键在电脑上生效")
                    descriptionNeverElide:
                        root.buttonReceiverStateCode() === "input_incomplete"
                    stateText: root.buttonReceiverStateText()
                    stateColor: root.buttonReceiverStateColor()

                    editorData: [CompactButton {
                        objectName: "repairHidHelperButton"
                        visible: SettingsController.hidHelperRepairVisible
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: SettingsController.hidHelperRepairBusy
                            ? qsTr("处理中…")
                            : (SettingsController.hidHelperSetupRequired
                                ? qsTr("启用改键")
                                : SettingsController.hidHelperCleanupPending
                                ? qsTr("完成清理") : qsTr("修复权限"))
                        highlighted: true
                        enabled: !SettingsController.hidHelperRepairBusy
                            && !SettingsController.bridgeLaunchBusy
                        onClicked: root.enableHidHelperRequested()
                    }, CompactButton {
                        objectName: "removeHidHelperButton"
                        visible: SettingsController.hidHelperRemovalVisible
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("移除权限")
                        enabled: !SettingsController.hidHelperRepairBusy
                            && !SettingsController.bridgeLaunchBusy
                        onClicked: root.removeHidHelperRequested()
                    }]

                    CompactButton {
                        objectName: "openButtonSettingsButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("按键设置")
                        onClicked: root.openButtonsRequested()
                    }
                }

                InlineSettingsRow {
                    objectName: "remoteServiceRow"
                    tokens: root.tokens
                    stateColumnWidth: root.deviceStateColumnWidth
                    actionColumnWidth: root.deviceActionColumnWidth
                    titleText: qsTr("遥控器服务")
                    descriptionText: qsTr("连接遥控器，接收按键和语音")
                    descriptionNeverElide: true
                    stateText: root.bridgeStateText()
                    stateColor: root.bridgeStateColor()

                    CompactButton {
                        objectName: "bridgeActionButton"
                        visible: root.bridgeActionVisible()
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: root.bridgeActionText()
                        highlighted: true
                        enabled: !SettingsController.bridgeLaunchBusy
                            && (SettingsController.bridgeRunning || !SettingsController.activeRemoteKey
                                || SettingsController.activeRemoteReady)
                            && !SettingsController.bridgeReconnectBusy
                            && !SettingsController.hidHelperRepairBusy
                            && !DiagnosticsController.vbCableTestRunning
                            && !DiagnosticsController.driverActionRunning
                            && !SettingsController.voiceHotkeyBusy
                            && !SettingsController.endpointPreflightBusy
                        onClicked: {
                            if (!SettingsController.bridgeRunning)
                                SettingsController.startBridge()
                            else if (root.bridgeNeedsRestartAction())
                                SettingsController.restartBridge()
                            else if (root.bridgeReconnectActionAvailable())
                                SettingsController.reconnectBridgeNow()
                        }
                    }
                }

                InlineSettingsRow {
                    objectName: "runtimeLogRow"
                    tokens: root.tokens
                    actionColumnWidth: root.deviceActionColumnWidth
                    titleText: qsTr("帮助")
                    descriptionText: ""
                    showDivider: false

                    editorData: Item {
                        Layout.fillWidth: true
                        implicitWidth: deviceUsageLink.implicitWidth
                        implicitHeight: deviceUsageLink.implicitHeight

                        CompactLink {
                            id: deviceUsageLink
                            objectName: "deviceUsageLink"
                            anchors.right: parent.right
                            tokens: root.tokens
                            text: qsTr("使用方法")
                            KeyNavigation.tab: launchAtLoginSwitch
                            onClicked: SettingsController.openUsageGuide()
                        }
                    }

                }
            }

            SectionFrame {
                objectName: "desktopBehaviorSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                SettingsSectionTitle {
                    objectName: "desktopBehaviorSectionTitle"
                    tokens: root.tokens
                    text: qsTr("通用设置")
                }

                SettingsListRow {
                    objectName: "launchAtLoginRow"
                    tokens: root.tokens
                    titleText: qsTr("随 Windows 启动")
                    descriptionText: qsTr("登录后后台运行")

                    CompactSwitch {
                        id: launchAtLoginSwitch
                        objectName: "launchAtLoginSwitch"
                        tokens: root.tokens
                        checked: SettingsController.launchAtLogin
                        Accessible.name: qsTr("随 Windows 启动")
                        onToggled: {
                            if (checked !== SettingsController.launchAtLogin)
                                SettingsController.setLaunchAtLogin(checked)
                        }
                    }
                }

                SettingsListRow {
                    objectName: "launchBridgeOnAppStartRow"
                    tokens: root.tokens
                    titleText: qsTr("启动程序时自动运行服务")
                    descriptionText: qsTr("自动连接遥控器")

                    CompactSwitch {
                        id: launchBridgeOnAppStartSwitch
                        objectName: "launchBridgeOnAppStartSwitch"
                        tokens: root.tokens
                        checked: SettingsController.launchBridgeOnAppStart
                        Accessible.name: qsTr("启动程序时自动运行遥控器服务")
                        onToggled: {
                            if (checked !== SettingsController.launchBridgeOnAppStart)
                                SettingsController.setLaunchBridgeOnAppStart(checked)
                        }
                    }
                }

                SettingsListRow {
                    objectName: "diagnosticTraceRow"
                    tokens: root.tokens
                    titleText: qsTr("诊断日志")
                    descriptionText: qsTr("记录按键与语音排查信息")

                    CompactLink {
                        id: deviceOpenLogButton
                        objectName: "deviceOpenLogButton"
                        tokens: root.tokens
                        text: SettingsController.logExportBusy ? qsTr("导出中…") : qsTr("导出日志")
                        enabled: !SettingsController.logExportBusy
                        KeyNavigation.backtab: launchBridgeOnAppStartSwitch
                        KeyNavigation.tab: deviceLogLocationLink
                        onClicked: {
                            logExportDialog.selectedFile = SettingsController.logExportDefaultFile()
                            logExportDialog.open()
                        }
                    }

                    CompactLink {
                        id: deviceLogLocationLink
                        objectName: "deviceLogLocationLink"
                        tokens: root.tokens
                        text: qsTr("打开日志位置")
                        Layout.rightMargin: root.deviceActionColumnWidth
                            - diagnosticTraceSwitch.implicitWidth
                        KeyNavigation.backtab: deviceOpenLogButton
                        KeyNavigation.tab: diagnosticTraceSwitch
                        onClicked: SettingsController.openLogLocation()
                    }

                    CompactSwitch {
                        id: diagnosticTraceSwitch
                        objectName: "diagnosticTraceSwitch"
                        tokens: root.tokens
                        checked: SettingsController.diagnosticTraceEnabled
                        Accessible.name: qsTr("诊断日志")
                        KeyNavigation.backtab: deviceLogLocationLink
                        KeyNavigation.tab: appLogSwitch
                        onToggled: {
                            if (checked !== SettingsController.diagnosticTraceEnabled)
                                SettingsController.setDiagnosticTraceEnabled(checked)
                        }
                    }
                }

                SettingsListRow {
                    objectName: "appLogRow"
                    tokens: root.tokens
                    titleText: qsTr("应用日志")
                    descriptionText: qsTr("记录运行日志 app.log；关闭后不再生成")

                    CompactSwitch {
                        id: appLogSwitch
                        objectName: "appLogSwitch"
                        tokens: root.tokens
                        checked: SettingsController.appLogEnabled
                        Accessible.name: qsTr("应用日志")
                        KeyNavigation.backtab: diagnosticTraceSwitch
                        KeyNavigation.tab: closeBehaviorSwitch
                        onToggled: {
                            if (checked !== SettingsController.appLogEnabled)
                                SettingsController.setAppLogEnabled(checked)
                        }
                    }
                }

                SettingsListRow {
                    objectName: "closeBehaviorRow"
                    tokens: root.tokens
                    titleText: qsTr("关闭窗口时隐藏到任务栏")
                    descriptionText: qsTr("保留右下角图标；关闭此项则完全退出")
                    showDivider: false

                    CompactSwitch {
                        id: closeBehaviorSwitch
                        objectName: "closeBehaviorSwitch"
                        tokens: root.tokens
                        checked: SettingsController.closeBehavior !== "quit"
                        Accessible.name: qsTr("关闭窗口时隐藏到任务栏")
                        onToggled: SettingsController.setCloseBehaviorIndex(checked ? 0 : 1)
                        KeyNavigation.backtab: appLogSwitch
                        KeyNavigation.tab: root.tabTarget
                    }
                }
            }

            Item { Layout.preferredHeight: tokens.pageVerticalPadding }
        }
    }
}
