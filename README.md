# 舆情搜索采集系统

面向多平台关键词搜索结果、搜索下拉词及页面截图的采集与整理项目，用于舆情观察和搜索表现对比。

## 当前阶段

采集、归档、拼图和定时核心已完成。本轮合并无头浏览器模式、已登录会话保存反馈、APP80%滚动、WEB截图判定及阶段耗时修复；开发证据和交付边界见下文文档，不重复重跑旧验收。

**当前结果：**两平台APP/WEB的TOP20和下拉词均有真实截图。小红书独立程序已使用正常登录的持久会话完成“零跑、比亚迪”双关键词批次与两类拼图。完整证据及边界见`docs/需求验收矩阵.md`；启动方式见`docs/交付结果.md`。早期未登录/风险提示是历史状态。

## 启动与验证

当前Windows环境已经建立项目`.venv`，复用已有Playwright/Pillow/PySide6并补装设备控制及调度依赖。开发、测试均使用MuMu模拟器，不依赖真机。复现依赖见`requirements-tested.txt`；新机器仍需准备兼容的浏览器内核与Android模拟器。

```powershell
# 打开配置界面；启动本身不执行采集
& 'H:\Public Opinion Search Capture System\scripts\run.ps1' -Action gui

# 仅查看配置与今日触发点
& 'H:\Public Opinion Search Capture System\scripts\run.ps1' -Action preview

# 下列命令仅在准备好会话后手动执行；本轮未开启持续定时运行
# & 'H:\Public Opinion Search Capture System\scripts\run.ps1' -Action run
# & 'H:\Public Opinion Search Capture System\scripts\run.ps1' -Action watch
```

默认本地配置：`H:\Public Opinion Search Capture System\.local\plan.json`；默认输出根目录：同项目`.local\exports`。关键词文件夹附稳定短标识，避免A1/A10、大小写或非法字符清洗后混淆。日期取实际截图时间，APP/WEB下拉词分组隔离；原图与拼图有独立模板，首版导出PNG。

APP设备：先启动MuMu，再点“刷新设备”。程序从运行进程/安装信息定位ADB；失败时点击“选择ADB文件”选一次真实adb.exe，本机记住路径，不依赖固定盘符或系统PATH。多设备按名称选择。命令行`OpinionCapture-CLI.exe devices`可只输出发现结果，不进入平台采集。

开发时只检查受影响路径，不默认重复全量测试。示例（不访问平台）：

```powershell
$env:PYTHONPATH='H:\Public Opinion Search Capture System\src'
$env:PYTHONIOENCODING='utf-8'
$env:QT_QPA_PLATFORM='offscreen'
& 'H:\Public Opinion Search Capture System\.venv\Scripts\python.exe' -m unittest tests.test_final_browser_flow.FinalBrowserFlowTests.test_home_recommendations_and_previous_query_are_not_keyword_suggestions
```

`watch`或GUI“开始计划”须保持工作进程运行。服务已实现持久化触发/FIFO、错过窗口记录、独占锁、取消、有限重试与恢复成功结果复用；延迟窗口由配置控制。停止计划与取消当前批次分开，默认停止后等待在途批次结束，不事后补造历史截图。

GUI已支持设备名称选择、原图/拼图独立模板、字段插入、实际路径预检、任务进度、历史报告与恢复、显式托盘运行。汇总图每组固定一张PNG，网格按“每行关键词数”换行，横排单行；没有人工像素/高度拆图上限，分块写入降低内存占用。WEB会话默认独立保存在`.local/browser-profiles`，不混入图片输出目录；显式打开网页登录由用户正常操作，不把关闭窗口当作已验证登录。

后台无头采集：选择“独立网页会话”，在程序专属窗口首次正常登录，随后勾选“后台无头采集”。已经登录会自动确认并保存会话；登录始终显示窗口。复用已有Chrome为另一种模式，不复制日常浏览器资料，也不把已有窗口转成无头。计划表单需单独保存。

按最新要求不追加重复测试/烟测；便携构建脚本`H:\Public Opinion Search Capture System\scripts\build-portable.ps1`生成全新候选目录并做结构/哈希检查，不自动运行产物。

## 程序定位

- `src/opinion_capture/`：配置、计划、数据库、字段导出、图像处理、APP/WEB适配器与GUI。
- `tests/`：离线单元与集成测试，替身不代表真实平台通过。
- `scripts/probe_*.py`：真实平台或参考素材的最小探针；平台探针会实际操作页面/设备。
- `docs/首轮验证与开发交接.md`：最终8项采集状态、证据与首版边界。
- `.local/`、`.capture-state/`：本地配置、会话、PNG/XML/HTML证据和批次数据库，均由Git忽略。

## 本地资料目录

截图仅保留在本地，不纳入 Git，也不上传远程仓库；以下截图目录在克隆仓库后需另行准备。

| 路径 | 内容 |
| --- | --- |
| `docs/20点B站TOP20/` | B 站搜索结果截图，28 张 |
| `docs/20点B站下拉/` | B 站搜索下拉词截图，28 张 |
| `docs/21点小红书TOP20/` | 小红书搜索结果截图，28 张 |
| `docs/21点小红书下拉词/` | 小红书搜索下拉词截图，28 张 |
| `docs/SEO综合界面/` | 两个平台搜索结果拼接参考图，2 张 |
| `docs/SEO个平台下拉词/` | 两个平台下拉词拼接参考图，2 张 |
| `PROJECT_PROGRESS.md` | 项目当前进度与下一步 |

本地参考素材共 116 张 JPG，约 189.49 MiB；保留原有文件名和目录结构。仓库仅同步项目说明、进度及后续代码等非截图内容。
