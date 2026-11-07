# 变体 Chromecast 遥控器支持分支（PID 9450 / REV 0110）

这是 [BenjmSek/windows-remote-mic-app](https://github.com/BenjmSek/windows-remote-mic-app)（无线麦Mod）的一个修复分支，目标是让 **谷歌 Chromecast 遥控器的 1 字节报告变体**（`VID 18D1 / PID 9450 / REV 0110`）在 Windows 上实现完整按键 + 语音输入。上游合并后本分支即可退役。

## 背景

两只同款 Chromecast 遥控器（蓝牙指纹完全相同）在原版程序上完全无响应：
- 任何按键在系统与程序里均无反应（遥控器本身配对连接正常）；
- 按住语音键遥控器 LED 常亮，电脑端无任何动作。

逐层抓取实时数据后定位出五个互相独立的问题，全部修复后两只遥控器按键、语音（ATVV → ADPCM → VB-CABLE → 输入法）全链路跑通。

## 五个修复

| # | 文件 | 问题 | 修复 |
|---|------|------|------|
| 1 | `chromecast_hid_tap_windows.py` | 该变体的按键 HID 报告为 **1 字节**（内容即 `CHROMECAST_REPORT_CODES` 键码），tap 只放行 8/2 字节，全部 `length_rejected` | 放行 `size === 1` 并垫成 8 字节形状 |
| 2 | `chromecast_device_windows.py` | ATVV 特征句柄与本机不符（41/43/46 vs 白名单 0x39/0x3B/0x3E）→ `unsupported_layout`，语音会话永远起不来 | 特征本就按 UUID 查找，删除硬编码句柄白名单，`voice_attributes` 改为动态句柄映射 |
| 3 | `chromecast_voice.py` | 该变体的 CAPS_RESP 为 **10 字节**（`0B 01 00 02 03 00F0 01 00 1A`，协议字段全部合规），`len == 9` 校验误杀 | 放宽为 `len >= 9` |
| 4 | `chromecast_worker.py` | Realtek 蓝牙适配器（实测 Ugreen 5.4 / VID_0BDA B851）**不产生蓝牙详细 ETW 事件**，间接通知模式下语音通知全灭；且 GATT 枚举偶发返回不完整特征表 | VoiceReceiver 强制 `direct_notifications=True`（WinRT CCCD 直订）；open 失败自动重建重试 |
| 5 | `chromecast_voice_receiver.py` | 兜底：若遥控器不发 `0x04` AUDIO_START 直接开始发音频，录音门永远不开 | gate 空闲时收到音频即合成一次 AUDIO_START（本机变体实际会发标准 START，此为保险） |

## 使用提示

- 语音需要在语音页保持 **"关闭音频发送"开关为关**（该开关会丢弃全部蓝牙音频，界面却一切正常，是最隐蔽的"无声"原因）。
- 服务首次启动若语音无反应，等几秒（自动重试）或按一下遥控器任意键唤醒后再试。

## 致谢

- [BenjmSek](https://github.com/BenjmSek) / [ZSTDJan](https://github.com/ZSTDJan) —— 无线麦本体与 Chromecast 链路
- [b0o/ATVVoice](https://github.com/b0o/ATVVoice) —— ATVV 协议逆向报告（GET_CAPS / MIC_OPEN / ADPCM / 保活）
- [VincentKingHsu/vRemoter](https://github.com/VincentKingHsu/vRemoter) —— 证明同款 PID 9450 遥控器可被标准蓝牙栈驯服
