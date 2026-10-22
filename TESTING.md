# Testing

本文件说明测试怎么跑、跑的是什么，以及哪些东西**没有**被测试覆盖。源码
`src/ovb/__init__.py` 与 `src/ovb/__main__.py` 引用本文件。

## 跑测试

```powershell
python scripts/run_tests.py -j 4 --no-confirm
```

只用标准库，不需要 pytest。runner 按 target 分进程，重模块按类分组再切分；崩溃的 target
会单独重试、再按类切、再按用例切，用来区分「负载下不稳定」和「这个模块不能和自己共享
进程」。因为机器忙而变红的用例会单独重跑，单跑能过的单列成 flaky，不算失败。

**不要设 `REMOTE_MIC_DEV_SESSION=1`。** 加了会造成约 122 个假失败。不加它时测试仍可能写
仓库内的 `config\`，所以跑完测试后不要直接把 `config\` 提上去（见仓库根 `.gitignore`）。

本机基线（Python 3.14.7，Windows 10，2026-10-22）：**3510 个测试，failing=0，flaky=0，
crashed=0**，约 104 秒。

## 覆盖的是什么

- 纯协议／字符串逻辑（HID 报告解码、ATVV 编解码、配置读写、按键映射）：跨平台可跑。
- Windows API 相关模块：用 `tests/fakes/` 下的假实现（假 WinRT、假 Raw Input 等）在进程内
  打桩验证，**不注入真实按键，不打开真实音频设备**。
- QML 界面：offscreen 加载与几何断言。

## 不覆盖什么

1. **真机。** 本仓库及其测试里不存在任何设备配对／控制行为。遥控器按键、语音、BLE 音频
   的真实行为只能靠人工在真机上验证。
2. **音频端点。** 不打开真实的 VB-CABLE 或系统采集设备，播放链路用桩。
3. **Frida 注入。** Gadget 的加载与脚本执行不在自动化测试里。
4. **元素导航导出。** `src/ovb/script/` 的导出脚本与模板不在本仓库，
   `tests/test_element_navigation_export.py` 在模板缺失时整体 skip。

未经真机实测的部分逐条列在 [`README.md`](README.md) 的「Known gaps」一节。
