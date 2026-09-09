# APP严格TOP N、原生长图与下拉词开发交接

## 2026-09-09 Android 80%最终交接

- 两APP TOP20成品均已完成；停止本切片设备操作，MuMu实例0保持运行，成功项不重跑。没有操作浏览器、Git、打包或子agent。
- 仅修改 `src/opinion_capture/android.py`、`android_results.py`、`android_image_evidence.py`、新增 `tests/test_android_scroll80_slice.py` 及本文；config/GUI/WEB/session/runner/PROJECT_PROGRESS由主agent负责。
- 正常手势为有效内容区90%→10%，目标80%，没有360px上限；接近TOP N按剩余条数及本帧密度缩短。最终复用原ADB，以距离确定时长，名义速度至多450px/s，避免快速释放造成大幅惯性；没有保留失败的UI多点RPC手势通道。
- `next_scroll`记录端点、目标距离、时长及命令状态；`scroll_measurement`/`rank_links`另外记录已验证内容位移、重叠及参考视口。命令完成不等于内容位移恰为80%；命令中断保留`interrupted_or_unknown`。
- 小红书跨帧卡片仅在原生结构、同列唯一候选、尾部标题/作者及至少96px原RGB一致重叠均成立时补证；前帧已经有身份时还要求身份一致。保留每片实际可见源矩形，不重写PNG/XML。严格模式、身份/排名/未知区域/无缝拼接门禁保留；缺证片段已滚过当前视口时立即停止，避免超过20条后无效前滚。
- 默认视觉模式也等待完整封面加载。修复高分辨率低纹理照片的误判：保留原边缘阈值与分块条件，增加64px统一尺度分析；原图及hash不变，纯色、轻微渐变、单个小字仍不作为已加载照片放行。
- 搜索入口使用明确的返回/搜索/宽查询框同父同行关系，不依赖固定y=42–79；已在前台的APP不重复launch，已识别结果查询框时不退点提交按钮。导航入口前后XML保留。客户缺失XML的界面未声称等同本机界面，其余列表页头规则未泛化。
- 父进程接入 `phase(device_ready/prepare_search/screenshot/validate_capture)`，无上下文为空操作；定点确认没有增加平台调用。

### 最终产物与实测

以下路径相对 `.local/android/scroll80-slice-20260909-124300/`；全部仅本地保留。

| 平台 | 最终结果 | 手势目标 / 已验证内容位移 | 成品及报告 |
| --- | --- | --- | --- |
| 小红书“零跑” | TOP20、rank_verified=true；1080×10270；同次实拍前8帧离线补证成功 | 正常1306/1368px；实测1315及1374–1383px。页头收起首步重叠395px，后续327–336px（约19.1%–19.6%） | `xiaohongshu/20260909-125920-caec2e6c18/derived/results-long.png`；`result-coverage-verified.json` |
| B站“比亚迪” | 本次capture直接success / top_n_verified；20条、rank_verified=true；1080×8384；6帧 | 正常1375/1431px；实测1382/1435/1356/1443px；重叠407/354/433/346px。尾段859px目标、865px实测，重叠924px | `bilibili/20260909-130430-ba44172875/derived/results-long.png`；`result.json` |

小红书原始`result.json`仍为失败历史：已识别超过20条，但同身份卡片前后都不完整的补证分支缺失，导致覆盖门禁不通过；此次只用其前8个不可变PNG/XML重新解析、逐帧验证、排名和拼接，未重采或更改原报告。新报告明确`offline_revalidation_of_same_capture`及原报告路径；它不是一次新增的capture成功。B站为最终源码下新capture直接完成。两份成品已查看整图/接缝，PNG可解码，产物及源PNG/XML哈希核对一致，排名1–20和连续源矩形完整。

### 最小检查与保留边界

- 新增15项用例按改动增量执行通过；另针对本次变更运行封面等待与5类合成封面两项既有定点。后者曾暴露原分类器已存在的低纹理图误判，统一尺度修正后通过。3个源码模块及新增测试AST通过；未全量测试、冻结烟测或重跑成功平台。
- 失败源证据均保留：小红书`124546`无可靠重叠（没有共同身份，整幅最佳误差30.71，右列58.72，首帧还有白色占位）；`125532`入口未打开；`125730`低纹理实图误判；B站`124737`UI RPC断开、只读诊断共同卡片位移413px。具体惯性/加载占比及RPC根因未编造。
- 原360px成功基线 `.local/completion/app-scrollshot-delivery-20260909-011508/summary.json`保持不变。当前主agent可接续打包/Git，无头与登录保存由主agent已有实测负责，本切片不追加平台操作。

