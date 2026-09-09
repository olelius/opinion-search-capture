# APP 模拟器实测与 Android 适配器首版

日期：2026-09-08；实际操作约21:57–22:05（Asia/Shanghai）。仅使用 MuMu 实例0，由本APP执行者独占串行操作。

## 已确认结论

- MuMu 6.5.3.0 / Android15（SDK35）；实际串号 `127.0.0.1:16384`；Manager `is_android_started=true`、ADB `device`、`sys.boot_completed=1`。
- 初查两个包均缺失。通过自带 `com.mumu.store` 的准确条目正常下载：小红书渠道“官方”，B站渠道“B站”。B站曾显示0%，随后完成；最终两个包都已安装，不再以缺包作为当前阻塞。
- 最终版本：`com.xingin.xhs` 9.31.2（9312801）；`tv.danmaku.bili` 9.10.0（9100200）。
- 桌面原始PNG为1920×1080横屏；两个原生App自动转为1080×1920竖屏。未改系统宽高/DPI/虚拟化，未创建、克隆、重置、删除实例，未安装其他模拟器。
- 首次隐私提示已留存；由执行者各完成一次首次使用同意。非必要通知权限均拒绝；小红书额外通知引导进入系统通知设置后原路返回，未启用通知。B站首次偏好信息未填写，使用返回离开；未登录、未输入账号/密码/验证码。
- 所有输入、启动与按钮操作基于实测控件，无硬编码坐标点击。小红书 resource-id 混淆，依赖明确 description/text/class；其输入框无障碍文字为“搜索, 零跑”，视觉显示“零跑”，已针对平台处理该前缀。B站已核实 `expand_search`、`search_fake_text` 入口。

## 关键词“零跑”、请求TOP3的实际结果

| 平台 | 类别 | 状态 | 原因 | 本地证据目录（各含PNG/XML/JSON） |
| --- | --- | --- | --- | --- |
| xiaohongshu | suggestions | partial | page_captured_top_n_and_rank_unverified | `H:\Public Opinion Search Capture System\.local\android\final\20260908-220314-c1b692fb1c` |
| xiaohongshu | results | blocked | login_required | `H:\Public Opinion Search Capture System\.local\android\verified\20260908-220414-204c966522` |
| bilibili | suggestions | partial | page_captured_top_n_and_rank_unverified | `H:\Public Opinion Search Capture System\.local\android\verified\20260908-220404-33d62fa192` |
| bilibili | results | partial | page_captured_top_n_and_rank_unverified | `H:\Public Opinion Search Capture System\.local\android\verified\20260908-220409-7c86b654f5` |

- 小红书下拉词实际出现“零跑a10”“零跑c10”等，同时混有官方账号与商品入口。页面取得，不把文本节点数量作为已验证排名。
- 小红书搜索结果出现“没有找到相关内容”与“登录查看更多精彩内容 / 去登录”；当前记录 `blocked/login_required`。这不是已证实零条业务结果，故 observed_count 为 null，不作空结果成功。
- B站下拉词实际出现“零跑a10”“零跑”“零跑c10”等；搜索结果确有视频卡片，同时混有UP主聚合、推荐/商品内容。首版尚未严证TOP3条目身份、广告口径与排名，所以两类均 partial、rank_verified=false、observed_count=null。
- 本轮不滚动或拼长图，不声称TOP20或稳定批量链路通过。PNG与XML按顺序采集，不是平台冻结快照；动态列表可能在二者之间重排。
- 部分中间报告保留首次失败/阻塞（max_retry设置、未识别首次引导、缺包、输入前缀、RPC断开），不删除或改写历史报告；上表才是最终验收口径。

## 开发范围与能力

仅修改 `src/opinion_capture/android.py`、`tests/test_android.py`、`scripts/probe_android.py`、本文；证据仅存 `.local/android/`。未改其他模块、进度或运行库，未Git提交，未修改全局依赖。

`capture(request: dict) -> dict` 覆盖指定统一字段：platform、client=APP、capture_kind、keyword、status、reason、requested_top_n、observed_count、rank_verified、artifact_paths、captured_at（时区ISO）、source_url、device。默认top_n=20、timeout_seconds=30；输出目录必须绝对路径。

- 单设备自动选择；多设备即使其中一个offline也要求明确serial；精确包名检测。
- 原图使用二进制 `adb exec-out screencap -p`，Pillow校验PNG但不重编码；UUID独立子目录，原图以xb模式写入避免覆盖。
- XML/PNG/JSON保存绝对路径；缺包同样提供设备画面与阻塞报告，但画面不是对应平台业务证据。
- uiautomator2调用位于沿用项目Python的子进程，外层以剩余总时限终止，避免超时后后台继续输入。ADB共用截止时间；进程启动/文件落盘存在少量开销，不是硬实时保证。
- 控件快照偶发 `RemoteDisconnected`：只读dump最多补试一次，不重复导航/点击。仍失败则保留PNG与错误，状态为failed；不伪造XML。
- 适配器不自动接受协议/权限、不自动安装、不代办登录、不解除验证；首次引导需在单独受控会话完成。调用方必须对同一设备串行调度，首版尚无跨进程设备锁。

## 验证与复现

11项离线unittest通过，覆盖设备唯一性/离线/未确认/错误串号、精确包名、截止时间、原始PNG字节保留/覆盖保护/JPEG拒绝、参数校验、实际登录提示与缺包报告。

```powershell
$env:PYTHONPATH='H:\Public Opinion Search Capture System\src'
& 'H:\Public Opinion Search Capture System\.venv\Scripts\python.exe' -B -m unittest tests.test_android -v
& 'H:\Public Opinion Search Capture System\.venv\Scripts\python.exe' -B 'H:\Public Opinion Search Capture System\scripts\probe_android.py' --platform bilibili --keyword 零跑 --capture-kind suggestions --top-n 3 --serial 127.0.0.1:16384 --output-dir 'H:\Public Opinion Search Capture System\.local\android\next'
```

原图尺寸/哈希复核记录：`H:\Public Opinion Search Capture System\.local\android\artifact-verification.json`；测试输出：同目录 `unittest.txt`；Manager/ADB实况：`mumu-info.json`、`devices.txt`。

## 待主智能体接手事项

1. 小红书需用户完成正常登录后重测搜索；本次未触发验证码处理。
2. 定义并实现两个平台的真实条目身份、广告/聚合卡口径、排序与TOP N证据边界；未严证前保持partial。
3. 补设备互斥锁、首次引导与页面稳定检查，不依赖固定一秒等待作为完整加载证据。
4. 运行问题条目由主智能体按写入边界整合：uiautomator2 3.7.0不支持settings[max_retry]；pm path缺包可能非零，已采用pm list packages后精确匹配；RPC只读快照偶发断开已有限补试。未关闭TLS、未绕过下载状态限制。

收尾设备仍运行，前台为小红书搜索/登录提示页；不关机。后续操作由主智能体重新明确串行归属。
