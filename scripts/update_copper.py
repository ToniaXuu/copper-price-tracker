#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪铜价格数据更新脚本
====================
追踪沪铜连续合约 + 国内现货 1# 电解铜 + LME 铜，自动更新 data.json。

数据来源（按优先级降级）:
  主指标 · 沪铜期货
    L1 新浪 InnerFuturesNewService.getDailyKLine  历史序列（2005 至今，含开高低收/结算/持仓）
    L2 上期所 kx{YYYYMMDD}.dat                    官方日行情（权威校验 + 兜底）
    L3 新浪 hq.sinajs.cn nf_CU0                   实时盘中（快照登记，不入历史序列）
  副指标 · 现货 1# 电解铜
    L4 生意社 plist-1-61-1（两步 cookie 握手）    多供应商均价
  参考指标 · 国际盘
    L5 新浪 GlobalFuturesService 日 K 线          LME 铜历史序列（2016 至今）
    L6 新浪 hq.sinajs.cn hf_CAD                   实时兜底 / 交叉校验

用法:
  python update_copper.py               # 自动检测并更新
  python update_copper.py --dry-run     # 仅检查，不写文件
  python update_copper.py --force       # 强制全量刷新（重取历史 + 现货回填）
  python update_copper.py --no-spot     # 跳过现货（调试用）

⚠️ 三个必须记住的坑（实测踩出，改动前先读）:
  1. 新浪必须用 `nf_CU0`，`CU0` 是停在 2024-07-17 的僵尸代码。
     两者都返回 200 和合法格式，数值却相差两年 —— 不会报错，是最危险的一个坑。
     本脚本用「日期必须 ≥ 今天-7 天」的断言把它堵死。
  2. SHFE 的 SETTLEMENTPRICE / CLOSEPRICE 在盘中是空字符串。
     所以定时任务必须放在收盘结算之后（本项目 08:30，取的是前一交易日）。
  3. 生意社是两步握手：首次请求只给 636 字节 JS 挑战页（内含 32 位 hex token），
     带上 HW_CHECK=<token> cookie 重放才拿到真页面。token 跨运行会轮换，每次都要重握手。
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Windows 控制台 UTF-8 编码兼容
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import requests
except ImportError:
    print("❌ 需要安装 requests 库: pip install requests")
    sys.exit(1)

# ============ 路径 ============
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_FILE = PROJECT_ROOT / "data.json"
HEALTH_FILE = PROJECT_ROOT / ".source_health.json"

# ============ 数据源地址 ============
# ⚠️ nf_CU0 的 nf_ 前缀不能去掉（见文件头 坑 1）
SINA_RT_URL = "https://hq.sinajs.cn/list=nf_CU0"
SINA_LME_URL = "https://hq.sinajs.cn/list=hf_CAD"
SINA_KLINE_URL = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
                  "var%20t=/InnerFuturesNewService.getDailyKLine?symbol=CU0")
# LME 铜（伦铜）日 K：CAD = LME Copper 3M。带回 2016 至今约 2500 条，
# 因此沪伦比序列可以一次性回填，不必从上线当天从零累积。
SINA_LME_KLINE_URL = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
                      "var%20t=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=CAD")
# ⚠️ 必须带 www：裸域 shfe.com.cn 在部分网络下直接 ProxyError
SHFE_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{date}.dat"
SPOT_URL = "https://www.100ppi.com/mprice/plist-1-61-1.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
SINA_HEADERS = {**HEADERS, "Referer": "https://finance.sina.com.cn"}
SHFE_HEADERS = {**HEADERS, "Referer": "https://www.shfe.com.cn/"}

# ============ 校验阈值 ============
# 沪铜历史区间：2005 年约 28,000 元/吨 → 2026 年 109,000 元/吨。
# 区间给足余量，只拦「明显不是铜价」的脏数据（比如误抓到 78,730 的僵尸值虽然仍在区间内，
# 所以另有「日期新鲜度」断言兜底，见 validate_futures）。
PRICE_MIN = 20_000
PRICE_MAX = 300_000
DAILY_CHANGE_LIMIT = 0.09      # 单日收盘涨跌幅上限 9%（铜很难超过，超了说明数据错）
ROLLOVER_JUMP_PCT = 3.0        # 单日涨跌幅 > 3% 时标注「疑似换月跳空/异常波动」
SPOT_DEVIATION_LIMIT = 0.15    # 现货相对期货结算价的合理偏离（基差率）
LME_MIN, LME_MAX = 3_000, 30_000   # LME 铜 美元/吨

# 现货序列最多回填多少个交易日（生意社页面只给最近 10 个交易日）
SPOT_BACKFILL_DAYS = 15

# ============ 数据源健康登记 ============
# 为什么需要这张表：源失效时 fetcher 会 `return []`/None，调用方看到的就是「没有新记录」，
# 任务照样绿着、通知里写「无变化」—— 数据静默停更，没有任何地方会报警。
# 所以每个 fetcher 都必须登记自己的结果，main() 据此汇总并在源全灭时明确失败。
SOURCE_HEALTH = {}


def mark_source(name, ok, detail=""):
    SOURCE_HEALTH[name] = {"ok": bool(ok), "detail": detail}