## 2026-09-08 历史交接（不代表上述当前状态）

日期：2026-09-08。按用户“不要重复测试/烟测”要求结束本轮；已停止设备操作并向主智能体让出MuMu实例0。未Git提交，未修改config/runner/progress/imaging.py。

## 1. 最终状态：实现与验收分开

### 已实证

- B站“比亚迪”TOP3：成功，3个独立视频/推广内容卡，账号聚合与直播不计数，原生矩形图1080×1655，已视觉检查。证据：`.local/android/goal-live-stage5/20260908-222713-0e8894bf3b/result.json`及`derived/results-long.png`。
- B站“零跑”下拉词：成功，10条普通搜索词，XML前后稳定，派生下拉图已检查。证据：`.local/android/goal-live-stage3/20260908-222323-31c2d24f3f/result.json`。
- 小红书“零跑”下拉词：成功，12条普通搜索词；账号建议、商品建议单列excluded，图中原样保留，不计入普通词。证据：`.local/android/goal-live-stage3/20260908-222329-31ed14def7/result.json`。以上三项在用户要求停止重复验证后均未重测。
- 用户登录前的小红书结果明确blocked；22:28用户正常登录导致B站未完成采集中途切App，已保留旧attempt，不拼接跨App帧；后来新增`foreground_app_changed`门禁，避免将其他App登录页归因给B站。
- 登录后小红书真实两帧：`.local/android/goal-xhs-inspect-223152`；解析到独立笔记、作者、封面与双列布局，396px重叠区域RGB误差0。该组仅两帧，不是TOP20。
- B站连续原生帧已支持“零跑”13条、旧“比亚迪”链路16条的离线分析；不把13/16说成20。零跑13条派生图1080×5910位于`.local/android/goal-offline-stage5/verified-first-13-partial.png`，已分段查看，报告明确partial、requested_top_n=20；无文字遮盖或猜测位移。

### 尚未验收

- **小红书TOP20：未通过。** 最后一次允许完成的attempt为`.local/android/goal-xhs-top20-v2/20260908-223809-64833d757b`，返回partial / `initial_search_not_at_top`。同词搜索复用了旧滚动位置，`scroll.toBeginning`仅让悬浮筛选栏出现，并未证明首卡回到顶部；严格起点门阻止把后续笔记报成前两条。
- **B站TOP20：未通过。** 曾遇网络、短暂提示层、未知模块和用户切换；已经保存的单一连续链路不足20条完整独立内容。代码已补动态/专栏/直播/独立商品广告排除、原地等待提示层消失、共同文字锚点等，不重新发起实拍提高置信度。
- 最后代码收口把小红书回顶改为“先选择综合，再在内容区有界下拖，必须出现列表头占位且首卡完整”；**该最新导航分支未再操作UI验证**。
- 双平台真实TOP20、多关键词TOP20、明确到底但不足N的真实案例、最终冻结EXE里的UI worker均未验收。用户明确停止复跑；本轮不新增整批/烟测。

## 2. 返回契约与主runner接入

入口`opinion_capture.android.capture(request)`保留原统一dict的所有字段，并新增：

- `primary_artifact_path`：唯一正式导出/汇总路径；未严证为null，绝不用第一原始帧冒充TOP N长图。
- `source_artifact_paths`：原始ADB PNG与XML前后快照。`frames`含源路径、哈希、时间、稳定性、source_index、accepted、拒绝原因；丢弃/重试的帧仍保留。
- `items`：identity、identity_kind、platform_content_id、title、author、duration、promoted、rank、cover_sha256、content_bounds和source_mappings。每个映射含实际source_index与接受序列index，避免跳过提示层帧后索引错位。
- `rank_policy='independent_content_cards'`；`rank_order='visible_top_y_then_x'`（主智能体已确认）。所有帧先转换到统一内容坐标系，再按顶边Y、同顶X排序。小红书回收节点index会变化，不作排名。
- 无BV/笔记ID时`platform_content_id=null`，identity明确为可见标题/作者/时长指纹；完整封面像素哈希辅助确认重复。新位置同指纹且同封面可记重复出现，不增加独立条数；证据不足的身份冲突partial，不静默合并。
- B站只计综合页独立视频/推广内容卡；账号聚合、相关搜索、直播、动态、专栏及独立商品广告均单列excluded。小红书独立笔记及笔记广告计数；账号、相关词、直播排除。
- 下拉词使用`rank_policy='text_suggestions_exclude_accounts_products'`，账号/商品行仍在原生截图上，不把它们算普通词。稳定XML配对、关键词相符、完整列表/尾标共同决定完成。
- `status`支持success/partial/blocked/failed/cancelled；取消后primary置null。只有明确尾标且前序边界已解释，少于N可success，`outcome='insufficient_results'`并返回真实数量。静止、不确定、超时不推断到底。

