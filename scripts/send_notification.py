#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日铜价通知 —— 微信（Server酱）+ 钉钉 双通道

用法:
  python send_notification.py [changed]

  changed 由 workflow 传入（"true"/"false"），表示 data.json 是否在本次运行中被改写。
  但**是否推送以 data.json 里的 meta.hasNewTradingDay 为准** —— workflow 的 changed
  只在「有新交易日」时才会是 true，节假日两者都为 false，逻辑一致；用 meta 是因为
  手动跑脚本时没有 workflow 变量，而脚本必须能独立判断。

环境变量:
  SERVERCHAN_SENDKEY  - Server酱 SendKey
  DINGTALK_WEBHOOK    - 钉钉机器人 Webhook
  DINGTALK_SECRET     - 钉钉机器人加签密钥（可选）
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime
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
    print("❌ 需要安装 requests: pip install requests")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_FILE = PROJECT_ROOT / "data.json"
HEALTH_FILE = PROJECT_ROOT / ".source_health.json"
PAGE_URL = "https://ToniaXuu.github.io/copper-price-tracker/"

# 大涨/大跌阈值：单日 1% 在铜上已经是很明显的行情，值得单独做标题
BIG_MOVE_PCT = 1.0

# 用铜成本换算基准（与页面「典型用铜成本速查」保持一致）
COST_REFS = [
    ("铜芯电缆 YJV 4×50", 2002, "公里"),
    ("配电变压器 S13-400kVA", 300, "台"),
]


# ============ 数据读取 ============

def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def read_health_alerts():
    """读取 update_copper.py 留下的数据源健康报告，有源不可用时返回告警文案。

    存在的意义：源失效时爬虫只会输出「没有新记录」，通知里照样风平浪静地写
    「无变化」—— 数据静默停更很久都不会有人发现。这里把故障主动顶进通知。
    """
    try:
        with open(HEALTH_FILE, "r", encoding="utf-8") as f:
            health = json.load(f)
    except Exception:
        return "", 0, 0

    if not health:
        return "", 0, 0

    bad = {n: v for n, v in health.items() if not v.get("ok")}
    if not bad:
        return "", 0, len(health)

    lines = "\n".join(
        f"- ❌ **{n}**：{v.get('detail', '不可用')}" for n, v in bad.items()
    )
    if len(bad) == len(health):
        head = "🚨 **全部数据源不可用 —— 数据可能已停止更新**"
    else:
        head = "⚠️ **数据源异常（其余源仍正常，数据未受影响）**"
    return f"\n\n---\n\n{head}\n\n{lines}", len(bad), len(health)


# ============ 发送通道 ============

def send_serverchan(sendkey, title, content):
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        r = requests.post(url, data={"title": title, "desp": content}, timeout=15).json()
        if r.get("code") == 0:
            print("  ✅ 微信通知发送成功")
        else:
            print(f"  ⚠️ 微信通知失败: {r.get('message', '')}")
    except Exception as e:
        print(f"  ⚠️ 微信通知异常: {str(e)[:120]}")


def send_dingtalk(webhook, title, text):
    secret = os.environ.get("DINGTALK_SECRET", "")
    url = webhook
    if secret:
        ts = str(round(time.time() * 1000))
        h = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
        url = f"{webhook}&timestamp={ts}&sign={urllib.parse.quote_plus(base64.b64encode(h))}"
    try:
        r = requests.post(
            url,
            json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
            timeout=15,
        ).json()
        if r.get("errcode") == 0:
            print("  ✅ 钉钉通知发送成功")
        else:
            print(f"  ⚠️ 钉钉通知失败: {r.get('errmsg', '')}")
    except Exception as e:
        print(f"  ⚠️ 钉钉通知异常: {str(e)[:120]}")


# ============ 文案工具 ============

def fmt_date(iso):
    p = str(iso).split("-")
    if len(p) != 3:
        return iso
    return f"{int(p[1])}月{int(p[2])}日"


