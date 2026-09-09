# 小红书 WEB 完整适配器开发交接

日期：2026-09-08。范围：本分工代码及离线验收收敛；**真实 WEB 仍为上一轮 300012/IP 风险阻塞，未记为平台通过**。

## 1. 已确认的业务口径

- 综合搜索页按独立笔记内容卡计数，含视频笔记；推广卡保留，`promoted=true`。
- 账号聚合、相关搜索和明确聚合模块不计数。身份不明确的候选卡产生 `partial`，不静默跳过后声称严格排名成立。
- `rank_policy="independent_content_cards"`。具体顺序为综合页 feed 的 DOM 序列，以笔记 ID 跨帧桥接；不是把瀑布流按 y 坐标重新排序，也不是平台官方热度排名。
- APP 已登录不代表 WEB 已登录。本分工未复用手机 cookie、通用浏览器会话，也未重新访问真实 WEB、切代理、修改反检测或处理验证码。

## 2. 已实现代码

| 文件 | 责任 |
| --- | --- |
| `H:\Public Opinion Search Capture System\src\opinion_capture\web_xiaohongshu.py` | 请求/状态、采集循环、截图/manifest、唯一最终图、共享预算/取消、专属 profile 和显式登录入口 |
| `H:\Public Opinion Search Capture System\src\opinion_capture\web_results_xiaohongshu.py` | 只读 DOM 观察协议、身份/排名/区间覆盖纯函数 |
| `H:\Public Opinion Search Capture System\tests\test_web_xiaohongshu*.py` | 请求、纯函数、进程控制及可选离线浏览器回归 |

本分工未编辑 config、runner、imaging、GUI、PROJECT_PROGRESS、RUNTIME_ISSUES；未 Git 提交。工作区其它改动由主任务负责，本分工未做清理。

### 搜索结果链路

1. 一次专属浏览器启动、一次搜索页导航。先查明确认证/风险阻塞，再绑定来源域名、搜索 query、输入关键词及综合 tab。
2. 观察独立 `section.note-item` 与 feed；通过 `/explore/{note_id}`、`/search_result/{note_id}` 的 24 位十六进制笔记 ID 交叉核对声明身份，跟踪参数不参与去重。
3. 等待目标 DOM 稳定及可见笔记图片 `complete && naturalWidth>0`；封面缺失、坏图、加载中均不算截图覆盖成功。
4. 保留原生 viewport PNG、截图前后 DOM 摘要及 HTML。快照指纹同时覆盖身份、文本、推广、位置、图片和边界状态；PNG 尺寸须与 viewport 相符。
5. 按当前文档或 feed 的实际滚动容器前进，每步最多约 55% 可视高度。覆盖片段要求整宽可见，处理固定页头/底部覆盖条，合并同一卡片的垂直可见区间。
6. 滚动加载新卡时追加身份。节点回收有序重叠可桥接；无重叠、插队/逆序、身份冲突和跨帧内容/尺寸矛盾保持 `partial`，不凭节点数猜测连续排名。
7. 足量且 TOP N 全部有截图区间证据，可返回 `success`、`rank_verified=true`。不足 N 时，必须在滚动底部连续两帧观察到同一明确末页提示、无加载状态，才认定 `outcome="insufficient_results"`；证据完整时 `status="success"`。零结果也要求明确末页证据。
8. 单纯高度不变、连续滚动停滞不是末页证明。达到预算或保护上限而缺证据时返回 `partial`。

### 最终图不是重新排版的“笔记清单”

- `source_artifact_paths` 保留全部实际原生视口帧。
- `primary_artifact_path` 唯一指向 `primary.png`：多帧按原尺寸无损纵接，保留原生瀑布流、重复页头、重叠内容及 TOP N 外的上下文；单帧同样生成唯一最终 PNG。
- 不裁掉/隐藏平台内容，不重新画标题或笔记卡，不拿首帧冒充完整结果，不声称这是一张无缝全页面长截图。
- manifest 的 `primary_layout` 记录各原始帧在最终图中的坐标。测试逐像素检查最终图各区域与对应原图相同，另有 PNG/HTML SHA-256。
- 只有来源绑定且存在目标观察/明确末页的 success/partial 场景生成最终业务图。认证受限只保留诊断证据，最终图为 `null`；取消/超时发生在合成之前也允许最终图为 `null`，不要自动回退取首帧。
- 单次最多 36 帧，最终图最多 6000 万像素。超出限制明确保留 partial，不强行压缩/删内容拼成“成功”。

