# NOTICE（修改声明）

本软件「无线麦Mod」（仓库曾用内部名 *windows-remote-mic-app* Chromecast 剥离版）是基于以下上游项目的**修改作品（modified version）**：

- 上游项目：windows-remote-mic-app
- 上游作者：ZSTDJan — <https://github.com/ZSTDJan/windows-remote-mic-app>
- 上游许可证：GNU General Public License v3.0 **only**（GPL-3.0-only）

## 本修改版版权与修改说明

- 修改部分版权声明：Copyright (C) 2026 BenjmSek
- 修改日期：2026-10
- 主要修改内容：
  1. 移除小米 RC003 遥控器支持（相关代码、配置、界面），仅保留谷歌 Chromecast 遥控器链路；
  2. Python 包名由 `ovb_rc003` 改为 `ovb`；设备档案改为 `config/chromecast-remote.json`；
  3. 产品显示名改为「无线麦Mod」，窗口标题「无线麦Mod win版」；
  4. 新增“应用日志”开关（关闭后停止生成 `app.log`，无需重启）；
  5. 默认窗口尺寸由 560 调整为 600；
  6. 修复遥控器音量 / 静音键在物理键防护下的误杀问题（仅对音量 / 静音三键放行瞬间点按）。

## 许可证适用

根据 GPL-3.0 第 5 条，本作品作为一个整体以 **GPL-3.0-only** 发布；不得将其重新许可为“GPL-3.0-or-later”或任何其他许可证。完整许可证文本见 [LICENSE.md](LICENSE.md)。
