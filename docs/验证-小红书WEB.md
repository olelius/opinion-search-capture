# 小红书 WEB 最小实测与首版适配器

日期：2026-09-08；结论：**两项真实实测均 blocked，适配器与离线验证已完成，平台目标采集尚未通过。**

## 实测结果

环境：Windows；仓库 `.venv` 复用已有 Python 3.11/Playwright/Pillow；采用本机 Playwright Chromium 1140，1440×1080 无头视口。每项创建独立专属 profile，不连接现有浏览器，不操作手机。未安装依赖、未下载浏览器、未切换网络、未执行网络重试。

| 请求 | 实际现场 | 状态 | 截图时间（+08:00） |
| --- | --- | --- | --- |
| 零跑 / results / TOP3 | 搜索地址重定向到安全限制页；错误码 300012，提示 IP 存在风险 | blocked / access_restricted | 21:57:38 |
| 零跑 / suggestions | explore 入口同样转到安全限制页；输入关键词前受阻 | blocked / access_restricted | 21:58:08 |

两项 `observed_count=null`、`rank_verified=false`。HTTP 200 仅表示错误页成功返回，不等于业务成功。下拉词请求中关键词已记录为“零跑”，但**未完成平台搜索框输入，也未观测到实际下拉词**。TOP3 同样未取得笔记卡片，不把零条或诊断图作为目标结果。

通过 `view_image` 检查了两张实拍原图：均为“安全限制 / IP存在风险 / 300012”页面，无目标内容。Pillow 验证均为完整的 1440×1080 PNG；两次独立截图画面相同，SHA-256 同为 `896ba0264302018dcbb856e14a30dbd491754c140ab05bd7dcb4fbf21d3a540c`。未修改或拼接原图。

### 现场证据（仅本地）

- 汇总：[probe-result.json](H:/Public%20Opinion%20Search%20Capture%20System/.local/web_xiaohongshu/probe-result.json)
- 搜索原图：[original.png](H:/Public%20Opinion%20Search%20Capture%20System/.local/web_xiaohongshu/captures/20260908T215735-66807887e3/original.png)
- 下拉词入口原图：[original.png](H:/Public%20Opinion%20Search%20Capture%20System/.local/web_xiaohongshu/captures/20260908T215738-c9fb0abfb2/original.png)
- 每张图同目录保存真实 `page.html` 与 `evidence.json`，包含截图前后 DOM 摘要、实际来源 URL、请求 URL、浏览器与 profile 信息；两项均 `artifact_role=diagnostic`、`stable=true`。
- `.local/web_xiaohongshu/` 的 profile 和临时文件不进入 Git；已核对忽略规则。进程查询未发现残留的本适配器 Chromium 进程。

## 已开发范围与保守边界

- `capture(request: dict) -> dict` 覆盖约定的 13 个返回字段；参数校验、时区时间、独占 profile 锁、浏览器选择、共享交互截止时间、错误返回、原始 PNG/HTML/JSON 证据落盘已实现。
- 默认 `top_n=20`、`timeout_seconds=30`。相对输出路径/profile、空关键词、非法种类、布尔/非正数 N、无限/非法超时均校验。可复用的新 profile 必须为空或带本适配器专属标记；已有普通浏览器目录不接管。
- 最多一次浏览器**启动**纠正；页面导航和搜索输入不进行网络重试。验证码、明确登录墙、风险限制直接记录 blocked。错误页 URL 中的 300012 可在文本尚未渲染时提前识别。
- 搜索结果仅统计当前视口内完整显示、未被中心遮挡的候选笔记卡片；目前没有滚动长图或经过真实验收的瀑布流排名口径。即使观察数量达到 N，仍返回 partial、`rank_verified=false`。
- 下拉词只匹配专门候选容器，关键词、来源域名、完整面板覆盖、图片加载、截图前后 DOM 稳定均满足才可能 success；截断面板为 partial。成功场景选择器仍待平台实测，不宣称已通过。
- 原图唯一目录、不覆盖；`artifact_paths` 包含诊断证据，调用方应先检查 status，并读取证据角色。**不要把列表中的 PNG 一律送入业务拼图。**
- 超时是浏览器交互预算；现场保存最多另给截图 5 秒，并有驱动/浏览器清理开销，不宣称为强制进程级硬截止。
- 临时驱动目录与默认 profile 统一位于 `.local/web_xiaohongshu/`；建议调度器以独立工作进程调用同步适配器，同一专属 profile 串行使用。

## 命令与验证

在 `H:\Public Opinion Search Capture System` 中执行：

```powershell
$env:PYTHONPATH = 'H:\Public Opinion Search Capture System\src'
& 'H:\Public Opinion Search Capture System\.venv\Scripts\python.exe' -B -m unittest tests.test_web_xiaohongshu -v
& 'H:\Public Opinion Search Capture System\.venv\Scripts\python.exe' -B 'H:\Public Opinion Search Capture System\scripts\probe_web_xiaohongshu.py' --keyword '零跑' --top-n 3 --timeout-seconds 30
```

脚本还支持 `--kind results` 或 `--kind suggestions`。手动重跑生成新证据目录，原子更新最近一轮 `probe-result.json`；全部 success 返回 0，包含 partial/blocked/failed 返回 2。此处仅实跑两项各一次，未因 blocked 再次请求平台。

离线 unittest **17 项全部通过**，覆盖请求默认值/非法输入/不修改原请求、JSON/时区合同、写入失败返回、登录导航与登录墙区分、验证码/风险分类、300012 URL 提前识别、目标域名/关键词检查、TOP N 保守判定、DOM 漂移与下拉面板完整性。默认不启动浏览器、不联网、不创建平台替代页面。

首次实测的下拉词输入定位因重定向等待至 30 秒预算；随后补充错误页 URL 识别，并将单次输入定位上限缩至 1.5 秒。已离线重判本轮两份真实 DOM 摘要，均仍为 blocked；**修订后的交互路径未重复联网验证**。首次终端 stdout 使用 GBK 导致中文显示乱码，但 UTF-8 JSON 与原图正确；探针现显式设置 UTF-8 stdout。受本次写入范围限制，未修改进度或运行问题库。

## 下一步

用户已确认本轮两项均为平台明确的 IP 风险错误 300012，线上验证正式停止。后续仅继续离线开发与集成；不自动复测、不更换网络、不采取规避措施。保留现有两条 blocked 及全部原始现场，恢复线上验证须由用户重新明确发起。

由使用者通过平台正常流程处理当前风险限制；本轮证据不足以认定具体触发原因。恢复访问后，用本适配器新建/专属会话进行一次真实复测，核实结果与下拉 DOM 结构，再验收排名、TOP N 覆盖和滚动长图。不要将当前 blocked 解释为登录必然可解决，亦不宣称其它网络或浏览器已验证可行。

本次只新增约定的适配器、测试、探针、报告四个文件及 `.local/web_xiaohongshu/` 本地产物；未改依赖、进度、其它模块，未 Git 提交。
