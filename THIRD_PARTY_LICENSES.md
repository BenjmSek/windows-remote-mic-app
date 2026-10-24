# 第三方组件与素材许可

本项目本身基于上游 [ZSTDJan/windows-remote-mic-app](https://github.com/ZSTDJan/windows-remote-mic-app) 按 **GPL-3.0-only** 修改发布，并使用了以下第三方组件。各组件按其各自的许可与授权使用，许可文本以各自上游发布为准。

| 组件 | 用途 | 许可 |
|---|---|---|
| PySide6 / Qt | GUI 框架 | LGPL-3.0（也可选 Qt 商业许可） |
| Frida | 运行时注入 / HID tap 通道 | MIT 或 Apache-2.0（二选一） |
| frida-gadget（预编译） | 随源码分发，位于 `src/ovb/frida_assets/` | 同上（Frida 许可） |
| VB-CABLE | 虚拟音频桥接（需用户自行安装，不随源码分发） | 免费软件，非开源驱动 |
| Python 3.13 / 3.14 | 运行环境 | PSF License |
| 搜狗 / 微信 / 豆包 / Chatterfly | 语音输入目标应用 | 各自厂商许可，与本项目无关 |

说明：

- 本仓库除随源码分发的 frida-gadget 预编译二进制外，不包含上述组件的完整源代码；其完整许可与源码以各自上游发布为准。
- 本修改版移除的小米 RC003 相关代码、配置与界面，其版权仍归上游原作者所有，按 GPL-3.0-only 提供。
- 本项目按“原样”提供，作者不对使用后果负责。
