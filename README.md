# 每日铜价追踪 · copper-price-tracker

沪铜期货 + 现货 + LME 的每日行情追踪站。零服务器、零数据库：GitHub Actions 定时抓数 → 写 `data.json` → 静态页渲染 → 微信/钉钉推送。

**线上地址**：https://ToniaXuu.github.io/copper-price-tracker/

> 数据仅供参考，不构成任何投资或采购建议。

---

## 1. 这个站解决什么

电力设备（复合绝缘横担、变压器、线缆）的成本大头是铜。需要每天知道三件事：

| 指标     | 含义                | 数据源                             |
| ------ | ----------------- | ------------------------------- |
| **沪铜结算价** | 主指标，人民币/吨，2005 年至今 | 新浪 K 线（L1）+ 上期所官方结算价（L2 校验）     |
| **现货均价** | 1# 电解铜多供应商均价，用来算基差 | 生意社（两步 cookie 握手）               |
| **沪伦比** | 沪铜结算价 ÷ LME 铜价，看内外盘强弱 | 新浪 LME 铜 3 月（`hf_CAD`）          |

推送方式：Server 酱（微信）+ 钉钉机器人，每天 **08:30** 一次。

---

## 2. 口径定义（写死，避免日后自相矛盾）

改代码前先看这一节。口径不一致是这个项目最容易出的错，且**不会报错**。

| 项       | 定义                                              |
| ------- | ----------------------------------------------- |
| 主指标基准价  | 沪铜连续（`nf_CU0`）**结算价**；早期无结算价时回退收盘价              |
| `change` | 按**结算价**算的日变动（`settle_d − settle_{d−1}`），**不是**收盘价差 |
| `changePct` | `change ÷ 前一日结算价 × 100`                          |
| 基差      | 现货均价 − 期货结算价（正 = 升水，负 = 贴水）                      |
| 沪伦比     | 沪铜结算价 ÷ LME 美元/吨，**未扣汇率与增值税**                   |

`data.json` 的 `meta.changeBasis` 固定为 `"settle"`，用于自证。页面上的四张涨跌卡、推送正文、连续涨跌统计，全部走同一个口径。

---

## 3. 架构：零服务器

```
GitHub Actions (cron 30 0 * * *，即 08:30 CST)
   └─ scripts/update_copper.py
        ├─ 五源抓取 + 健康登记 + 校验 + 推导（基差/沪伦比）
        ├─ 写 data.json
        └─ 主指标全源失效 → exit(1)（workflow 红灯，不发假消息）
   └─ git commit & push data.json
   └─ scripts/send_notification.py  →  微信 + 钉钉
                ↓
GitHub Pages 静态页  fetch('./data.json')  →  渲染
```

为什么要等收盘：上期所 `kx{date}.dat` 里的 `SETTLEMENTPRICE` / `CLOSEPRICE` 在盘中是**空字符串**，08:30 拿到的正是上一交易日的完整结算数据。

---

## 4. 数据源与降级链

```
主指标 · 沪铜期货
  ├─ L1  新浪 InnerFuturesNewService.getDailyKLine  历史序列（首选，2005 至今 5289 条）
  ├─ L2  上期所 kx{date}.dat                        官方结算价（权威校验，比对 12 个交割月）
  └─ L3  新浪 hq.sinajs.cn  nf_CU0                  盘中快照兜底

副指标 · 现货电解铜
  └─ 生意社 plist-1-61（JS 挑战页 → 带 HW_CHECK cookie 重放）

参考指标 · 国际盘
  └─ 新浪 hf_CAD（LME 铜 3 月）
```

每个源都落一条健康记录（`data.json` 的 `sourceHealth`），页面「数据源状态」区原样展示；有源异常时推送标题改成 `⚠️ 铜价数据源异常 | 请检查`。

---

## 5. 本地运行

```bash
pip install requests

# 抓数（默认写 data.json）
python scripts/update_copper.py

# 只看结果不写盘
python scripts/update_copper.py --dry-run

# 强制重抓（忽略增量判断）
python scripts/update_copper.py --force

# 跳过现货源（生意社改版时应急）
python scripts/update_copper.py --skip-spot

# 预览本地页面（必须走 HTTP，file:// 下 fetch data.json 会被 CORS 拦）
python -m http.server 8000
```

推送脚本本地自测：

```bash
# 不配置 SERVERCHAN_SENDKEY / DINGTALK_WEBHOOK 时只打印不发送，不会报错
python scripts/send_notification.py
```

---

## 6. 部署

1. 推到 GitHub，仓库 **Settings → Pages** 选 `main` 分支根目录。
2. **Settings → Secrets and variables → Actions** 加三个 Secret：

| Secret 名             | 用途                             |
| ------------------- | ------------------------------ |
| `SERVERCHAN_SENDKEY` | Server 酱 SendKey（微信推送）         |
| `DINGTALK_WEBHOOK`   | 钉钉机器人 Webhook URL               |
| `DINGTALK_SECRET`    | 钉钉加签密钥（用加签模式时必须，否则留空）           |

3. Actions 的 `schedule` 常延迟 5~20 分钟，精确触发另用 **cron-job.org** 定时 `POST` 仓库的 `workflow_dispatch` 接口兜底。

通知步骤带 `if: always()`：数据源全灭导致抓取脚本 `exit(1)` 时，告警仍然送得出去。

---