### 原生矩形长图

`long_image.placements`记录每段源矩形与目标矩形，`rank_links`记录共同身份/文字锚点、物理位移、误差、歧义与丢弃锚点。`layout='continuous_native_rectangle'`，全宽无缩放地拼接至覆盖前N完整卡的最低底边。

**不使用白块遮盖邻列内容。** 小红书双列不等高时，矩形带出的N+1完整或局部内容原样保留、不计入N；manifest分别给出`target_ranks`、`incidental_ranks`、`partially_visible_ranks`。尚未获得完整身份/排名的边缘碎片记录在`partially_visible_fragments`，rank=null，不凭照片碎片猜rank。`duplicate_occurrences`记录重复出现。源帧不裁、不重编码、不覆盖。

## 3. 进程、取消与GUI/打包接口

- `timeout_seconds`正有限总预算，兼容主计划180秒及1800秒。ADB/UI子进程与算法检查共用剩余期限；`cancel_file`为可选绝对路径，检测存在立即停止后续命令。互斥由主runner设备OS锁承担。
- Windows非冻结UI worker使用`sys._base_executable`，环境PYTHONPATH注入repo src、虚拟环境`Lib/site-packages`并保留既有值；避免venv redirector被终止后实际Python继续操作设备。
- 已实际运行超时和取消两条子进程回归：worker先写ready，再延迟写模拟设备动作；父进程终止后延迟动作文件始终未出现。该回归不接触真实设备。
- 冻结分支与打包执行者约定：同目录`OpinionCapture-CLI.exe --ui-worker JSON`，stdout仅JSON；缺CLI返回`frozen_cli_worker_not_found`，不从windowed GUI入口猜stdout。launcher已由打包任务实现分派，本子任务未跑冻结UI烟测。
- ADB路径优先显式executable、`OPINION_CAPTURE_ADB`、PATH、已有MuMu位置；不connect、不创建模拟器、不代办登录。打包侧同时负责ADBUTILS_ADB_PATH/PATH。
- `discover_devices()->list[dict(name,serial,state)]`：GUI后台可用，最多3秒，仅`adb devices -l`；name优先model否则serial，不读取/改变App。异常交GUI显示。
- 偶发uiautomator2只读dump断连最多重试一次，沿用本地RUNTIME_ISSUES方案；导航不盲重试。新Windows redirector问题已向主说明，由主按写范围整合问题库，本子任务未修改该文件。

## 4. 验证记录与限制

- 开发过程中最近一次执行的Android测试为**34项通过**（含真实本地XML回归、合成连续像素、统一返回契约、明确不足/零条、跨App检测、取消/超时进程回归）。合成数据不替代实拍。
- 用户要求停止复跑之后未重跑这些已通过测试；新增小红书真实结构/广告/起点回归用例已写入，但**未执行**。最后仅对两模块及3个Android测试文件做AST语法检查，均通过；不将AST视为行为验收。
- 本地真实XML/PNG不随仓库分发；缺本机证据的回归明确skip。所有新证据只保存在`.local/android/goal-*`，不提交图片/会话。
- 只读评估了imaging.stitch_vertical：其固定裁切接口未覆盖原生悬浮栏变化、XML锚点和条目边界。因此新增android_results.py验证算法，未修改imaging.py。

## 5. 文件与后续最小动作

本轮代码仅：`src/opinion_capture/android.py`、新增`src/opinion_capture/android_results.py`；测试仅`tests/test_android.py`、`tests/test_android_results.py`、`tests/test_android_capture.py`；文档仅本文。

后续在用户明确允许时，只验证最新小红书起点恢复和未完成TOP20，不重做已通过三项、不重装/初始化。TOP20验证前可继续使用旧XML/PNG离线修正；跨attempt、用户切App前后的帧不得拼作一个连续会话。主runner仍仅导出非空primary_artifact_path。
