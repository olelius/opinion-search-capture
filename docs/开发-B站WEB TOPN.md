# B站 WEB 采集与本轮修复

更新：2026-09-09。

## 当前实现

- 综合页按独立视频/推广内容卡计数，账号聚合、相关搜索、直播及课堂课程合集保留在画面中但不计入TOP N；未知普通视频身份仍阻止排名确认。
- 下拉词只截当前视口，检查关键词、完整词条、顺序、无遮挡与相对布局，不因无关整页尺寸变化误判。原生整体位移单独记录。
- TOP N采用原始视口帧拼接目标区域，不再通过full_page改变现有Chrome滚动条布局；逐帧核对身份/顺序、目标区域无遮挡、原图哈希及裁切映射。
- 原始帧、派生区域、最终拼图分开登记；额外帧采用短名page-NNN-fNNN-vN.png，避免Windows长路径边界。最终导出名称仍按客户模板生成。
- authenticated_navigation(page)只认同源可见账号导航的资料链接/头像，不读取Cookie或密码；sessions负责正常关闭上下文并报告会话已保存。

## 本轮证据

- 客户下拉证据：.local/customer-evidence/bilibili-12a2e35a。文字/顺序一致，截图前后横移7px、整页尺寸变化导致旧稳定性判定失败；已用本地页面复现并更换采集范围。
- 新增14项定点及10项受影响回归由B站子任务完成，原图与拼图已查看，未运行全量套件。
- 独立无头真实批次e0437feb58ed42d09c814876da8d5cec最终2项成功，权威报告：.local/merged-fixes/exports/.capture-state/runs/e0437feb58ed42d09c814876da8d5cec/report-df876e5cf0664db490e5552ed45f7477.json。
- 同一批次中先修复中间帧260字符路径问题，再定位课堂ss卡被误当普通视频的缺ID判定；只恢复未通过的结果项，成功下拉词未重采。
- 历史TOP3/TOP20/跨页TOP35证据仍保留于.local/web_bilibili/goal-topn-live/acceptance-v2.json，不重复采集。

## 阶段与边界

- prepare_search、waiting_content、screenshot、validate_capture通过本地阶段记录传给GUI，并在结果中保存耗时。
- 暂无客户电脑更新后实测结论；本机无头验收不替代客户机器事实。出现新界面/验证门槛按现场记录，不盲重试或降低判定标准。