## 7. 文件结构

```
├── index.html                          # 行情主页面（ECharts 走 CDN）
├── releases.html                       # 版本发布记录（独立页，与主页共用同一套设计令牌）
├── data.json                           # 行情数据（Actions 自动提交，别手改）
├── releases.json                       # 版本记录数据（发版时手改）
├── manifest.json / sw.js               # PWA
├── scripts/
│   ├── update_copper.py                # 数据层：五源降级 + 健康登记 + 校验
│   ├── send_notification.py            # 推送层：双通道 + 涨跌特化标题
│   ├── generate_icons.py               # 生成 PWA 图标
│   ├── verify_static.py                # 静态校验：双页 JSON / JS / DOM id / 资源路径 / 令牌一致性
│   └── runtime_probe.py                # 运行时探针：自写 CDP 客户端取真实渲染数值
└── .github/workflows/update-copper.yml # 定时工作流
```

`sw.js` 里静态资源走「缓存优先」，`data.json` 与 `releases.json` 走「网络优先」并绕开 HTTP 缓存 —— 否则会拿到陈旧数据。

两条容易忘的连带约定：

- 两个页面共用一套设计令牌（`:root` + 两处暗色覆盖），**改配色必须两个文件一起改**。`verify_static.py` 会逐值比对 79 个令牌，漂移直接判失败。
- 改完任一页面，**必须同步升 `sw.js` 的 `CACHE_NAME`**。页面本身走「缓存优先」，不升版本号老访客会一直看到旧页面。

---

## 8. 四个必须记住的坑（都实测踩过）

**坑 1：新浪 `CU0` 是僵尸代码，必须用 `nf_CU0`**

```
hq.sinajs.cn/list=CU0     → 停在 2024-07-17 的死数据   ❌
hq.sinajs.cn/list=nf_CU0  → 当前真实行情                ✅
```

两者都返回 200、格式都合法，**数值却相差两年**（78,730 vs 109,300）。它不会报错，只会让你照着两年前的铜价做采购决策。脚本里因此有「日期必须 ≥ 今天 − 7 天」的断言。

**坑 2：上期所盘中结算价是空字符串**

```json
{"DELIVERYMONTH": "2611", "OPENPRICE": 109280, "SETTLEMENTPRICE": "", "CLOSEPRICE": ""}
```

所以定时必须在收盘结算之后（这也是 08:30 而非盘中跑的原因）。脚本遇到空值跳过并记健康日志。

**坑 3：ECharts 的 `dataZoom.startValue` 对 category 轴是精确值匹配**

按天减出来的「一年前」多半不是交易日，传进去匹配不上，ECharts **静默回退成全量**——图表看着没反应，而文字提示（用字符串比较）还是对的，两边不一致。所以 `computeRangeStart()` 必须把起点对齐到「首个 ≥ 目标日」的真实交易日。

顺带一个同源陷阱：`new Date(iso + 'T00:00:00+08:00').toISOString().slice(0,10)` 会**少一天**（东八区午夜等于 UTC 前一天 16:00）。日期算术一律走 `setUTCDate`。

**坑 4：`Array.prototype.slice.call()` 对 Map/Set 的 Iterator 无效**

```js
const days = Array.prototype.slice.call(byDate.keys());  // ❌ 得到 []（Iterator 没有 length）
const days = Array.from(byDate.keys());                  // ✅ 正确
```

`slice.call` 只对**类数组**（有 `length`）有效：`querySelectorAll` 返回的 NodeList 可以，`Map.keys()` / `Set.values()` 返回的 Iterator 不行。

它不报错，只静默给你空数组 —— 于是 `days[0]` 是 `undefined`，下游再取属性时才炸，**报错位置离根因很远**。版本发布记录页第一次跑就是这样：统计数字全对（在出错前已渲染），整条时间线却一片空白。`node --check` 这类静态检查完全抓不到，只有运行时探针能发现。

---

## 9. 验证

改动后跑这两条，别只改不验：

```bash
python scripts/verify_static.py    # 双页：JSON / JS 语法 / DOM id 闭环 / 资源路径 / 令牌一致性
python scripts/runtime_probe.py    # 无头 Chrome 取真实渲染值（主页 + 版本记录页）
```

`runtime_probe.py` 自带一个零依赖 CDP 客户端，直接连 DevTools 协议，检查项包括：

- 数据真加载（未走 `FALLBACK_DATA`）、期货序列条数与 `data.json` 一致
- 三个 ECharts 实例存在且数据点数正确、现货线诚实断线（`connectNulls:false`）
- **涨红跌绿**：按国内惯例验证真实计算色（涨 `rgb(201,42,42)` / 跌 `rgb(4,120,87)`）
- 主题三态切换（auto/light/dark）+ 切换后无白屏、图表不丢
- 区间切换 `dataZoom` 百分比真的收窄（防上面坑 3 复发）
- 换算器交互、全源失效红色告警、运行时报错

---

## 10. 与油价项目的关系

本项目是 [fuel-price-tracker](https://github.com/ToniaXuu/fuel-price-tracker) 的姊妹项目，复用同一套零服务器架构（Actions + Pages + 双通道推送 + 数据源健康登记）。不同的是：铜走的是**期货结算价**口径，多一层上期所官方数据校验，且现货历史无免费长序列 —— 只能从上线当天起自建累积，页面上明示起点，**不伪造历史**。
