# 参与贡献

这个仓库维护无线麦 Windows 客户端的**谷歌 Chromecast 遥控器**分支。上游
[`ZSTDJan/windows-remote-mic-app`](https://github.com/ZSTDJan/windows-remote-mic-app)
另有自己的维护入口；小米遥控器（RC003）链路不在本仓库。

## 报告问题

提交前请先搜索现有 Issue，并核对最新正式版是否已修复。普通问题使用 Issue 模板；安全漏洞
按 [`SECURITY.md`](SECURITY.md) 私密报告。

问题报告请写清：

- 使用的程序版本和下载来源；源码运行时附 commit；
- Windows 版本、遥控器型号、配对状态和相关输入法或应用；
- 最短复现步骤、实际结果和预期结果；
- 已执行的相关检查及其真实结果。

**不要上传真实蓝牙地址、HID 路径、完整用户配置、个人绝对路径或未脱敏日志。**

## 本地开发

在 Windows PowerShell 中进入仓库根：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
python scripts\run_tests.py -j 4 --no-confirm
```

跑全量测试时**不要设 `REMOTE_MIC_DEV_SESSION=1`**，那会造成约 122 个假失败。测试可能写
仓库内的 `config\`，所以跑完后不要把 `config\` 提上去（`.gitignore` 只放行
`config/chromecast-remote.json`）。

## 提交修改

- 保持改动围绕一个明确问题，不顺带重构无关模块；
- 新增依赖时同步更新锁定版本、`THIRD_PARTY_NOTICES.md`、`THIRD_PARTY_SOURCE.md` 和
  `ASSET_LICENSES.md`；
- 不提交用户数据、日志、构建目录、下载的第三方二进制或签名材料；
- 行为变化应补自动测试；真实硬件未验证时，把该检查点明确写进 `README.md` 的
  「Known gaps」一节，不要写成已实现；
- Pull Request 说明问题、方案、验证结果和仍未完成的人工项。

## 许可

向本仓库提交代码或文档，表示贡献者有权提供这些内容，并同意按项目现行的 `GPL-3.0-only`
许可证发布。第三方代码和素材必须同时保留原始许可与归属。
