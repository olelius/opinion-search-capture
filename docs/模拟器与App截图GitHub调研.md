# Android 模拟器与 App 截图：GitHub 调研

日期：2026-09-08。状态：仓库/代码静态核查；未安装模拟器、未运行目标 App、未执行截图测试。

## 1. 本轮结论与约束

- 用户已明确开发测试也不使用真机；撤回上一轮“真机优先”的建议。手机端采用电脑上的 Android 模拟器运行正式原生 App。
- 模拟器是运行环境，uiautomator2、Airtest、Appium 是操作/截图工具；它们不是互相替代的同一类产品。
- 已找到模拟器支持的直接文档和代码证据，也找到接近小红书/B站搜索流程的脚本；本轮尚未核实到能直接交付全部8种组合、TOP N长图、下拉词、调度与模板导出的成品。
- 搜索未确认不等于项目不存在；作者 README、源码、维护状态和本项目实际兼容性分别陈述。
- 默认建议仍用 Python 主程序；Android 侧由 uiautomator2 控制，正式原图使用 ADB PNG 截取。雷电作为首轮模拟器候选，MuMu作为第二候选，AVD用于标准化验证。排序是工程建议，不是性能或平台兼容性实测结论。

## 2. 通用开源候选

| 仓库 | 本轮核实的能力 | 对本项目的判断 |
| --- | --- | --- |
| [openatx/uiautomator2](https://github.com/openatx/uiautomator2) | Python设备连接、控件树、输入/滑动、截图；README含B站启动和控件操作示例 | 首选操作层；不自带两个平台的TOP N业务判定 |
| [AirtestProject/Airtest](https://github.com/AirtestProject/Airtest) | Android模拟器连接、截图后端选择、图像匹配和输入；`snapshot`有质量/尺寸参数 | 可做可视化验证与局部图像定位补充；默认不与其他框架同时控制同一实例 |
| [appium/appium-uiautomator2-driver](https://github.com/appium/appium-uiautomator2-driver) | README明确覆盖原生/混合/移动网页应用，在模拟器与真机上测试；支持AVD能力和截图 | 标准化驱动备选；需要Appium服务、驱动和SDK/JDK等配套，不与openatx包混为同一产品 |
| [MaaXYZ/MaaFramework](https://github.com/MaaXYZ/MaaFramework) | 控制方式文档列出ADB截图，以及MuMu/雷电/AVD专用截图扩展；源码有MuMu截图实现 | 可借鉴模拟器适配和截图后端；首版仅做周期截图时暂不为此引入整套框架 |
| [Genymobile/scrcpy](https://github.com/Genymobile/scrcpy) | 显示、控制、录制Android设备画面 | 适合观察调试，不是关键词批次采集/长图/下拉词成品 |

本轮 GitHub 连接器元数据确认以上五个仓库均为公开且未归档；未用星标数推断稳定性，也未把最新分支视作已安装/已测试版本。

### 可核查源码位置

- uiautomator2：[`uiautomator2/__init__.py`](https://github.com/openatx/uiautomator2/blob/master/uiautomator2/__init__.py)，`screenshot`、`dump_hierarchy`。
- Airtest：[`docs/wiki/device/android_zh.md`](https://github.com/AirtestProject/Airtest/blob/master/docs/wiki/device/android_zh.md)，有独立的“Android模拟器连接”段落，说明JAVACAP后端；[`airtest/core/api.py`](https://github.com/AirtestProject/Airtest/blob/master/airtest/core/api.py)，`snapshot`。
- Appium：[`README.md`](https://github.com/appium/appium-uiautomator2-driver/blob/master/README.md)，Requirements、Emulator、截图扩展。当前文档存在大版本兼容性约束，正式接入时锁定配套版本。
- MaaFramework：[`docs/en_us/2.4-ControlMethods.md`](https://github.com/MaaXYZ/MaaFramework/blob/main/docs/en_us/2.4-ControlMethods.md)；[`MuMuPlayerExtras.cpp`](https://github.com/MaaXYZ/MaaFramework/blob/main/source/MaaAdbControlUnit/EmulatorExtras/MuMuPlayerExtras.cpp)实际读取模拟器画面；[`AdbDeviceWin32Finder.cpp`](https://github.com/MaaXYZ/MaaFramework/blob/main/source/MaaToolkit/AdbDevice/AdbDeviceWin32Finder.cpp)处理MuMu和雷电发现。

Airtest作者的[模拟器连接教程](https://airtest-new.doc.io.netease.com/airtest/IDEdocs/device_connection/3_emulator_connection/)明确提供无真机时的模拟器测试路径。教程中的旧品牌测试表和端口示例仅作参考，连接串号以所选模拟器实际输出为准。

## 3. 平台业务相关项目：哪些接近，哪些应排除

### 小红书：appium_python

[guochunyang2004/appium_python](https://github.com/guochunyang2004/appium_python) 的 [`index.py`](https://github.com/guochunyang2004/appium_python/blob/master/index.py)确实操作 `com.xingin.xhs` 原生应用，从Excel读取搜索词、滚动匹配标题、截图后插入Excel。

这是接近需求的流程参考，但尚不是可用成品：截图在匹配标题后触发，不是完整TOP N搜索结果长图，也未见下拉词截图链路；入口写死设备和旧Appium连接方式，存在未定义变量。子智能体核查默认分支最新提交为[2020-02-08](https://github.com/guochunyang2004/appium_python/commit/9d1e9c67652c34ac7be177085cd87789e525513d)。不建议直接运行旧脚本。

### B站：Bilibili_Appium_AutoTest

[Zcm-hellbenter189/Bilibili_Appium_AutoTest](https://github.com/Zcm-hellbenter189/Bilibili_Appium_AutoTest) 的 [`base/base_driver.py`](https://github.com/Zcm-hellbenter189/Bilibili_Appium_AutoTest/blob/main/base/base_driver.py)使用 Android UiAutomator2、设备地址 `127.0.0.1:7555`；[`page_objects/search_page.py`](https://github.com/Zcm-hellbenter189/Bilibili_Appium_AutoTest/blob/main/page_objects/search_page.py)包含清空、输入搜索词、搜索并打开首条视频。

可参考页面对象和搜索动作，但截图主要是失败报告，不是搜索截图归档。当前代码 `noReset=False` 与README会话复用描述有差异，直接照搬可能改变会话状态。子智能体核查默认分支最新提交为[2026-09-05](https://github.com/Zcm-hellbenter189/Bilibili_Appium_AutoTest/commit/29c51a41e36df17c4a397501c6d30b380087c06b)；近期提交不等于本项目测试通过。

### 三个易误选的候选

- [Gloridust/xhs_screenshot_spyder](https://github.com/Gloridust/xhs_screenshot_spyder)：主智能体复核[`main.py`](https://github.com/Gloridust/xhs_screenshot_spyder/blob/main/main.py)，使用Selenium Chrome `mobileEmulation`，再拼接顶部/底部素材并替换返回图标。属于手机风格网页图，不是原生App运行截图，排除出APP采集链路。
- [easonk804/xiaohongshu_GET](https://github.com/easonk804/xiaohongshu_GET)：描述提到模拟器，但子智能体核查当前[`base_browser.py`](https://github.com/easonk804/xiaohongshu_GET/blob/main/src/browser/base_browser.py)使用Playwright/Chromium和网页选择器；不据仓库描述认定其原生App支持。
- [Cranberrycrisp/XiaohongshuSpider](https://github.com/Cranberrycrisp/XiaohongshuSpider)：使用模拟器/Appium操作App，但[`app_mitmproxy.py`](https://github.com/Cranberrycrisp/XiaohongshuSpider/blob/main/app_mitmproxy.py)核心输出是接口数据和下载的笔记图片，区别于屏幕截图；不选为截图主链路。

## 4. 模拟器选型

### 雷电：首轮候选

官方[ADB说明](https://help.ldmnq.com/docs/lei-dian-mo-ni-qi-ADB-ming-ling-shi-yong-shuo-ming)列出模拟器截图和导出、应用管理、输入与多实例选择；[ldconsole说明](https://help.ldmnq.com/docs/LDPlayer-ming-ling-hang-kai-fa-zhe-shi-yong-shuo-ming)提供实例查询、启动/关闭、应用管理等命令。

选择理由是Windows本地部署和实例生命周期控制有明确接口，不是已证明它比其他模拟器更快、更适合目标平台。使用实际受支持版本，安装正式App后验证ABI、界面与登录；不写死旧教程的端口，也不依赖ROOT作为普通截图前提。

### MuMu：第二候选

Airtest模拟器文档及MaaFramework的发现/截图代码提供现成集成参考。普通操作仍优先ADB，截图优先通用PNG；专用SDK截图仅在性能数据证明必要且兼容性通过时再接入。开源适配器存在不代表模拟器本体也是开源产品。

### Android Emulator / AVD：标准化备选

官方支持[命令行启动、数据目录、无窗口运行](https://developer.android.com/studio/run/emulator-commandline)，便于固定系统镜像、分辨率和自动化环境。

代价是SDK/系统镜像准备和目标APK架构兼容性验证。Windows硬件加速按[官方加速指南](https://developer.android.com/studio/run/emulator-acceleration)检查，当前指南推荐WHPX；不要照搬旧文章把关闭Hyper-V或安装HAXM作为统一前提。本轮未更改系统虚拟化设置。

### 实例组织建议

- 开发验证可从一个实例开始，两个App串行切换；不是每个关键词启动一个模拟器。
- 如果实测一个实例影响整批时限，可用两个实例分别承载两平台，每个实例内部仍串行。
- 固定竖屏分辨率、DPI、字体缩放、App版本和会话；例如1080×2400是匹配现有资料宽度的候选，不是强制标准。
- 直接采集Android显示内容，不截Windows桌面上的模拟器外壳，避免窗口边框、桌面缩放和遮挡影响。
- 能通过ADB操作不等于任意窗口最小化策略都不影响App渲染；挂起/后台降帧/长时间运行仍在模拟器中验证。

## 5. 代码核查发现：PNG后缀不保证上游无损

本轮读取的 uiautomator2 代码中，默认截图调用 `takeScreenshot(1, 80)`；其[设备端实现](https://github.com/openatx/android-uiautomator-server-jar/blob/master/app/src/main/java/com/wetest/uia2/stub/AutomatorServiceImpl.java)先以JPEG压缩，然后Python端可另存为PNG。另存格式不会恢复已经丢失的细节。

因此建议将“操作”和“最终原图获取”分开：

- uiautomator2负责控件定位、输入、滑动和状态判断。
- 证据原图走Android [ADB `screencap -p`](https://developer.android.com/tools/adb#screencap) PNG路径，保留原始字节和分辨率。
- Airtest用于报告或辅助定位时，显式控制其quality/max_size；报告缩略图不充当原图。
- Windows实现使用二进制子进程输出或设备文件+`adb pull`，避免把原始图像字节交给文本处理管道。此处为实现建议，未执行截图。

## 6. 原生 App 长截图候选：virtual-device-scrollshot

[ashfaqahmed39/virtual-device-scrollshot](https://github.com/ashfaqahmed39/virtual-device-scrollshot)是本轮最贴近“模拟器内原生App长图”的候选。主智能体复核提交`1ec2857f6fc0e15ac66ad123914093079eaa5e14`的核心代码，`package.json`声明版本0.1.0、MIT，使用Node.js、Appium和Sharp。

### 已确认的实现

- [`src/capture-full-page.js`](https://github.com/ashfaqahmed39/virtual-device-scrollshot/blob/1ec2857f6fc0e15ac66ad123914093079eaa5e14/src/capture-full-page.js)连接当前前台App，使用不清数据、不自动启动的会话设置；选择面积最大的可滚动节点，先回到顶部，再滚动截取屏幕。
- [`src/image-stitcher.js`](https://github.com/ashfaqahmed39/virtual-device-scrollshot/blob/1ec2857f6fc0e15ac66ad123914093079eaa5e14/src/image-stitcher.js)确实寻找相邻帧内容重叠：滚动区域灰度缩略图比对，根据分数裁切后合成PNG。并非只按固定高度拼接。
- 滚动区域上方取首帧、下方取末帧各一次；不要求目标App集成长图SDK。

### 尚不满足的业务要求

- `maxFrames=20`是最多20屏，不是前20条结果。现有循环要求滚动到底，达到帧数上限而未到底时抛错；对持续加载的搜索列表尤其需要修改停止条件。
- 不做关键词输入、下拉词采集、结果身份/排名/去重计数；这些属于平台适配器。
- Android滚动后使用固定450ms等待，没有页面图片加载完成和列表重排验证。
- 固定控件如果位于滚动矩形内部，仍可能干扰匹配；重复封面、大片空白、动态图片造成误配属于源码风险推断，未在本项目实测。

定位：优先作为长图PoC对照基线，不直接作为最终TOP N引擎。先验证算法在目标模拟器上的效果，再决定移植到Python截图模块或保留独立Node工作进程；不为了这个小库立刻更换整个项目语言。测试期间也避免Appium与openatx自动化会话同时操作同一实例。

其他长图相关候选：

- [PGSSoft/scrollscreenshot](https://github.com/PGSSoft/scrollscreenshot)：子智能体核查[`ScrollScreenShot.java`](https://github.com/PGSSoft/scrollscreenshot/blob/da8c23e366c4f71d9e1dc94ae36400b9134a4434/project/src/main/java/com/pgssoft/scrollscreenshot/ScrollScreenShot.java)，为滚动后固定几何拼接，输入事件设备路径也需适配；不优先采用。
- [lovell/sharp](https://github.com/lovell/sharp)：静态图像合成库，适合现成长图的多关键词汇总；不自动操作模拟器或解决排名计数。Python主程序已有Pillow路线时无需同时引入。
- Android [`ScrollCaptureCallback`](https://developer.android.com/reference/android/view/ScrollCaptureCallback)是系统与目标View/Window协作的接口，不等同于一个对任意第三方App都通用的外部长截图命令。

## 7. 下一步验证范围

选定一个模拟器后，只验证最小闭环：安装/启动两个正式App、输入中文关键词、保留下拉词屏幕、提交搜索、读取控件与条目顺序、逐屏保存PNG、拼出长图。

验收重点：原生App界面而非网页包装；搜索框文字一致；中文未丢失；TOP N口径可解释；接缝无重叠/遗漏；长期会话、模拟器重启、截图时间真实记录。登录验证若需要人工处理，则记录并提示，不把验证码画面当正常产物。

尚未验证：所选模拟器上当前小红书/B站版本能否完整运行、控件树信息是否充足、长图重叠算法准确率、每批耗时、整日运行稳定性。本轮不将这些写成已完成。

## 8. 本轮协作与检索覆盖

- 主智能体：核查通用驱动、ADB/模拟器官方资料及关键代码，复核两个平台候选，发现截图默认压缩差异；更新原技术路线。
- 平台调研子智能体：16组GitHub仓库搜索、6组代码搜索，核查5个关键候选及默认分支提交/issue；返回原生App与网页/数据抓取边界，未修改文件。
- 长图调研子智能体：核查3个相关仓库及Android系统接口，确认virtual-device-scrollshot是真实重叠匹配、缺少TOP N计数，区分单屏/系统回调/滚动拼图/静态合成；主智能体复核核心代码后集成。未修改文件、未运行目标App。