### 下拉词

- 仅在专门候选面板 `.sug-container/.suggestion-container` 中提取候选；校验当前输入关联、唯一可见面板、完整显示、内部无滚动截断、候选项显示范围及截图前后稳定。
- 不把搜索历史、热门词、账号区域当候选，不以页面滚动展开下拉词。
- 下拉面板内缺图会影响完整性；背后笔记坏图不影响已完整显示的纯文字下拉面板。
- 下拉词 success 的 `rank_verified=false`，不套用 TOP N 笔记排名含义。

## 3. 主 runner / GUI 集成接口

### 采集

`capture(request: dict) -> dict`：保留旧字段，增加以下兼容字段。

| 字段 | 含义 |
| --- | --- |
| `primary_artifact_path` | 唯一最终业务图的绝对路径或 null；正式导出/拼图只取此字段 |
| `source_artifact_paths` | 原始视口 PNG 的绝对路径列表；仅保留证据，不再逐一作为最终导出图 |
| `artifact_paths` | 全部已登记原图、HTML、manifest 和最终图；不是业务图候选列表 |
| `items` | 已观测去重笔记序列；`rank`、`counted` 标明 TOP N 边界，保留 `id/href/title/text/promoted/screenshot_refs` |
| `rank_verified` | 已有证据证明本次 TOP N 前缀/明确末页及截图覆盖；未通过时 rank 仅为观察顺序 |
| `observed_count` | 去重观测数，可大于 N；blocked 时为 null |
| `captured_count` | 结果采集中 TOP N 已完整截图覆盖条数 |
| `rank_policy` | `independent_content_cards`；细节见 evidence 的 versioned policy |
| `outcome` | `top_n / insufficient_results / suggestions / incomplete / blocked / cancelled / timeout` |
| `captured_at_range` | 第一帧开始到最后一帧结束的带时区时间区间 |
| `started_at / finished_at` | 任务开始/结束；`captured_at` 保持实际截图时间兼容字段 |

请求默认 `timeout_seconds=180`，支持 `(0,1800]` 的有限数字，布尔值不作为数字。`cancel_file` 可省略/null，提供时须为绝对路径；文件存在立即取消并返回 `status="cancelled"`。不删除调用方取消文件。

总预算由独立子进程监督：启动、导航、等待、截图、合成共用一个 monotonic 截止时间；驱动 evaluate 卡住也可被终止。父进程约 50ms 检查取消，不给诊断截图另开额外交互预算。到期后仍有 OS 终止/回收及本地清单写入开销；不是毫秒级硬实时承诺。最终替身测试：0.5s 预算约 0.703s 返回，0.5s 触发取消约 0.797s 返回，子进程均已回收。

同一 profile 在适配器内还有 OS 文件锁，进程结束后自动释放；配合主进程已有设备/profile 锁使用。默认新建专属 profile，复用须明确传 `user_data_dir`。

### 显式可见手动登录

```python
from opinion_capture.web_xiaohongshu import open_login

# 只在用户点击 GUI 的 WEB 登录按钮后，在工作线程执行。
result = open_login(
    profile_dir=r"H:\Public Opinion Search Capture System\.local\web_xiaohongshu\profiles\manual-web",
    timeout_seconds=1800,
    cancel_file=r"H:\Public Opinion Search Capture System\.local\web_xiaohongshu\manual-login.cancel",
    output_dir=r"H:\Public Opinion Search Capture System\.local\web_xiaohongshu\manual-login",
)
```

- 完整签名：`open_login(profile_dir: str, *, timeout_seconds: float=1800, cancel_file: str|None=None, output_dir: str|None=None) -> dict`。
- 使用 `headless=False` 的专属持久 context，首次目录必须为空或带匹配的本适配器标记；禁止接管常见通用 Chrome/Edge User Data 目录，也不连接现有通用浏览器。
- 只导航一次，由用户正常操作并关闭窗口；不自动填凭据。关闭后返回 `outcome="manual_session_closed"`，reason 明确 `authentication_not_asserted`，不宣称登录成功。
- 风险页/验证码立即 blocked，登录墙本身在手动入口可等待用户操作。正常关闭窗口的瞬间驱动断连按关闭处理。
- 下一次 `capture` 的 `user_data_dir` 传相同目录。APP 登录态不用于 WEB。
- 此轮只用替身检查可见启动参数、单次导航、关闭/阻塞分支；**未实际调用该入口打开平台登录窗口**。主 GUI 统一 login 桥接仍归主任务。
- 当前监督子进程已验证 Python 源码运行入口。冻结发行包若将 `sys.executable` 替换为 GUI EXE，需要主打包链路提供对应 worker 入口；本分工未把冻结包启动记为通过。

