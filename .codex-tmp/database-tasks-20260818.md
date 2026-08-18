# 数据库任务完整清单

- 生成时间：2026-08-18（Asia/Shanghai）
- 任务总数：136
- 状态汇总：CONFIRMED 115、SUCCEEDED 5、FAILED 4、CANCELLED 12
- 归档汇总：已归档 131、未归档 5
- 数据来源：Dashboard `/api/state`（SQLite 任务事实源的只读投影）

| # | 任务 ID | 标题 | 状态 | 优先级 | 运行环境 | 已归档 | 创建时间 |
|---:|---|---|---|---|---|:---:|---|
| 1 | INIT-001 | 初始化 Local Agent Loop 原型 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-29T10:22:51+08:00 |
| 2 | ORDER-EMAIL-PREFERENCE-001 | 将下单成功邮件开关迁移至用户通知首选项 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-29T10:52:47+08:00 |
| 3 | QUOTE-DELIVERY-AUDIT-001 | 核验报价单交期与上传时间功能是否已完成 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-29T11:03:27+08:00 |
| 4 | QUOTE-TIME-TOOLTIP-001 | 为询价时间列表头增加交期时效提示 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-29T11:06:22+08:00 |
| 5 | ADMIN-CONTRACT-ITEM-OVERRIDES-001 | 管理端合同与质量保证书支持订单项覆盖信息 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-29T11:09:44+08:00 |
| 6 | ADMIN-CONTRACT-REMARK-001 | 管理端下载新合同时支持订单项备注覆盖 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-29T11:11:04+08:00 |
| 7 | CART-PACKAGE-HIERARCHY-AUDIT-001 | 商品详情、购物车和结算页突出展示大小包装关系 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-29T11:15:59+08:00 |
| 8 | PC-CART-PDP-PACKAGE-FOCUS-AUDIT-001 | 核验购物车跳转 PDP 后包装选择是否突出定位 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-29T11:19:11+08:00 |
| 9 | NEW-PRODUCT-BADGE-DUAL-END-001 | PC 与小程序为新品增加商品角标 | CANCELLED | high | self_hosted_agent | 是 | 2026-07-29T11:21:34+08:00 |
| 10 | CART-CHECKOUT-BENEFIT-CARD-STYLE-001 | 更新 PC 购物车和结算页优惠券及礼品卡样式 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-29T11:23:18+08:00 |
| 11 | MINI-CHECKOUT-BENEFIT-CARD-STYLE-001 | 更新 RS 小程序优惠券和礼品卡样式 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-30T11:50:25.346+08:00 |
| 12 | AUTH-CAPTCHA-MOBILE-DRAG-001 | 修复登录注册图形验证器在移动设备上无法拖动 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-30T14:04:38.145+08:00 |
| 13 | LOOP-DASHBOARD-STATUS-NAV-001 | 将 Loop Agent 监控状态汇总移至顶部导航栏 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-30T14:18:26.306+08:00 |
| 14 | GIFT-CARD-AGREEMENT-DUAL-END-001 | PC 与小程序购物车增加礼品卡协议校验 | FAILED | high | self_hosted_agent | 是 | 2026-07-30T14:29:55.442+08:00 |
| 15 | DARPHIN-GIFT-TRANSFER-NEW-MEMBER-001 | 朵梵礼品转赠券增加首次注册会员领取限制 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-30T14:47:40.765+08:00 |
| 16 | MINI-PDP-BETTER-WORLD-CONTENT-001 | RS 小程序商品详情增加 Better World 内容 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-30T14:54:43.896+08:00 |
| 17 | PC-PDP-BETTER-WORLD-CONTENT-001 | RS PC 商品详情增加 Better World 内容 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-30T14:57:11.866+08:00 |
| 18 | ADMIN-PRODUCT-LINE-PRICE-RECORD-001 | 商品管理端增加划线价搜索并调整导入导出记录 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-30T15:00:03.757+08:00 |
| 19 | PC-BOM-NARROW-PADDING-001 | 修复 RS PC BOM 页面小尺寸左侧贴边 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-30T15:30:01.696+08:00 |
| 20 | LOOP-DASHBOARD-PENDING-FILTER-001 | Loop Agent 前端增加待执行筛选 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-30T15:35:16.503+08:00 |
| 21 | LOOP-DASHBOARD-HEADER-METADATA-001 | 精简 Loop Agent 顶部元数据并移动更新时间 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-30T15:41:19.397+08:00 |
| 22 | PC-BETTER-WORLD-BADGE-SIZE-001 | 核验并统一 PC 商品列表 Better World 标签尺寸 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-30T15:46:47.388+08:00 |
| 23 | LOOP-DASHBOARD-CONFLICT-BLOCKER-001 | Loop Agent 前端显示等待冲突的阻塞任务 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-30T16:21:25.981+08:00 |
| 24 | PC-MY-ORDER-DETAIL-BUTTON-SIZE-001 | 统一 RS PC 我的订单详情按钮尺寸和样式 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-30T16:42:26.649+08:00 |
| 25 | LOOP-DASHBOARD-ARCHIVED-FILTER-LAYOUT-001 | Loop Agent 筛选栏保持单行并增加已归档筛选 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-30T16:45:13.440+08:00 |
| 26 | QUOTE-TIME-LABEL-VISIBILITY-001 | 修正批量询价报价时间的名称和显示范围 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-30T17:24:28.900+08:00 |
| 27 | NEW-PRODUCT-BADGE-PC-001 | RS PC 商品列表与详情增加新品角标 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-31T09:27:35.795+08:00 |
| 28 | NEW-PRODUCT-BADGE-MINI-001 | RS 小程序商品列表与详情增加新品角标 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-31T09:27:58.958+08:00 |
| 29 | LOOP-INDEPENDENT-ARCHIVE-ATTRIBUTE-001 | 将 Loop Agent 归档改为独立 archived_at 属性 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T09:37:04.745+08:00 |
| 30 | LOOP-DASHBOARD-COPY-TASK-ID-001 | Loop Agent 任务名称旁增加复制任务 ID 按钮 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T09:43:09.172+08:00 |
| 31 | LOOP-DASHBOARD-ATTENTION-SINGLE-ITEM-001 | Loop Agent 需要关注面板只显示一条任务 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T09:48:50.396+08:00 |
| 32 | HOLDING-AI-GROUP-HEALTH-JSON-001 | 修复 Holding AI 分组体检返回无效 JSON | CONFIRMED | medium | self_hosted_agent | 是 | 2026-07-31T14:01:11.289+08:00 |
| 33 | HOLDING-WORKBENCH-TITLE-001 | 将 Holding 网站标题改为粮仓工作台 | CONFIRMED | medium | self_hosted_agent | 是 | 2026-07-31T14:09:07.708+08:00 |
| 34 | LOOP-DASHBOARD-REMOVE-HUMAN-FILTER-001 | Loop Agent 前端移除需人工筛选 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T14:26:35.401+08:00 |
| 35 | RS-ADMIN-CMS-VALIDITY-DATETIME-001 | RS 管理端四个 CMS 有效时间支持时分秒 | CONFIRMED | medium | self_hosted_agent | 是 | 2026-07-31T14:34:25.832+08:00 |
| 36 | LOOP-DASHBOARD-NAV-STATS-REORDER-001 | 重排 Loop Agent 顶部统计并替换卡顿与失败项 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T14:37:39.000+08:00 |
| 37 | LOOP-WORKER-INTERVAL-20MIN-001 | 将 Loop Agent Worker 默认周期调整为 20 分钟 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T14:43:05.714+08:00 |
| 38 | LOOP-DASHBOARD-ACTIVE-EXECUTION-SINGLE-ITEM-001 | Loop Agent 活动执行面板最多展示一项 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T14:49:29.096+08:00 |
| 39 | LOOP-DASHBOARD-PROFILE-CARD-GRID-001 | Loop Agent 执行档位改为三列卡片网格 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T14:51:37.617+08:00 |
| 40 | GIFT-CARD-AGREEMENT-MINI-001 | RS 小程序购物车增加礼品卡协议校验 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-31T15:03:45.767+08:00 |
| 41 | GIFT-CARD-AGREEMENT-PC-001 | RS PC 购物车增加礼品卡协议校验 | CONFIRMED | high | self_hosted_agent | 是 | 2026-07-31T15:03:45.767+08:00 |
| 42 | LOOP-DASHBOARD-NAV-ATTENTION-COUNT-001 | 统一 Loop Dashboard 顶部需关注统计口径 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T15:11:25.757+08:00 |
| 43 | LOOP-DASHBOARD-SIDEBAR-WIDTH-001 | 调整 Loop Dashboard 左右栏宽度 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T15:25:43.212+08:00 |
| 44 | LOOP-DASHBOARD-TASK-LIST-VIEWPORT-SCROLL-001 | 限制 Loop Dashboard 任务列表为视口剩余高度滚动 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T15:37:01.748+08:00 |
| 45 | LOOP-DASHBOARD-PROFILE-CARD-METRICS-001 | 重排 Loop Dashboard 执行档位卡片指标 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T15:42:59.863+08:00 |
| 46 | LOOP-DASHBOARD-FILTER-TYPOGRAPHY-001 | 统一 Loop Dashboard 筛选栏字体样式 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T15:47:07.961+08:00 |
| 47 | HOLDING-AI-GROUP-HEALTH-INVALID-RESULT-001 | 修复 Holding AI 分组体检未返回有效内容 | CONFIRMED | medium | self_hosted_agent | 是 | 2026-07-31T15:49:59.052+08:00 |
| 48 | LOOP-DASHBOARD-SIDEBAR-ACCORDION-001 | 将 Loop Dashboard 右侧面板改为手风琴布局 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T15:58:09.090+08:00 |
| 49 | LOOP-DASHBOARD-ACCORDION-HEADER-LAYOUT-001 | 调整 Loop Dashboard 手风琴标题布局和默认展开项 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T17:02:13.175+08:00 |
| 50 | HOLDING-AI-GROUP-HEALTH-JSON-REGRESSION-001 | 修复 Holding AI 分组体检无效 JSON 回归 | CONFIRMED | medium | self_hosted_agent | 是 | 2026-07-31T17:13:21.484+08:00 |
| 51 | LOOP-DASHBOARD-TABLE-HEADER-FILTERS-001 | 将 Loop Dashboard 项目与档位筛选迁移到表头 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T17:16:53.267+08:00 |
| 52 | LOOP-DASHBOARD-TASK-TIMELINE-COLUMN-001 | Loop Dashboard 耗时列改为竖排任务时间信息 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T17:19:18.824+08:00 |
| 53 | LOOP-DASHBOARD-NAV-STAT-FILTER-DRILLDOWN-001 | Loop Dashboard 顶部统计联动任务列表筛选 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-07-31T17:36:44.755+08:00 |
| 54 | ADMIN-CONTRACT-QUANTITY-LIMIT-001 | 管理端质量保证书数量上限与 PC 单商品下单一致 | CONFIRMED | medium | self_hosted_agent | 是 | 2026-07-31T18:18:28.258+08:00 |
| 55 | PC-HOME-RIGHT-OFFSET-001 | 修复 RS PC 首页主内容异常靠右 | CONFIRMED | high | self_hosted_agent | 是 | 2026-08-03T09:53:25.136+08:00 |
| 56 | LOOP-DASHBOARD-STATUS-PRIORITY-HEADER-FILTERS-001 | 为 Loop Dashboard 状态和优先级表头增加筛选 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T10:10:59.601+08:00 |
| 57 | LOOP-DASHBOARD-REMOVE-PENDING-CONFIRM-FILTER-001 | 删除 Loop Dashboard 待确认/归档筛选分类 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T10:17:31.277+08:00 |
| 58 | LOOP-DEEPSEEK-AGENT-PROVIDER-001 | 为自建 Agent 接入 DeepSeek 模型 API | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T10:38:43.072+08:00 |
| 59 | LOOP-RUNTIME-ENVIRONMENT-ROUTING-001 | 为任务队列增加运行环境路由 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T10:38:43.072+08:00 |
| 60 | LOOP-SELF-HOSTED-AGENT-RUNTIME-001 | 实现可接入模型 API 的自建 Agent 运行时 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T10:38:43.072+08:00 |
| 61 | LOOP-DASHBOARD-CONFIRMATION-LABEL-001 | 将 Dashboard 顶部待确认归档文案改为待确认 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T11:32:56.954+08:00 |
| 62 | LOOP-DASHBOARD-REMOVE-PROGRESS-COLUMN-001 | 删除 Dashboard 任务列表进度列 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T11:32:57.300+08:00 |
| 63 | LOOP-DASHBOARD-TIME-HHMM-001 | Dashboard 任务时间改为 24 小时制时分显示 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T11:32:57.565+08:00 |
| 64 | LOOP-DASHBOARD-DEPENDENCY-INDICATOR-001 | 在 Dashboard 状态标签旁显示依赖状态圆点 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T11:32:57.814+08:00 |
| 65 | LOOP-DASHBOARD-MAIN-SIDEBAR-WIDTH-001 | 精简 Dashboard 右侧模块并调整左右区域宽度 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T11:36:58.392+08:00 |
| 66 | DARPHIN-NEW-MEMBER-SWITCH-LAYOUT-001 | 统一朵梵首次注册会员限制开关的尺寸与位置 | CONFIRMED | medium | self_hosted_agent | 是 | 2026-08-03T11:41:07.379+08:00 |
| 67 | LOOP-DASHBOARD-NAV-METRICS-VERTICAL-LAYOUT-001 | 统一 Dashboard 顶部统计为上标签下数字布局 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T11:48:27.673+08:00 |
| 68 | RS-ADMIN-DECORATION-RESTORE-OPERATION-DATE-001 | 修复 RS 管理端装修版本恢复操作时间回显 | CONFIRMED | high | self_hosted_agent | 是 | 2026-08-03T11:50:39.895+08:00 |
| 69 | RS-ADMIN-CMS-VALIDITY-LIST-DATETIME-001 | 补全 RS 管理端 CMS 列表有效期时间回显 | CONFIRMED | high | self_hosted_agent | 是 | 2026-08-03T14:03:29.654+08:00 |
| 70 | LOOP-DEEPSEEK-LIVE-SMOKE-001 | 验证 DeepSeek Agent 真实只读执行链路 | FAILED | critical | self_hosted_agent | 是 | 2026-08-03T14:24:00.876+08:00 |
| 71 | LOOP-DEEPSEEK-SAFE-DIAGNOSTICS-001 | 补充 DeepSeek Agent 安全失败诊断 | CANCELLED | critical | self_hosted_agent | 是 | 2026-08-03T14:29:04.340+08:00 |
| 72 | LOOP-DASHBOARD-HEADER-TIME-SETTINGS-DRAWER-001 | 简化 Dashboard 更新时间并增加设置抽屉入口 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T14:45:06.894+08:00 |
| 73 | LOOP-CODEX-CLI-DISPATCHER-001 | 实现 Codex CLI 单一调度器与安装脚本 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T14:46:52.952+08:00 |
| 74 | LOOP-CODEX-CLI-LIVE-SMOKE-001 | 验证 Codex CLI 真实只读执行链路 | CANCELLED | critical | self_hosted_agent | 是 | 2026-08-03T14:46:52.952+08:00 |
| 75 | LOOP-CODEX-CLI-RUNNER-001 | 实现单任务 Codex CLI Runner | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T14:46:52.952+08:00 |
| 76 | LOOP-DASHBOARD-PROFILE-LEVEL-COLORS-001 | 用分级背景色区分 Dashboard 执行档位卡片 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T15:21:13.700+08:00 |
| 77 | LOOP-DASHBOARD-RESET-HEADER-FILTERS-ON-STATUS-CHANGE-001 | Dashboard 默认显示待执行并在状态切换时重置筛选 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T15:28:11.806+08:00 |
| 78 | LOOP-DASHBOARD-CONTEXTUAL-FILTER-OPTIONS-001 | Dashboard 表头筛选仅显示当前有数据的选项 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T15:30:35.640+08:00 |
| 79 | LOOP-DASHBOARD-PROFILE-CODEX-LEVEL-LABELS-001 | 将 Dashboard 执行档位卡片改为 Codex 1 至 6 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T15:33:31.706+08:00 |
| 80 | LOOP-DASHBOARD-DB-REVISION-LABEL-001 | 将 Dashboard 数据库版本文案改为 DB.V | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T15:36:38.192+08:00 |
| 81 | LOOP-DASHBOARD-ARCHIVE-ACTION-001 | 在 Dashboard 已结束列表增加任务归档操作 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T15:40:01.630+08:00 |
| 82 | LOOP-DASHBOARD-ARCHIVED-METRIC-001 | 将 Dashboard 完成率统计替换为已归档数量 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T15:45:41.394+08:00 |
| 83 | LOOP-DASHBOARD-COMPACT-PROFILE-SIDEBAR-001 | 缩窄 Dashboard 任务档位侧栏并统一档位文案 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T16:37:52.777+08:00 |
| 84 | LOOP-CODEX-CLI-LIVE-SMOKE-002 | 重新验证 Codex CLI 用户配置兼容后的真实只读执行链路 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T16:48:06.825+08:00 |
| 85 | LOOP-CODEX-CLI-USER-CONFIG-COMPAT-001 | 修复 Codex CLI Runner 对本机用户配置与 custom provider 的兼容 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-03T16:48:06.825+08:00 |
| 86 | LOOP-DASHBOARD-RUNTIME-PROFILE-SCHEDULER-MATRIX-001 | 将 Dashboard 任务档位改为双环境六行调度矩阵 | CANCELLED | high | self_hosted_agent | 是 | 2026-08-03T17:58:11.681+08:00 |
| 87 | LOOP-CAPABILITY-EXECUTION-PROFILE-SCHEMA-001 | 建立五级任务能力与多平台执行配置 Schema | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-04T13:44:08.649+08:00 |
| 88 | LOOP-EXECUTABLE-QUEUE-PLATFORM-CAPACITY-001 | 实现统一可执行逻辑队列与两级并发容量 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-04T13:44:19.498+08:00 |
| 89 | LOOP-RUNTIME-EXECUTION-PROFILE-POLICY-001 | 让 CLI 与自建 Agent 执行完整五级执行配置 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-04T13:44:29.938+08:00 |
| 90 | LOOP-CODEX-AUTOMATION-L1-L5-COMPAT-001 | 迁移 Codex 客户端 Worker 到五级能力领取契约 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-04T13:44:38.995+08:00 |
| 91 | LOOP-SCHEDULING-L1-L5-DASHBOARD-E2E-001 | 更新五级多平台调度看板并完成兼容验收 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-04T13:45:01.209+08:00 |
| 92 | LOOP-SCHEDULING-L1-L5-PRODUCTION-CUTOVER-001 | 人工切换五级多平台调度架构 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-04T13:46:52.955+08:00 |
| 93 | RS-MINI-PRODUCT-LIST-RANDOM-REFRESH-001 | RS 小程序商品列表增加随机下拉刷新 | CONFIRMED | high | self_hosted_agent | 是 | 2026-08-05T10:43:46.114+08:00 |
| 94 | LOOP-DASHBOARD-SCHEDULER-CAPACITY-LV1-LV5-001 | 将 Dashboard 调度容量精简为 Lv1 至 Lv5 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-05T10:46:19.192+08:00 |
| 95 | LOOP-MANAGED-SECRET-DEEPSEEK-E2E-001 | 验证托管密钥驱动的 DeepSeek 真实执行链路 | FAILED | critical | self_hosted_agent | 是 | 2026-08-05T11:07:54.337+08:00 |
| 96 | LOOP-SECRET-API-DASHBOARD-001 | 增加 Dashboard Provider 密钥管理界面与 Secret API | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-05T11:07:54.337+08:00 |
| 97 | LOOP-SECRET-STORE-INITIALIZATION-001 | 建立统一 SecretStore 与密钥初始化能力 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-05T11:07:54.337+08:00 |
| 98 | LOOP-SERVER-SECRET-BACKEND-001 | 接入服务器 SecretStore 后端与远程管理安全 | CANCELLED | critical | self_hosted_agent | 是 | 2026-08-05T11:07:54.337+08:00 |
| 99 | LOOP-DASHBOARD-ACTION-COLUMN-RIGHT-PADDING-001 | 为 Dashboard 操作列增加右侧边距 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-05T12:09:14.500+08:00 |
| 100 | LOOP-CODEX-STALE-TIMEOUT-RECOVERY-001 | 修正 Codex 客户端停滞超时与 scope 隔离状态机 | CONFIRMED | blocker | self_hosted_agent | 是 | 2026-08-05T13:52:44.395+08:00 |
| 101 | LOOP-DEEPSEEK-SAFE-DIAGNOSTICS-002 | 恢复 DeepSeek Provider 安全失败诊断 | CONFIRMED | blocker | self_hosted_agent | 是 | 2026-08-05T15:33:22.328+08:00 |
| 102 | LOOP-DEEPSEEK-TOOL-LOOP-CONVERGENCE-001 | 修正 DeepSeek 工具循环收敛与最终结果协议 | CONFIRMED | blocker | self_hosted_agent | 是 | 2026-08-05T15:33:22.328+08:00 |
| 103 | DARPHIN-ADMIN-PRODUCTION-BUILD-001 | 构建 Darphin 管理端正式版发布包 | CONFIRMED | high | self_hosted_agent | 是 | 2026-08-05T16:21:12.522+08:00 |
| 104 | DARPHIN-MINI-PRODUCTION-CONFIG-001 | 将 Darphin 小程序切换为正式环境配置 | CONFIRMED | high | self_hosted_agent | 是 | 2026-08-05T16:21:12.739+08:00 |
| 105 | LOOP-MANAGED-SECRET-DEEPSEEK-E2E-002 | 重新验证修复后的托管密钥 DeepSeek 真实 E2E | FAILED | critical | self_hosted_agent | 是 | 2026-08-05T18:16:09.608+08:00 |
| 106 | LOOP-DEEPSEEK-FINAL-SHAPE-DIAGNOSTICS-001 | 增加 DeepSeek final 安全响应形状诊断 | CONFIRMED | blocker | self_hosted_agent | 是 | 2026-08-06T09:47:48.941+08:00 |
| 107 | RS-ADMIN-DECORATION-STATIC-PREVIEW-001 | 将 RS 管理端装修预览改为静态预览 | CONFIRMED | high | self_hosted_agent | 是 | 2026-08-06T09:49:01.754+08:00 |
| 108 | LOOP-MANAGED-SECRET-DEEPSEEK-E2E-003 | 验证 DeepSeek final 安全形状诊断后的真实 E2E | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-06T10:14:21.313+08:00 |
| 109 | LOOP-DASHBOARD-DEEPSEEK-RUNTIME-LABEL-001 | 将 Dashboard 的 Self-hosted Agent 展示名改为 DeepSeek | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-06T10:26:42.013+08:00 |
| 110 | LOOP-DASHBOARD-SCHEDULER-CAPACITY-CARDS-001 | 将 Dashboard 调度容量五个档位改为纵向卡片 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-06T10:29:28.659+08:00 |
| 111 | LOOP-DEEPSEEK-DIAGNOSTIC-PERSISTENCE-001 | 持久化 DeepSeek 允许列表式安全诊断 | CANCELLED | blocker | self_hosted_agent | 是 | 2026-08-06T10:33:12.535+08:00 |
| 112 | LOOP-DEEPSEEK-FINAL-CONTRACT-REPAIR-001 | 修正 DeepSeek final 类型契约与受限结构修正 | CANCELLED | blocker | self_hosted_agent | 是 | 2026-08-06T10:33:12.535+08:00 |
| 113 | LOOP-OPERATOR-ACTIVE-TASK-DEDUP-001 | 将 Operator 语义查重限定为当前任务 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-06T10:33:58.309+08:00 |
| 114 | LOOP-PLANNER-PREFLIGHT-STATE-001 | 建立 Planner 预检状态机与任务契约 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-06T13:44:33.693+08:00 |
| 115 | LOOP-PLANNER-HYBRID-SCOPE-LOCK-001 | 实现 Planner 结果驱动的混合 scope 锁与待执行排队 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-06T13:44:48.278+08:00 |
| 116 | LOOP-PLANNER-CLIENT-INTEGRATION-001 | 接入 Planner 客户端角色、只读边界与初始化 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-06T13:44:57.992+08:00 |
| 117 | LOOP-DASHBOARD-PLANNER-WORKFLOW-001 | 接入 Dashboard 的 Planner 预检工作流与分类 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-06T13:45:07.268+08:00 |
| 118 | LOOP-DASHBOARD-REMOVE-CURRENT-TASK-METRIC-001 | 移除 Dashboard 顶部当前任务指标 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-10T10:44:25.909+08:00 |
| 119 | LOOP-DASHBOARD-REMOVE-BRAND-TEXT-001 | 移除 Dashboard 顶部品牌标题文本并压缩布局 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-10T11:06:28.995+08:00 |
| 120 | LOOP-DASHBOARD-REMOVE-STATUS-FILTER-BAR-001 | 移除 Dashboard 任务表头状态筛选栏 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-10T11:48:10.548+08:00 |
| 121 | LOOP-OPERATIONS-CONFIG-UI-FOUNDATION-001 | 新增独立运维配置页面与配置目录基础 | CONFIRMED | high | self_hosted_agent | 是 | 2026-08-10T12:09:35.637+08:00 |
| 122 | LOOP-DASHBOARD-HUMAN-ATTENTION-STATUS-001 | 修正 Dashboard 的等待人工状态分类与计数 | CANCELLED | high | self_hosted_agent | 是 | 2026-08-10T13:53:13.571+08:00 |
| 123 | LOOP-DASHBOARD-STATUS-TOOLTIPS-001 | 为顶部任务状态统计添加悬浮说明 | CANCELLED | critical | self_hosted_agent | 是 | 2026-08-10T14:17:56.361+08:00 |
| 124 | LOOP-REGRESSION-TEST-SUITE-REORGANIZE-001 | 整理并按职责拆分 Local Agent Loop 回归测试 | CANCELLED | critical | self_hosted_agent | 是 | 2026-08-11T14:42:53.926+08:00 |
| 125 | RS-ADMIN-UNLOCK-CONTACT-REFRESH-20260812 | 管理端解锁手机号或邮箱后刷新用户详情回显 | CONFIRMED | medium | self_hosted_agent | 是 | 2026-08-12T11:43:47.859+08:00 |
| 126 | RS-MINI-COUPON-GIFTCARD-DESCRIPTION-DIALOG-20260812 | RS小程序优惠券和礼品卡说明支持弹窗查看完整内容 | CONFIRMED | medium | self_hosted_agent | 是 | 2026-08-12T11:50:22.007+08:00 |
| 127 | RS-MINI-GIFTCARD-SELECTION-INDICATOR-20260812 | RS小程序礼品卡增加选中对勾和状态文本 | CONFIRMED | medium | self_hosted_agent | 是 | 2026-08-12T11:54:22.840+08:00 |
| 128 | RS-PC-ERROR-NOTICE-CENTER-20260812 | RS PC端全站错误提醒统一改为页面中间显示 | SUCCEEDED | medium | self_hosted_agent | 否 | 2026-08-12T11:54:22.840+08:00 |
| 129 | RS-ADMIN-RESOURCE-VIDEO-UPLOAD-STATUS-20260812 | RS管理端资源库修复多视频上传卡住并展示上传状态 | SUCCEEDED | medium | self_hosted_agent | 否 | 2026-08-12T14:02:24.575+08:00 |
| 130 | LOOP-WORKER-TEMP-CLEANUP-NONBLOCKING-20260812 | Worker临时日志清理失败不得误阻塞业务任务 | CONFIRMED | critical | self_hosted_agent | 是 | 2026-08-12T14:48:03.929+08:00 |
| 131 | RS-PC-ERROR-DIALOG-STYLE-ALIGN-20260812 | RS PC端错误提醒统一复用现有中间弹窗样式 | SUCCEEDED | medium | self_hosted_agent | 否 | 2026-08-12T15:59:06.077+08:00 |
| 132 | RS-ADMIN-MULTI-VIDEO-SUBMIT-SPINNER-20260812 | RS管理端修复多视频确定上传后持续转圈 | SUCCEEDED | high | self_hosted_agent | 否 | 2026-08-12T16:08:04.070+08:00 |
| 133 | RS-MINI-GIFTCARD-SELECTION-COMPACT-20260812 | RS小程序礼品卡移除选中文本并优化选择标识布局 | CANCELLED | medium | self_hosted_agent | 是 | 2026-08-12T17:14:58.095+08:00 |
| 134 | RS-PC-CARD-DESCRIPTION-OVERFLOW-DIALOG-20260812 | RS PC端卡片说明单行省略并支持查看完整内容 | SUCCEEDED | medium | self_hosted_agent | 否 | 2026-08-12T17:18:06.066+08:00 |
| 135 | RS-MINI-CARD-CONTROLS-ALIGNMENT-20260812 | RS小程序统一优惠券和礼品卡选择框与信息按钮布局 | CONFIRMED | medium | self_hosted_agent | 是 | 2026-08-12T17:51:23.275+08:00 |
| 136 | LOOP-DASHBOARD-SCOPE-PROJECT-DISPLAY-20260813 | 修复Dashboard文件级和模块级任务项目显示未识别 | CANCELLED | critical | self_hosted_agent | 是 | 2026-08-13T15:44:24.895+08:00 |

