# 来源与改动说明

本文件说明本仓库相对上游 [`ZSTDJan/windows-remote-mic-app`](https://github.com/ZSTDJan/windows-remote-mic-app)
的来源关系与做了哪些改动，供 GPL-3.0 第 5 条「显著改动声明」与使用者核对用。

## 授权链

| 层 | 项目 | 许可 |
| --- | --- | --- |
| 上游 | [`ZSTDJan/windows-remote-mic-app`](https://github.com/ZSTDJan/windows-remote-mic-app) | `GPL-3.0-only` |
| 上游的上游 | [`nijez/open-voice-bridge`](https://github.com/nijez/open-voice-bridge)（Windows RC003 实现） | `GPL-3.0-only` |
| 本分支 | 本仓库 | `GPL-3.0-only` |

`src/ovb/atvv_protocol.py` 另有独立出处说明：它是 Android TV Voice-over-BLE（ATVV）
协议的净室重实现，参考了 GPL-3.0-only 项目
`xxb26553663-star/remote-bridge-hub @ 8a93f321ac71a602300c6cd77f7256fa4b63068e` 的 ATVV
bridge 实现。该模块只取互操作事实（GATT UUID、控制 opcode、IMA/DVI ADPCM 表），未复制
上游源码行。该段说明以模块内 docstring 为准。

## 本分支起点

- 本分支版本：`src/ovb/VERSION` = `0.1`
- 上游对应 tag：`v1.0.91`（commit `f238a195d1b31d7b66e2470c073038b4213f6185`，
  2026-10-13，`release: publish 1.0.91 source snapshot`）
- 起点若不是该 tag，请把上面那行改成实际 commit 后再发布。

版本号从上游的 `1.0.91` 重置为 `0.1`：本分支是独立的发布序列，沿用上游版本号会让人误以为
这是上游的第 1.0.91 版。上游基线仍是 `v1.0.91`，上面那行不改。

## 改动范围

相对上游 v1.0.91 的 `apps/windows/rc003/src/ovb_rc003/`，本分支做了下面几类改动。
模块级差异以实测为准：上游该 tag 下 129 个 `.py`/`.qml` 文件，本分支 130 个，两者同名
94 个；上游独有的 35 个、本分支独有的 36 个中，大部分是同一个文件因目录层级不同被算成
两个名字（上游 `qml/X.qml` vs 本分支 `qml\X.qml`），真正的内容差异如下。

**1. 包改名与目录扁平化**

- Python 包 `ovb_rc003` → `ovb`，从上游的 `apps/windows/rc003/` 三层结构提到仓库根。
- 设备页、按键页、语音页等 26 个 QML 文件随包一起上移。

**2. 只保留谷歌 Chromecast 链路**

- 随包只保留一个设备档案 `config/chromecast-remote.json`；上游的小米 RC003 档案、
  `Resources/RC003-remote-photo.png` 与相关代码不在本分支中。
- 移除的设备相关模块：`rc003_doubao_session.py`、`battery_monitor_winrt.py`、
  `connection_supervisor.py`、`identity.py`、`key_testing.py`、`application_update.py`
  （上游内置更新通道）及 `qml/BatteryGauge.qml`、`qml/FormField.qml`、
  `qml/DiagnosticResultRow.qml`。
- `src/ovb/settings_qt_classes.py` 由上游 `settings_ui.py` 拆分重构而来；
  `src/ovb/physical_key_tristate_windows.py`、`src/ovb/system_playback_mute.py` 为本分支
  新增。

**3. 新增：元素导航子系统**

`src/ovb/script/` 下 6 个模块（`element_navigation_*`、`element_targeting_core`、
`spatial_navigation_core`）是从上游独立项目 OrthoFocus 导入的源码副本。它们不 import
`ovb`，也不使用 `REMOTE_MIC_*` 环境变量，由 `tests/test_element_navigation_export.py`
断言这一隔离。上游 OrthoFocus 仓库见
[`ZSTDJan/orthofocus`](https://github.com/ZSTDJan/orthofocus)。

**4. 行为改动**

- 语音键链路：新增「关闭音频发送」开关（设置项 `disable_audio_sending`），打开后不再
  打开音频发送端点，直接发按键，并在更早的阶段丢弃 BLE 音频。
- 语音键自愈：针对「按过一次物理修饰键后语音键永久失效」的陈旧按下状态死锁，加入有界
  的自愈与放弃上限。
- 退出清理：加入有界放弃上限，避免退出时必然超时。
- 提权自启动：改为走计划任务（`/rl HIGHEST`），因为 `Run` 键永远不能提权。
- 日志目录收敛到仓库根的 `log/`。

上述每一项的验证深度不同，未经真机实测的部分在 `README.md` 的「Known gaps」一节逐条
列出。

## 未包含的内容

- 上游 `build/`、`installer/`、`docs/`、`apps/windows/orthofocus/` 整棵子树。
- 上游的 GitHub Actions、Issue/PR 模板与项目控制记录（`.codex`、`AGENTS.md`）。
- 上游 `docs/screenshots/`、`docs/images/` 下的界面截图与 QQ 群二维码。因此本分支
  README 不含截图，改用文字说明操作步骤。