## 4. 离线验证与证据

**74 项离线测试通过**：原接口 17、纯函数/控制 34、子进程/手动入口控制 6、真实浏览器运行的离线 DOM 17。最终清单一致性修订后，控制路径 6 项再次通过。

覆盖 DOM 异常、重复/歧义身份、推广、聚合排除、节点回收、乱序插队、坏图/延迟图、局部遮挡/多帧覆盖、内部滚动容器、滚动追加新笔记、末页不足/零条、停滞不等于到底、下拉截断/隐藏/多面板以及取消/总期限。

离线浏览器只对 `fixture.invalid` 使用本地 `route.fulfill`，context 设置 offline、阻止 service worker，所有其它请求 abort。测试里扩充来源白名单只作用于测试进程，生产入口不接受 fixture 开关。夹具从零创建并显示 “OFFLINE FIXTURE / 人工 DOM，非平台验收”，未删改真实平台内容。

最新证据：

- 总览与历史原图不变校验：`H:\Public Opinion Search Capture System\.local\web_xiaohongshu\goal-20260908-complete\verification-summary.json`。
- 74 项日志：`H:\Public Opinion Search Capture System\.local\web_xiaohongshu\goal-20260908-complete\tests-latest.txt`。
- 最终控制复测：`H:\Public Opinion Search Capture System\.local\web_xiaohongshu\goal-20260908-complete\control-final.txt`。
- 全套 DOM/PNG/manifest 与网络拦截审计：`H:\Public Opinion Search Capture System\.local\web_xiaohongshu\goal-20260908-complete\offline-dom-4b2ba62800`。

代表性结果：TOP5 共观测 8 张独立卡、2 帧、success；滚动追加用例初始 6 卡→观测 9 卡，TOP8 共 3 帧、success；明确末页 2/5 及 0/5 返回 success + insufficient_results；坏图、停滞与下拉截断保留 partial。人工查看最终 TOP5 多帧图和完整下拉词图，确认文字可见、原生布局未重新排列，帧间重复内容如实保留。

对上一轮两份真实错误页 HTML 仅做离线静态解析，并结合已保存 URL 重判，仍为 access_restricted。原有 2 份 PNG、2 份 HTML、2 份 manifest 共 6 个文件 SHA-256 均未改变。

复现命令（只执行本分工测试）：

```powershell
$env:PYTHONPATH='H:\Public Opinion Search Capture System\src'
$env:PYTHONIOENCODING='utf-8'
$env:OPINION_XHS_OFFLINE_BROWSER_TESTS='1'
& 'H:\Public Opinion Search Capture System\.venv\Scripts\python.exe' -B -m unittest `
  tests.test_web_xiaohongshu tests.test_web_xiaohongshu_results `
  tests.test_web_xiaohongshu_control tests.test_web_xiaohongshu_dom -v
```

省略 `OPINION_XHS_OFFLINE_BROWSER_TESTS=1` 时跳过 17 项浏览器夹具测试，默认测试不启动浏览器。所有证据位于已忽略的 `.local`，未同步截图/会话。

## 5. 外部实测边界与下一步

**已实现**：上列源码和状态分支；**已离线通过**：人工 DOM 的真实渲染、算法及控制协议；**外部实测阻塞**：真实小红书 WEB 300012/IP 风险，真实搜索结果和下拉词 DOM 仍未取得。

选择器沿用现有适配器候选结构并用人工 DOM 扩展验证，不构成当前线上 DOM 的确认。真实页面可能存在其它聚合样式、tab 类名、末页提示和虚拟化行为；遇到未证明结构会 partial，不自动放宽排名校验。

主任务先接统一 login 桥接；正常 WEB 会话准备由用户显式触发。风险条件实际解除且用户重新明确发起时，再一次性核对真实 DOM/图片/排名与最终图。当前不自动探测恢复，不把 APP 已登录推断成 WEB 可访问，也不将暂时 blocked 缩小成永久占位需求。
