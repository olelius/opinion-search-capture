# B站 WEB 最小实测与首版适配器

日期：2026-09-08，Asia/Shanghai。关键词：`零跑`。本任务只新增约定的四个文件，未修改进度、依赖或其他模块，未执行 Git 提交。

## 已确认结果

21:56 完成搜索/下拉词各一次初探并检查真实 PNG，随后立即开发。适配器完成两轮集成复核：首轮发现搜索框局部遮挡导致计数提前返回空值，修正后复核通过。各调用仅导航一次，无网络重试、无验证码处理、无登录操作。

| 最终复核 | 状态 | 同页观测 | 原图 | 说明 |
| --- | --- | --- | --- | --- |
| 搜索 TOP3，22:00:50 +08:00 | `partial` | 27 个有标题/视频身份的去重视频卡 | PNG 1440×2388 | 综合页混入直播和推广模块；普通登录提示遮住搜索框右部及部分首行画面。未严证综合 TOP3 排名/截取边界，`rank_verified=false` |
| 下拉词，22:00:53 +08:00 | `success` | 10 条 | PNG 1440×1498 | 输入框为零跑，10 条均完整可见且采样点未被遮挡；下拉词不按 top_n 截断，`rank_verified=false` 表示未执行排名任务 |

- 下拉词依次为：零跑a10、零跑c10、零跑c11、零跑c16、零跑b10、零跑a05、零跑、零跑b01、零跑d19、零跑汽车。
- 使用本机 `C:\Program Files\Google\Chrome\Application\chrome.exe`，Playwright headless、1440×1000 视口、独立新 profile；Chrome 启动成功，未使用浏览器纠正分支。没有连接其他浏览器会话，也未访问手机。
- 两类最终 PNG 均经过 `view_image` 人工视觉检查、Pillow 格式/尺寸检查、SHA-256 与证据清单比对；采集前后任务相关 DOM 指纹均一致。指纹一致不代表所有动画像素同时冻结。
- 本次未遇到强制登录或验证码；阻塞分支只有离线分类验证，不宣称经过真实验证码页实测。

## 关键证据（仅本地，不入 Git）

最新汇总：`H:\Public Opinion Search Capture System\.local\web_bilibili\probe-result.json`。

- 搜索目录：`H:\Public Opinion Search Capture System\.local\web_bilibili\captures\results-20260908T220046647265-b65adeec`。
- 下拉词目录：`H:\Public Opinion Search Capture System\.local\web_bilibili\captures\suggestions-20260908T220050790369-386f5736`。
- 每个目录均含 `original.png`、`page.html`、`evidence.json`；原图未裁切、重绘、拼接或覆盖。HTML 只是同一真实 Page 的现场记录，没有作为伪造平台页渲染。
- 搜索原图 SHA-256：`da38abb0b66d71e5ed7ac7c237acd730ff22aabd2c66d64100e08482a906b12b`。
- 下拉词原图 SHA-256：`f454e98340146baa503eb8f2dd497f42e6ead67502a1c97b4ff6ec468a849d70`。
- `git check-ignore` 已确认本地汇总与原图被忽略。早期现场保留在同一 `.local\web_bilibili` 根目录，最终清单指向新产物，不覆盖历史原图。

## 接口与复现

`H:\Public Opinion Search Capture System\src\opinion_capture\web_bilibili.py` 提供 `capture(request: dict) -> dict`，返回约定的全部 JSON 字段。支持 `results/suggestions`，默认 `top_n=20`、`timeout_seconds=30`；请求错误直接返回 `failed`，不启动浏览器。

```powershell
$env:PYTHONIOENCODING='utf-8'
Set-Location -LiteralPath 'H:\Public Opinion Search Capture System'
& 'H:\Public Opinion Search Capture System\.venv\Scripts\python.exe' -B 'H:\Public Opinion Search Capture System\scripts\probe_web_bilibili.py' --keyword 零跑 --top-n 3 --timeout-seconds 30
& 'H:\Public Opinion Search Capture System\.venv\Scripts\python.exe' -B -m unittest discover -s 'H:\Public Opinion Search Capture System\tests' -p 'test_web_bilibili.py' -v
```

- **验证结果：31 项离线 unittest 全部通过**，三份 Python 文件 AST 解析通过；测试默认不联网。覆盖参数错误、普通登录提示/强制登录/验证码/HTTP 错误分类、关键词和来源不符、缺图、重复条目、图片未加载、遮挡/DOM 变化、专属 profile 保护、最多两次启动尝试。
- 每次采集新建唯一产物子目录；可选 `user_data_dir` 要求绝对路径，接受空目录或本适配器标记的专属 profile，其他非空目录直接失败。临时文件和默认 profile 固定在 `.local\web_bilibili`。
- `observed_count` 表示同页可确认的视频去重数/下拉词条数，不表示严证 TOP N。搜索首版固定返回保守的 `partial`，不将页脚视频链接、直播卡或未确认身份模块凑入 N。
- 错误页面返回 `blocked/failed`，截图命名为 `diagnostic.png`；失败留图仍是诊断，不是采集成功。
- 操作按总超时预算执行，超时另留至多 3 秒诊断截图窗口，浏览器关闭可能有尾延迟。只关闭本调用创建的上下文。同步 API 宜由独立采集进程调用，同一 profile 不并发；进程内并发未实测。

## 待解决边界

1. 明确综合搜索中直播、推广等模块的计数与排序口径，再实现严格 TOP N 的边界、视觉顺序校验和必要的分页/滚动；本版只采综合第一页，不隐式改为视频分类。
2. 普通登录提示并非登录门槛，但会遮挡部分搜索画面；本版保留现场并降级，不隐藏页面元素或假装画面完整。
3. 不同独立会话/采集时点的结果顺序实际有变化，原因未验证；不得把某次结果宣传为全平台固定排名。登录保持、真实阻塞页、长时间运行、打包环境仍待专项验证。

补充决定：收到“可在现有页面自然关闭普通遮挡弹层一次”的指示时，本轮独立上下文已关闭，因此结束实测，未重新导航、删改 DOM 或补造 TOP3 图。最终保持 `partial/27` 与 `keyword_input_partly_obscured`。主集成按图片后缀筛选产物，HTML/JSON 继续作为证据文件，不作为图片导出。

工具记录：一次 PowerShell 内嵌正则的引号解析失败，改用 here-string 成功读取原 HTML；中文终端输出显式设置 UTF-8。依照本任务写入边界，未更新本地运行问题库。