def save_health():
    """把健康状态写到磁盘，供通知脚本读取（失败不应影响主流程）。"""
    try:
        with open(HEALTH_FILE, "w", encoding="utf-8") as f:
            json.dump(SOURCE_HEALTH, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ 健康状态落盘失败: {e}")


# ============ 通用工具 ============

def to_float(v):
    """'' / None / 非法 → None；其余转 float"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(v):
    f = to_float(v)
    return None if f is None else int(round(f))


def num(v):
    """输出用：把 float 收敛成 int（价格在元/吨 量级没有小数意义），None 保持 None"""
    if v is None:
        return None
    return int(round(v)) if abs(v - round(v)) < 1e-9 else round(v, 2)


# ============ L1: 新浪日 K 线（历史序列）============

def fetch_sina_kline():
    """抓取沪铜连续日 K 线全量历史。

    返回 list[{date, open, high, low, close, settle, volume, openInterest}]（按日期升序）。
    这是主指标的唯一权威序列：2005 至今一条线，无需处理主力换月拼接。
    """
    try:
        resp = requests.get(SINA_KLINE_URL, headers=SINA_HEADERS, timeout=25)
        resp.encoding = "utf-8"
    except Exception as e:
        mark_source("新浪K线", False, f"请求失败: {str(e)[:100]}")
        return []

    if resp.status_code != 200:
        mark_source("新浪K线", False, f"HTTP {resp.status_code}")
        return []

    # 响应是 JSONP: var t=([{...},{...}]);  也可能直接是数组
    m = re.search(r"\((\[.*\])\)\s*;?\s*$", resp.text.strip(), re.S)
    payload = m.group(1) if m else resp.text.strip()
    try:
        raw = json.loads(payload)
    except Exception as e:
        mark_source("新浪K线", False, f"JSON 解析失败（页面结构可能已变）: {str(e)[:80]}")
        return []

    if not isinstance(raw, list) or not raw:
        mark_source("新浪K线", False, "返回空数组")
        return []

    rows = []
    for r in raw:
        d = str(r.get("d", "")).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            continue
        rows.append({
            "date": d,
            "open": to_int(r.get("o")),
            "high": to_int(r.get("h")),
            "low": to_int(r.get("l")),
            "close": to_int(r.get("c")),
            "settle": to_int(r.get("s")),
            "volume": to_int(r.get("v")),
            "openInterest": to_int(r.get("p")),
        })
    rows.sort(key=lambda x: x["date"])

    if not rows:
        mark_source("新浪K线", False, "解析出 0 条有效记录")
        return []

    # ---- 僵尸数据断言 ----
    # 这是本脚本最重要的一道闸：用错 CU0 会拿到停在 2024 年的序列，
    # 格式完全合法、绝不报错，只会让全站数字悄悄偏两年。
    newest = rows[-1]["date"]
    today = datetime.now().date()
    try:
        gap = (today - datetime.strptime(newest, "%Y-%m-%d").date()).days
    except Exception:
        gap = 999
    if gap > 15:
        mark_source("新浪K线", False,
                    f"数据陈旧：最新一条为 {newest}，距今 {gap} 天 —— "
                    f"疑似抓到了停更的僵尸代码（应使用 nf_CU0 对应的 CU0 K线）")
        return []

    mark_source("新浪K线", True,
                f"{len(rows)} 条，{rows[0]['date']} → {newest}（距今 {gap} 天）")
    return rows


# ============ L2: 上期所官方日行情（校验 + 兜底）============

def fetch_shfe_day(date_str):
    """抓取指定交易日期的上期所官方行情。

    返回 (status, data)：
      status = "ok"      当日已结算，data 为 dict
      status = "intraday" 文件存在但结算价为空（盘中）
      status = "missing"  该日无文件（非交易日 / 未发布）
      status = "error"    请求或解析失败
    data = {date, open, high, low, close, settle, volume, openInterest, contract}
           取的是当日 cu_f 持仓量最大的合约（即主力合约）
    """
    d = date_str.replace("-", "")
    url = SHFE_URL.format(date=d)
    try:
        resp = requests.get(url, headers=SHFE_HEADERS, timeout=20)
    except Exception:
        return "error", None

    if resp.status_code != 200:
        return "missing", None

    try:
        payload = resp.json()
    except Exception:
        # 非交易日返回 404 且正文是 HTML 错误页，这里统一按 missing 处理
        return "missing", None

    rows = payload.get("o_curinstrument", [])
    cu = []
    for x in rows:
        pid = str(x.get("PRODUCTID", "")).strip()
        dmonth = str(x.get("DELIVERYMONTH", "")).strip()
        # 过滤掉「小计」「总计」这类汇总行
        if pid == "cu_f" and dmonth.isdigit():
            cu.append(x)
    if not cu:
        return "error", None

    # 主力合约 = 持仓量最大者
    def oi(x):
        return to_int(x.get("OPENINTEREST")) or 0
    main = max(cu, key=oi)

    settle = to_float(main.get("SETTLEMENTPRICE"))
    close = to_float(main.get("CLOSEPRICE"))
    if settle is None and close is None:
        return "intraday", {"contract": f"cu{dmonth}"}

    return "ok", {
        "date": date_str,
        "contract": f"cu{str(main.get('DELIVERYMONTH', '')).strip()}",
        "open": to_int(main.get("OPENPRICE")),
        "high": to_int(main.get("HIGHESTPRICE")),
        "low": to_int(main.get("LOWESTPRICE")),
        "close": to_int(close) if close is not None else to_int(settle),
        "settle": to_int(settle) if settle is not None else to_int(close),
        "volume": to_int(main.get("VOLUME")),
        "openInterest": to_int(main.get("OPENINTEREST")),
        "contracts": len(cu),
    }


def shfe_nearest_settled(days_back=10):
    """向上回溯，取最近一个已结算交易日的上期所官方数据（L2 兜底用）。

    只回退 days_back 个自然日：再远就不是「最新行情」而是考古了。
    """
    today = datetime.now().date()
    for i in range(days_back):
        ds = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        status, data = fetch_shfe_day(ds)
        if status == "ok":
            return data
    return None


def shfe_verify(date_str, kline_close):
    """用上期所官方数据校验新浪 K 线同日的收盘价。

    判定方式：官方当日 cu 各交割月合约里，是否存在收盘价与新浪连续合约相同的那个。
    连续合约是主力拼接，正常情况下必然等于某个真实合约的收盘价。
    返回 (ok, detail)。
    """
    status, data = fetch_shfe_day(date_str)
    if status == "missing":
        return None, f"{date_str} 官方文件不存在（非交易日或未发布）"
    if status == "error":
        return None, "官方文件解析失败"
    if status == "intraday":
        return None, f"{date_str} 官方数据尚未结算（盘中）"

    # ok：重新拉全量合约做「同价」匹配
    d = date_str.replace("-", "")
    try:
        resp = requests.get(SHFE_URL.format(date=d), headers=SHFE_HEADERS, timeout=20)
        rows = resp.json().get("o_curinstrument", [])
    except Exception:
        return None, "官方文件二次读取失败"

    closes = []
    for x in rows:
        if str(x.get("PRODUCTID", "")).strip() != "cu_f":
            continue
        if not str(x.get("DELIVERYMONTH", "")).strip().isdigit():
            continue
        c = to_int(x.get("CLOSEPRICE"))
        if c:
            closes.append(c)

    if not closes:
        return None, "官方文件中无有效收盘价"

    if kline_close in closes:
        return True, (f"{date_str} 官方结算价 {data['settle']}、主力合约 {data['contract']}，"
                      f"新浪收盘 {kline_close} 在官方 {len(closes)} 个交割月报价中找到同价")
    return False, (f"{date_str} 官方 {len(closes)} 个交割月报价中未找到新浪收盘 {kline_close}"
                   f"（官方主力 {data['contract']} 收盘 {data['close']}）")


# ============ L3: 新浪实时（盘中快照，不入历史序列）============

def fetch_sina_realtime():
    """抓取沪铜连续实时行情。

    ⚠️ 只作为「盘中参考快照」写进 meta.realtime，**不写入 futures 历史序列** ——
    盘中价每天会变，混进结算序列会让历史数据反复被改写。

    返回 (data, ok, detail)；data = {price, high, low, settle, openInterest, date, time}
    """
    try:
        resp = requests.get(SINA_RT_URL, headers=SINA_HEADERS, timeout=15)
        resp.encoding = "gbk"
    except Exception as e:
        mark_source("新浪实时", False, f"请求失败: {str(e)[:100]}")
        return None, False, f"请求失败: {str(e)[:80]}"

    m = re.search(r'"([^"]*)"', resp.text)
    if not m or not m.group(1).strip():
        mark_source("新浪实时", False, "返回内容为空（接口可能已变更）")
        return None, False, "返回内容为空"

    parts = m.group(1).split(",")
    if len(parts) < 20:
        mark_source("新浪实时", False, f"字段数异常（{len(parts)} < 20）")
        return None, False, f"字段数异常 {len(parts)}"

    name = parts[0]
    # 字段位：0=名称 1=时间 2=开 3=高 4=低 6=买 7=卖 10=昨结 13=持仓 17=日期 18=交易所 19=品种
    now_date = parts[17].strip() if len(parts) > 17 else ""
    price = to_float(parts[8]) or to_float(parts[7]) or to_float(parts[2])
    data = {
        "name": name,
        "price": num(price),
        "open": num(to_float(parts[2])),
        "high": num(to_float(parts[3])),
        "low": num(to_float(parts[4])),
        "prevSettle": num(to_float(parts[10])),
        "openInterest": num(to_float(parts[13])),
        "date": now_date,
        "time": parts[1].strip(),
    }

    # ---- 僵尸数据断言（与 K 线同款闸门）----
    # hq.sinajs.cn/list=CU0 会返回停在 2024-07-17 的死数据，且格式完全合法。
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", now_date):
        mark_source("新浪实时", False, f"日期字段异常: {now_date!r}")
        return None, False, f"日期字段异常: {now_date!r}"

    gap = (datetime.now().date() - datetime.strptime(now_date, "%Y-%m-%d").date()).days
    if gap > 7:
        mark_source("新浪实时", False,
                    f"数据陈旧：实时行情日期为 {now_date}（距今 {gap} 天）—— "
                    f"疑似抓到僵尸代码，应使用 nf_CU0 而非 CU0")
        return None, False, f"数据陈旧（{now_date}，距今 {gap} 天）"

    if not price or not (PRICE_MIN <= price <= PRICE_MAX):
        mark_source("新浪实时", False, f"价格越界: {price}")
        return None, False, f"价格越界: {price}"

    detail = f"{name} {num(price)} 元/吨 @ {now_date} {data['time']}"
    mark_source("新浪实时", True, detail)
    return data, True, detail


# ============ L4: 生意社现货（两步 cookie 握手）============

def fetch_spot_100ppi():
    """抓取生意社「1# 电解铜 / 标准阴极铜 Cu-CATH-2」多供应商报价。

    两步握手（见文件头 坑 3）：
      第 1 步 → 636 字节 JS 挑战页，内含 32 位 hex token
      第 2 步 → 带 HW_CHECK=<token> cookie 重放，拿到完整页面

    返回 list[{date, price, low, high, count, suppliers:[{name,price}]}]，按日期升序。
    price 为多供应商算术平均（四舍五入到元）。
    """
    session = requests.Session()
    html = ""
    handshake = False

    try:
        r1 = session.get(SPOT_URL, headers=HEADERS, timeout=25)
        r1.encoding = "utf-8"
        html = r1.text
    except Exception as e:
        mark_source("生意社现货", False, f"请求失败: {str(e)[:100]}")
        return []

    # 命中挑战页特征（体积很小 + 含 32 位 hex token）→ 二次握手
    if "电解铜" not in html:
        tok = re.search(r'"([0-9a-f]{32})"', html)
        if not tok:
            mark_source("生意社现货", False,
                        f"既非报价页也非挑战页（长度 {len(html)}），页面结构可能已变"
                        if html else "返回空内容")
            return []
        try:
            r2 = session.get(SPOT_URL, headers=HEADERS,
                             cookies={"HW_CHECK": tok.group(1)}, timeout=25)
            r2.encoding = "utf-8"
            html = r2.text
            handshake = True
        except Exception as e:
            mark_source("生意社现货", False, f"握手重放失败: {str(e)[:100]}")
            return []

    if "电解铜" not in html:
        mark_source("生意社现货", False,
                    f"握手后仍未拿到报价数据（长度 {len(html)}），页面结构可能已变")
        return []

    # ---- 解析报价表 ----
    by_date = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(cells) < 8:
            continue
        spec, supplier, quote, pub_date = cells[1], cells[2], cells[3], cells[7]
        if "电解铜" not in spec and "阴极铜" not in spec:
            continue
        pm = re.search(r"(\d{4,6})\s*元\s*/\s*吨", quote)
        if not pm:
            continue
        dm = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", pub_date)
        if not dm:
            continue
        date_str = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
        by_date.setdefault(date_str, []).append({
            "name": supplier,
            "price": int(pm.group(1)),
        })

    if not by_date:
        mark_source("生意社现货", False, "页面已获取但未解析出任何电解铜报价")
        return []

    series = []
    for date_str in sorted(by_date):
        quotes = by_date[date_str]
        prices = [q["price"] for q in quotes]
        series.append({
            "date": date_str,
            "price": int(round(sum(prices) / len(prices))),
            "low": min(prices),
            "high": max(prices),
            "count": len(quotes),
            "suppliers": quotes,
        })

    newest = series[-1]
    mark_source("生意社现货", True,
                f"{'两步握手通过，' if handshake else ''}{len(series)} 个交易日 / "
                f"{sum(s['count'] for s in series)} 条报价，"
                f"最新 {newest['date']} 均价 {newest['price']} 元/吨（{newest['count']} 家）")

    # 只保留最近 SPOT_BACKFILL_DAYS 个交易日，避免页面数据里混入过陈报价
    return series[-SPOT_BACKFILL_DAYS:]


# ============ L5: 新浪 LME 铜历史日 K ============

def fetch_lme_kline():
    """抓取 LME 铜（伦铜）日 K 线全量历史。

    ⚠️ 与沪铜的差别：GlobalFutures 接口会把**今天这根未走完的 K 线**也返回，
    所以最后一条的 close 是实时变动的。这里照常写入（同日 upsert 会不断刷新它），
    但给当天那条打 `intraday` 标，页面/推送据此知道它还不是最终值。
    """
    try:
        resp = requests.get(SINA_LME_KLINE_URL, headers=SINA_HEADERS, timeout=25)
        resp.encoding = "utf-8"
    except Exception as e:
        mark_source("新浪LME", False, f"日K请求失败: {str(e)[:100]}")
        return []

    m = re.search(r"\((\[.*\])\)\s*;?\s*$", resp.text.strip(), re.S)
    payload = m.group(1) if m else resp.text.strip()
    try:
        raw = json.loads(payload)
    except Exception as e:
        mark_source("新浪LME", False, f"日K解析失败: {str(e)[:80]}")
        return []

    if not isinstance(raw, list) or not raw:
        mark_source("新浪LME", False, "日K返回空数组")
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for r in raw:
        d = str(r.get("date", "")).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            continue
        close = to_float(r.get("close"))
        if not close or not (LME_MIN <= close <= LME_MAX):
            continue
        row = {
            "date": d,
            "price": round(close, 2),
            "high": num(to_float(r.get("high"))),
            "low": num(to_float(r.get("low"))),
        }
        if d == today:
            row["intraday"] = True
        rows.append(row)
    rows.sort(key=lambda x: x["date"])

    if not rows:
        mark_source("新浪LME", False, "日K无有效记录")
        return []
    return rows


# ============ L6: 新浪 LME 实时（兜底 / 交叉校验）============

def fetch_lme():
    """抓取 LME 铜实时（新浪 hf_CAD）。

    用途有两个：日 K 拿不到时的兜底，以及和日 K 最后一条做交叉校验。
    返回 (data, ok, detail)，data = {date, price}
    """
    try:
        resp = requests.get(SINA_LME_URL, headers=SINA_HEADERS, timeout=15)
        resp.encoding = "gbk"
    except Exception as e:
        mark_source("新浪LME", False, f"请求失败: {str(e)[:100]}")
        return None, False, f"请求失败: {str(e)[:80]}"

    m = re.search(r'"([^"]*)"', resp.text)
    if not m or not m.group(1).strip():
        mark_source("新浪LME", False, "返回内容为空")
        return None, False, "返回内容为空"

    parts = m.group(1).split(",")
    if len(parts) < 14:
        mark_source("新浪LME", False, f"字段数异常（{len(parts)}）")
        return None, False, f"字段数异常 {len(parts)}"

    price = to_float(parts[0])
    quote_date = parts[12].strip() if len(parts) > 12 else ""

    if not price or not (LME_MIN <= price <= LME_MAX):
        mark_source("新浪LME", False, f"价格越界: {price}（合理区间 {LME_MIN}~{LME_MAX} 美元/吨）")
        return None, False, f"价格越界: {price}"

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", quote_date):
        mark_source("新浪LME", False, f"日期字段异常: {quote_date!r}")
        return None, False, f"日期字段异常: {quote_date!r}"

    return {"date": quote_date, "price": round(price, 2)}, True, \
        f"{num(price)} 美元/吨 @ {quote_date}"


# ============ 合并与计算 ============

def build_futures_series(kline_rows, existing_rows, force=False):
    """把新浪 K 线合并进已有序列，并计算涨跌与换月标注。

    - 以 date 为键做 upsert：K 线（结算后）覆盖已有记录（含 --force 全量重算）
    - **change 按「结算价」算，不按收盘价** —— 这是本项目最容易被写歪的一处：
      主指标口径是结算价（第 6 章口径定义），页面、推送、年内位置全部引用 change，
      若这里用收盘价，就会出现「结算价涨了 130、却提示下跌」这种自相矛盾的输出
      （实测踩过：推送里出现「单日上涨 -130 元/吨」）。
    - rollover 标注：|changePct| > ROLLOVER_JUMP_PCT 时打标（连续合约换月拼接处的伪跳空）
    """
    merged = {}
    if not force:
        for row in existing_rows or []:
            d = row.get("date")
            if d:
                merged[d] = dict(row)

    added, updated, rejected = 0, 0, 0
    SRC_KEYS = ("open", "high", "low", "close", "settle", "volume", "openInterest")
    for row in kline_rows:
        d = row["date"]
        close = row.get("close")
        if close is None or not (PRICE_MIN <= close <= PRICE_MAX):
            rejected += 1
            continue
        if d in merged:
            # 只比源字段：已有记录里还挂着派生字段（change/changePct/rollover），
            # 整字典比较会永远不相等，于是每次都误报「更新了全部记录」
            if any(merged[d].get(k) != row.get(k) for k in SRC_KEYS):
                updated += 1
        else:
            added += 1
        merged[d] = row

    series = [merged[d] for d in sorted(merged)]

    # 重新计算全序列的涨跌（合并后前值可能变化，不能只算新增段）
    # 基准价 = 结算价，缺失时回退收盘价（2005 年前后部分记录没有结算价）
    def base_price(row):
        return row.get("settle") if row.get("settle") is not None else row.get("close")

    for i, row in enumerate(series):
        if i == 0:
            row["change"] = None
            row["changePct"] = None
            row.pop("rollover", None)
            continue
        prev = base_price(series[i - 1])
        cur = base_price(row)
        if prev and cur:
            chg = cur - prev
            pct = chg / prev * 100
            row["change"] = num(chg)
            row["changePct"] = round(pct, 2)
            if abs(pct) > ROLLOVER_JUMP_PCT:
                # 连续合约换月处会出现与真实行情无关的跳空；标出来，页面/推送会提示
                row["rollover"] = True
            else:
                row.pop("rollover", None)
        else:
            row["change"] = None
            row["changePct"] = None

    return series, added, updated, rejected


def build_spot_series(spot_rows, existing_spot, force=False):
    """合并现货序列（与期货同理，以日期为键 upsert）。"""
    merged = {}
    if not force:
        for row in existing_spot or []:
            d = row.get("date")
            if d:
                merged[d] = dict(row)
    added = 0
    for row in spot_rows:
        d = row["date"]
        p = row.get("price")
        if p is None or not (PRICE_MIN <= p <= PRICE_MAX):
            continue
        if d not in merged:
            added += 1
        merged[d] = row
    return [merged[d] for d in sorted(merged)], added


def build_lme_series(lme_rows, existing_lme, force=False):
    """合并 LME 序列（以日期为键 upsert；当天那条会被每次运行刷新为最新值）。"""
    merged = {}
    if not force:
        for row in existing_lme or []:
            d = row.get("date")
            if d:
                merged[d] = dict(row)
    added = 0
    for row in lme_rows or []:
        d = row["date"]
        if d not in merged:
            added += 1
        merged[d] = dict(row)
    return [merged[d] for d in sorted(merged)], added


def attach_derived(futures, spot, lme):
    """给现货补基差、给 LME 补沪伦比（都依赖期货结算价）。

    两个序列都是按日期升序的，所以用单指针推进（O(n+m)）而不是逐行二分/回扫 ——
    期货 5000+ 条 × LME 2500+ 条，写成 O(n×m) 会白等好几秒。
    """
    def make_lookup():
        ptr = -1

        def settle_on_or_before(date_str):
            nonlocal ptr
            while ptr + 1 < len(futures) and futures[ptr + 1]["date"] <= date_str:
                ptr += 1
            if ptr < 0:
                return None
            return futures[ptr]

        return settle_on_or_before

    lookup_spot = make_lookup()
    for s in spot:
        f = lookup_spot(s["date"])
        if f:
            base = f.get("settle") or f.get("close")
            s["basis"] = num(s["price"] - base) if base else None
            s["basisVsDate"] = f["date"]

    lookup_lme = make_lookup()
    for l in lme:
        f = lookup_lme(l["date"])
        if f:
            base = f.get("settle") or f.get("close")
            l["futuresBase"] = base
            l["ratio"] = round(base / l["price"], 2) if (base and l.get("price")) else None

    return futures, spot, lme


# ============ 校验 ============

def validate_all(futures, spot, lme):
    """全量合理性校验，返回 (problems, notes)。只报告与提示，不擅自删数据。"""
    problems, notes = [], []

    if not futures:
        problems.append("期货序列为空 —— 主指标不可用")
        return problems, notes

    # 1) 单日涨跌幅
    for r in futures[-30:]:
        pct = r.get("changePct")
        if pct is not None and abs(pct) > DAILY_CHANGE_LIMIT * 100:
            problems.append(
                f"{r['date']} 单日涨跌幅 {pct}% 超出上限 ±{DAILY_CHANGE_LIMIT*100:.0f}%")

    # 2) 期货最新价与现货的基差率
    last = futures[-1]
    base = last.get("settle") or last.get("close")
    for s in spot[-3:]:
        if s.get("basis") is not None and base:
            rate = abs(s["basis"]) / base
            if rate > SPOT_DEVIATION_LIMIT:
                problems.append(
                    f"{s['date']} 现货 {s['price']} 与期货结算 {base} 基差率 "
                    f"{rate*100:.1f}% 超出上限 ±{SPOT_DEVIATION_LIMIT*100:.0f}%")
    if spot and base:
        notes.append(f"最新基差 {spot[-1].get('basis')} 元/吨"
                     f"（现货{spot[-1]['price']} − 期货结算{base}）")

    # 3) 涨跌方向连续性（连续合约换月处的伪跳空已在 build 阶段标 rollover）
    rolls = [r["date"] for r in futures[-60:] if r.get("rollover")]
    if rolls:
        notes.append(f"近 60 个交易日内 {len(rolls)} 处疑似换月跳空/异常波动: "
                     f"{', '.join(rolls[:5])}{' …' if len(rolls) > 5 else ''}")

    # 4) 沪伦比区间（历史大致 6.5~8.5）
    ratios = [l["ratio"] for l in lme[-5:] if l.get("ratio")]
    for rt in ratios:
        if not (5.0 <= rt <= 10.0):
            problems.append(f"沪伦比 {rt} 超出合理区间 5.0~10.0")

    # 5) 现货序列过短提示
    if len(spot) < 3:
        notes.append(f"现货序列仅 {len(spot)} 条（生意社只提供最近若干交易日）")

    return problems, notes


# ============ 主流程 ============

def main(dry_run=False, force=False, skip_spot=False):
    t_start = time.time()
    print("=" * 66)
    print("🟠 沪铜价格数据更新")
    print("=" * 66)

    # ---- 加载现有数据 ----
    existing = None
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception as e:
            print(f"  ⚠️ 现有 data.json 解析失败，将重建: {e}")
    existing = existing or {}
    old_futures = existing.get("futures") or []
    old_spot = existing.get("spot") or []
    old_lme = existing.get("lme") or []

    print(f"📋 现有数据: 期货 {len(old_futures)} 条"
          f"（至 {old_futures[-1]['date'] if old_futures else '—'}）"
          f" / 现货 {len(old_spot)} 条 / LME {len(old_lme)} 条")

    # ---- L1: 新浪 K 线 ----
    print("\n🔍 [L1] 新浪日 K 线（沪铜连续 CU0）...")
    kline = fetch_sina_kline()
    print(f"  {'✅' if kline else '❌'} {SOURCE_HEALTH.get('新浪K线', {}).get('detail', '')}")

    # ---- L2: 上期所官方（校验 + 兜底）----
    print("\n🔍 [L2] 上海期货交易所官方日行情 ...")
    l2_used_as_source = False
    if kline:
        verify_date = kline[-1]["date"]
        ok, detail = shfe_verify(verify_date, kline[-1]["close"])
        if ok is True:
            mark_source("上期所", True, detail)
            print(f"  ✅ {detail}")
        elif ok is False:
            mark_source("上期所", False, f"交叉校验不一致：{detail}")
            print(f"  ⚠️ 交叉校验不一致：{detail}")
        else:
            mark_source("上期所", True, f"可用但未参与本次校验（{detail}）")
            print(f"  ℹ️ 未参与校验：{detail}")
    else:
        # K 线挂了 → 用上期所兜底出一条最新记录，保证主指标不断供
        print("  ⚠️ 新浪 K 线不可用，改用上期所官方数据兜底")
        fallback = shfe_nearest_settled(days_back=10)
        if fallback:
            kline = [{
                "date": fallback["date"],
                "open": fallback["open"], "high": fallback["high"], "low": fallback["low"],
                "close": fallback["close"], "settle": fallback["settle"],
                "volume": fallback["volume"], "openInterest": fallback["openInterest"],
                "fallbackSource": "上期所",
            }]
            l2_used_as_source = True
            mark_source("上期所", True,
                        f"作为兜底数据源提供 {fallback['date']} 记录"
                        f"（主力 {fallback['contract']} 结算 {fallback['settle']}）")
            print(f"  ✅ 兜底取到 {fallback['date']}：结算 {fallback['settle']}")
        else:
            mark_source("上期所", False, "近 10 日无任何已结算的官方文件")
            print("  ❌ 近 10 日无任何已结算的官方文件")

    # ---- L3: 新浪实时（仅快照）----
    print("\n🔍 [L3] 新浪实时行情（盘中快照，不入历史序列）...")
    realtime, rt_ok, rt_detail = fetch_sina_realtime()
    print(f"  {'✅' if rt_ok else '❌'} {rt_detail}")

    # ---- L4: 生意社现货 ----
    spot_rows = []
    if skip_spot:
        mark_source("生意社现货", True, "本次运行通过 --no-spot 主动跳过")
        print("\n🔍 [L4] 生意社现货（已跳过）")
    else:
        print("\n🔍 [L4] 生意社现货 · 1# 电解铜（两步 cookie 握手）...")
        spot_rows = fetch_spot_100ppi()
        print(f"  {'✅' if spot_rows else '❌'} "
              f"{SOURCE_HEALTH.get('生意社现货', {}).get('detail', '')}")

    # ---- L5: LME 历史日 K ----
    print("\n🔍 [L5] 新浪 LME 铜日 K 线（伦铜，2016 至今）...")
    lme_rows = fetch_lme_kline()

    # ---- L6: LME 实时（兜底 + 交叉校验）----
    print("\n🔍 [L6] 新浪 LME 实时（hf_CAD）...")
    lme_rt, lme_rt_ok, lme_rt_detail = fetch_lme()
    print(f"  {'✅' if lme_rt_ok else '❌'} {lme_rt_detail}")

    if lme_rows:
        # 实时值与日 K 最后一条做交叉校验：两者不该差太多（同一时刻同一品种）
        last_k = lme_rows[-1]
        if lme_rt_ok and lme_rt and lme_rt["date"] == last_k["date"]:
            drift = abs(lme_rt["price"] - last_k["price"]) / last_k["price"]
            if drift > 0.02:
                print(f"  ⚠️ 日K {last_k['price']} 与实时 {lme_rt['price']} 偏差 "
                      f"{drift*100:.1f}%（可能一个是盘中价、一个是收盘价，仅提示）")
        mark_source("新浪LME", True,
                    f"日K {len(lme_rows)} 条，{lme_rows[0]['date']} → {lme_rows[-1]['date']}"
                    f"（最新 {lme_rows[-1]['price']} 美元/吨）"
                    + (f"；实时 {lme_rt['price']} 美元/吨" if lme_rt_ok else "；实时不可用"))
        print(f"  ✅ {SOURCE_HEALTH['新浪LME']['detail']}")
    elif lme_rt_ok and lme_rt:
        # 日 K 挂了 → 用实时兜底出一条当日记录，至少不缺席
        lme_rows = [{"date": lme_rt["date"], "price": lme_rt["price"], "intraday": True}]
        mark_source("新浪LME", True, f"日K不可用，改用实时兜底 {lme_rt['price']} 美元/吨 @ {lme_rt['date']}")
        print(f"  ⚠️ 日K不可用，已用实时值兜底")
    else:
        mark_source("新浪LME", False, "日K 与实时均不可用")
        print("  ❌ 日K 与实时均不可用")

    # ---- 健康汇总 ----
    print("\n🩺 数据源健康:")
    for name, st in SOURCE_HEALTH.items():
        print(f"  {'✅' if st['ok'] else '❌'} {name}: {st['detail']}")
    healthy = [n for n, st in SOURCE_HEALTH.items() if st["ok"]]
    main_sources = ["新浪K线", "上期所"]
    main_ok = [n for n in main_sources if SOURCE_HEALTH.get(n, {}).get("ok")]

    # ---- 合并 ----
    print("\n🔢 合并与计算...")
    futures, f_add, f_upd, f_rej = build_futures_series(kline, old_futures, force)
    spot, s_add = build_spot_series(spot_rows, old_spot, force)
    lme, l_add = build_lme_series(lme_rows, old_lme, force)
    futures, spot, lme = attach_derived(futures, spot, lme)

    print(f"  期货: 新增 {f_add} / 更新 {f_upd} / 剔除 {f_rej} → 共 {len(futures)} 条")
    print(f"  现货: 新增 {s_add} → 共 {len(spot)} 条")
    print(f"  LME : 新增 {l_add} → 共 {len(lme)} 条")

    if futures:
        last_f = futures[-1]
        print(f"  最新期货: {last_f['date']} 收 {last_f['close']} / 结算 {last_f['settle']}"
              f" / 涨跌 {last_f.get('change')} ({last_f.get('changePct')}%)"
              f"{'  ⚠️ 疑似换月跳空' if last_f.get('rollover') else ''}")
    if spot:
        print(f"  最新现货: {spot[-1]['date']} 均价 {spot[-1]['price']} 元/吨"
              f" / 基差 {spot[-1].get('basis')}")
    if lme:
        last_l = lme[-1]
        print(f"  最新LME : {last_l['date']} {last_l['price']} 美元/吨"
              f" / 沪伦比 {last_l.get('ratio')}"
              f"{'（盘中，未收盘）' if last_l.get('intraday') else ''}")

    # ---- 校验 ----
    print("\n✅ 合理性校验...")
    problems, notes = validate_all(futures, spot, lme)
    for n in notes:
        print(f"  ℹ️ {n}")
    for p in problems:
        print(f"  ⚠️ {p}")
    if not problems:
        print("  全部通过")

    # ---- 非交易日跳过（D-7）----
    # 判据：期货序列没有产生「更晚的日期」→ 今天没有新的交易日数据。
    # 这天然覆盖周末与节假日，不需要维护交易日历。
    newest_date = futures[-1]["date"] if futures else None
    old_newest = old_futures[-1]["date"] if old_futures else None
    has_new_trading_day = bool(newest_date and newest_date != old_newest)

    if dry_run:
        print("\n🔍 [Dry Run] 不写入文件。")
        print(f"  期货最新交易日: {newest_date}（库内旧值 {old_newest or '—'}）"
              f" → {'有新数据' if has_new_trading_day else '无新交易日'}")
        return

    if not main_ok:
        # 主指标两个源全灭 —— 绝不能写成「已是最新」然后一片祥和地绿着
        print("\n❌ 无法确认数据是否最新 —— 沪铜主指标的可用数据源（新浪K线 / 上期所）全部失效！")
        save_health()
        sys.exit(1)

    # ---- 写盘 ----
    prev_meta = existing.get("meta") or {}
    prev_health = existing.get("sourceHealth") or {}

    # sourceHealth 累积失败计数，让页面/推送能看出「这个源连续失败几天了」
    health_out = {}
    for name in ["新浪K线", "上期所", "新浪实时", "生意社现货", "新浪LME"]:
        st = SOURCE_HEALTH.get(name)
        if not st:
            continue
        fails = 0 if st["ok"] else (prev_health.get(name, {}).get("consecutiveFailures", 0) + 1)
        health_out[name] = {"ok": st["ok"], "detail": st["detail"], "consecutiveFailures": fails}

    meta = {
        "lastUpdated": newest_date,
        "lastRunAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "primary": "futures",
        "primarySymbol": "nf_CU0",
        "primaryName": "沪铜连续",
        "spotSince": spot[0]["date"] if spot else None,
        "lmeSince": lme[0]["date"] if lme else None,
        "dataSource": "上海期货交易所 · 新浪财经 · 生意社",
        "unit": "元/吨",
        "lmeUnit": "美元/吨",
        "changeBasis": "settle",   # 涨跌按结算价算（与主指标口径一致），不要改成 close
        "basisDef": "现货均价 − 期货结算价（正=现货升水）",
        "ratioDef": "沪铜结算价 ÷ LME(美元/吨)，未扣汇率（名义沪伦比）",
        "historySince": futures[0]["date"] if futures else None,
        "totalTradingDays": len(futures),
        "spotDays": len(spot),
        "lmeDays": len(lme),
        "hasNewTradingDay": has_new_trading_day,
        "notes": notes,
        "warnings": problems,
    }
    if realtime:
        meta["realtime"] = {
            "price": realtime["price"],
            "date": realtime["date"],
            "time": realtime["time"],
            "high": realtime.get("high"),
            "low": realtime.get("low"),
            "prevSettle": realtime.get("prevSettle"),
            "openInterest": realtime.get("openInterest"),
        }
    else:
        # L3 挂了不该让整个任务变红，但必须在 meta 里留痕，页面才能诚实地不显示它
        meta["realtime"] = None

    data = {
        "meta": meta,
        "futures": futures,
        "spot": spot,
        "lme": lme,
        "sourceHealth": health_out,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        # 紧凑输出：5000+ 条历史下，indent=2 会让文件体积翻倍，而这是每次页面加载都要拉的文件
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = DATA_FILE.stat().st_size / 1024
    print(f"\n💾 已保存: {DATA_FILE}  ({size_kb:.0f} KB)")
    save_health()

    elapsed = time.time() - t_start
    if has_new_trading_day:
        print(f"🎉 更新完成! 新交易日 {newest_date}，期货共 {len(futures)} 条，用时 {elapsed:.1f}s")
    else:
        print(f"✅ 无新交易日（最新仍为 {newest_date}），数据已是最新，用时 {elapsed:.1f}s")


if __name__ == "__main__":
    argv = set(sys.argv[1:])
    main(
        dry_run="--dry-run" in argv,
        force="--force" in argv,
        skip_spot="--no-spot" in argv,
    )
