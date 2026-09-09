# 修复：App 封面与几何

## 1. 封面加载证据与保守重试边界

2026-09-08 本地证据 `app-final-20260908-231446-945f8e/xiaohongshu/20260908-231446-ffda76731a` 中，frame-013 与 frame-021 含同标题、作者、`0:36` 的笔记。013 的完整封面可见，021 的相同位置是白色渐变占位，已造成 `identity_collision_or_unverified_duplicate`。新增 `opinion_capture.android_image_evidence.assess_cover()` 对完整分辨率封面 crop 计算原始 RGB 像素字节 SHA-256，并先避开封面四角控件取中心分析区，再联合检查 3×3 块方差、边缘能量和近纯色范围；当前默认边缘界限为 0.025。

判定只表达内容是否已获得足够可见证据：`content_visible` 或 `blank_or_unverified`。均匀背景、接近纯色和渐变占位保持“待确认加载”，不是准确的空图分类；白底稀疏文字证据不足时保持待确认，避免把角落播放按钮或时长误作正文；明显纹理需同时满足中心区多块和边缘界限。只有完整可见且结果为 `blank_or_unverified` 时返回 `retry_recommended=true`。本次只新增接口、定点测试和记录，不改写原图、不填补历史封面、不放松 identity 比对；主流程接入由后续变更完成。