def weekday_cn(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except Exception:
        return ""
    return "周" + "一二三四五六日"[d.weekday()]


def today_cn():
    now = datetime.now()
    return f"{now.month}月{now.day}日 周{'一二三四五六日'[now.weekday()]}"


def sgn(v, unit=""):
    if v is None:
        return "—"
    return f"{'+' if v > 0 else ''}{v:,}{unit}"


def dir_of(v):
    if v is None or v == 0:
        return "flat"
    return "up" if v > 0 else "down"


def streaks(futures):
    """统计末尾的连续涨/跌。返回 (方向, 天数, 累计变动)。"""
    if len(futures) < 2:
        return 0, 0, None
    last = futures[-1]
    sign = 0
    if last.get("change"):
        sign = 1 if last["change"] > 0 else -1 if last["change"] < 0 else 0
    if sign == 0:
        return 0, 1, 0
    cnt = 1
    for i in range(len(futures) - 2, 0, -1):
        c = futures[i].get("change")
        if c is None:
            break
        s = 1 if c > 0 else -1 if c < 0 else 0
        if s == sign:
            cnt += 1
        else:
            break
    base_idx = len(futures) - 1 - cnt
    base = futures[base_idx].get("settle") or futures[base_idx].get("close")
    cur = last.get("settle") or last.get("close")
    return sign, cnt, (cur - base) if base else None


def year_position(futures):
    """当前价在本年区间里的百分位。返回 (分位, 年内最低, 年内最高, 当前值) 或 None。"""
    if not futures:
        return None
    year = str(futures[-1]["date"])[:4]
    ys = [r for r in futures if str(r["date"])[:4] == year]
    if not ys:
        return None
    vals = [(r.get("settle") or r.get("close")) for r in ys]
    vals = [v for v in vals if v]
    if not vals:
        return None
    lo, hi = min(vals), max(vals)
    cur = futures[-1].get("settle") or futures[-1].get("close")
    pos = (cur - lo) / (hi - lo) * 100 if hi > lo else 50
    return pos, lo, hi, cur


# ============ 消息构建 ============

def build_msg(data, has_change):
    meta = data.get("meta") or {}
    futures = data.get("futures") or []
    spot = data.get("spot") or []
    lme = data.get("lme") or []

    if not futures:
        return None, None

    last = futures[-1]
    settle = last.get("settle") or last.get("close")
    chg = last.get("change")
    pct = last.get("changePct")
    d = dir_of(chg)

    last_spot = spot[-1] if spot else None
    last_lme = lme[-1] if lme else None

    # ---- 标题（N-3：大涨/大跌单独做标题）----
    move_word = ""
    if chg is not None and pct is not None and abs(pct) >= BIG_MOVE_PCT:
        move_word = "大涨" if chg > 0 else "大跌"
    if move_word:
        title = f"{'📈' if chg > 0 else '📉'} 铜价{move_word} | {'🔺' if chg > 0 else '🔻'}{sgn(chg)} 元/吨"
    elif d == "up":
        title = f"📈 今日铜价 | 🔺{sgn(chg)} 元/吨"
    elif d == "down":
        title = f"📉 今日铜价 | 🔻{sgn(chg)} 元/吨"
    else:
        title = f"📊 今日铜价 | 持平"

    # ---- 头部 ----
    header = (f"🕐 {today_cn()}\n"
              f"📅 数据日期 {fmt_date(last['date'])}（{weekday_cn(last['date'])}）\n"
              f"🏦 上海期货交易所 · 沪铜连续")

    # ---- 三指标表格（钉钉 Markdown 表格）----
    rows = ["| 指标 | 价格 | 变动 |", "|------|------|------|"]
    # settle / chg 都可能为 None（历史首条没有 change），所有格式化都必须过一遍 None
    settle_str = f"{settle:,} 元/吨" if settle else "—"
    if chg is None:
        chg_str = "—"
    elif chg > 0:
        chg_str = f"🔺{sgn(chg)} 元/吨"
    elif chg < 0:
        chg_str = f"🔻{sgn(chg)} 元/吨"
    else:
        chg_str = "持平"
    rows.append(f"| **沪铜连续** | {settle_str} | {chg_str} |")

    if last_spot:
        prev_spot = spot[-2] if len(spot) >= 2 else None
        sdiff = (last_spot["price"] - prev_spot["price"]) if prev_spot else None
        rows.append(f"| 1# 电解铜现货 | {last_spot['price']:,} 元/吨 | "
                    f"{sgn(sdiff) + ' 元/吨' if sdiff is not None else '—'} |")
    if last_lme:
        prev_lme = lme[-2] if len(lme) >= 2 else None
        ldiff = round(last_lme["price"] - prev_lme["price"], 1) if prev_lme else None
        rows.append(f"| LME 铜（3 月） | {last_lme['price']:,.0f} 美元/吨 | "
                    f"{sgn(ldiff) + ' 美元/吨' if ldiff is not None else '—'} |")
    table = "\n".join(rows)

    # ---- 实用信息（N-5）----
    info = []

    # 单位换算 —— 工程报价最常用的一步换算
    if settle:
        info.append(f"💰 **换算**：{settle/1000:.2f} 元/公斤 · {settle/2000:.2f} 元/斤")

    # 年内位置
    pos_info = year_position(futures)
    if pos_info:
        pos, lo, hi, cur = pos_info
        if pos >= 75:
            verdict = "偏高"
        elif pos >= 45:
            verdict = "中位"
        elif pos >= 20:
            verdict = "偏低"
        else:
            verdict = "低位"
        info.append(f"📊 **年内位置**：{lo:,} ~ {hi:,} 区间的 {pos:.0f}% 分位（{verdict}）")

    # 连续涨跌
    sign, cnt, sum_v = streaks(futures)
    if sign != 0 and cnt >= 2:
        info.append(f"🔍 **趋势**：连续 {cnt} 日{'上涨' if sign > 0 else '下跌'}，"
                    f"累计 {sgn(round(sum_v))} 元/吨")
    elif sign != 0:
        info.append(f"🔍 **趋势**：单日{'上涨' if sign > 0 else '下跌'} "
                    f"{sgn(round(sum_v)) if sum_v is not None else '—'} 元/吨")

    # 沪伦比
    if last_lme and last_lme.get("ratio") is not None:
        info.append(f"⚖️ **沪伦比**：{last_lme['ratio']}（名义值，未扣汇率）")

    # 基差 / 现货松紧（唯一的「采购建议」来源，不臆测方向）
    if last_spot and last_spot.get("basis") is not None:
        b = last_spot["basis"]
        if b > 0:
            note = f"现货升水 {b:,} 元/吨，现货相对偏紧"
        elif b < 0:
            note = f"现货贴水 {abs(b):,} 元/吨，现货相对宽松"
        else:
            note = "现货与期货基本平水"
        info.append(f"💡 **基差**：{note}")

    # 用铜成本敏感度（贴近实际业务的一条）
    if settle:
        cost_bits = []
        for name, kg, unit in COST_REFS:
            per1000 = kg * 1000 / 1000
            cost_bits.append(f"{name} 约 {per1000:,.0f} 元/{unit}")
        info.append("🏭 **每涨跌 1,000 元/吨**：" + "；".join(cost_bits))

    # 盘中参考
    rt = meta.get("realtime")
    if rt and rt.get("price"):
        t = str(rt.get("time") or "")
        hhmm = f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else ""
        info.append(f"⏱ **盘中参考**：{rt['price']:,} 元/吨（{fmt_date(rt['date'])} {hhmm} 快照）")

    body = (
        f"## {title}\n\n"
        f"{header}\n\n"
        f"---\n\n"
        f"### 三大指标\n\n{table}\n\n"
        f"---\n\n"
        f"### 实用信息\n\n" + "\n\n".join(info) + "\n\n"
        f"---\n\n"
        f"[📊 查看完整走势与换算工具]({PAGE_URL})"
    )
    return title, body


# ============ 主流程 ============

def main():
    argv_changed = len(sys.argv) > 1 and sys.argv[1] == "true"

    if not DATA_FILE.exists():
        print("❌ data.json 不存在，无法推送")
        sys.exit(1)

    data = load_data()
    alerts, bad_cnt, total_cnt = read_health_alerts()

    meta = data.get("meta") or {}
    has_new = bool(meta.get("hasNewTradingDay"))
    if argv_changed:
        has_new = True    # workflow 明确说 data.json 变了，以它为准

    print(f"📋 新交易日: {has_new}　数据源: {total_cnt - bad_cnt}/{total_cnt} 正常")

    # ---- N-6 节假日不推送 ----
    # 判据是「有没有新的交易日数据」，天然覆盖周末与节假日，不需要维护交易日历。
    # 但数据源有异常时必须照发 —— 否则「源挂了」和「今天休市」在下游看起来一模一样。
    if not has_new and not alerts:
        print("⏭️ 无新交易日数据且数据源全部正常（周末/节假日），本次不推送")
        return

    if alerts:
        print(f"⚠️ 检测到 {bad_cnt} 个数据源异常，已附加告警")

    title, body = build_msg(data, has_new)
    if not body:
        print("❌ 无法构建消息（futures 序列为空）")
        sys.exit(1)

    if not has_new:
        # 休市 + 有告警：标题必须直接点明是故障而不是行情
        title = "⚠️ 铜价数据源异常 | 请检查"
        body = body.replace("## 📊 今日铜价 | 持平", "## ⚠️ 铜价数据源异常", 1)
        print("⚠️ 本次为告警推送（无新数据）")

    if alerts:
        body += alerts

    sent = 0
    if os.environ.get("SERVERCHAN_SENDKEY"):
        print(f"📤 [微信] {title}")
        send_serverchan(os.environ["SERVERCHAN_SENDKEY"], title, body)
        sent += 1

    if os.environ.get("DINGTALK_WEBHOOK"):
        print(f"📤 [钉钉] {title}")
        send_dingtalk(os.environ["DINGTALK_WEBHOOK"], title, body)
        sent += 1

    if sent == 0:
        print("⚠️ 未配置任何推送通道（SERVERCHAN_SENDKEY / DINGTALK_WEBHOOK 均为空）")
        # 不 exit(1)：本地/未配置 secrets 时不该把 workflow 判红。
        # 真正的数据故障由 update_copper.py 的 exit(1) 负责报红。


if __name__ == "__main__":
    main()
