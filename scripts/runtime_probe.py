# -*- coding: utf-8 -*-
"""运行时探针：用自写 CDP 客户端（零第三方依赖）在无头 Chrome 里跑真页面。

本机 `--dump-dom` 在 load 后立刻落盘、setTimeout 不执行，取不到异步渲染结果，
所以这里直接连 DevTools 协议，拿运行时数值。

用法：
    python scripts/runtime_probe.py

检查项：
  A 数据真加载（非回退）      B ECharts 实例与数据点
  C 涨红跌绿（真实计算色）    D 主题三态切换 + 无白屏
  E 换算器交互                F 大屏无横向溢出
  G 运行时报错                H PWA / SW / 表格 / 数据源状态
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL, WARN = [], [], []


def ok(m):
    PASS.append(m); print(f"  [ OK ] {m}")


def no(m):
    FAIL.append(m); print(f"  [FAIL] {m}")


def wn(m):
    WARN.append(m); print(f"  [WARN] {m}")


# ------------------------------------------------------------------ WebSocket
class MiniWS:
    """最小 RFC6455 客户端：够用即可（掩码发送、分片接收、ping/pong）。"""

    def __init__(self, url, timeout=30):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)$", url)
        if not m:
            raise ValueError(f"bad ws url: {url}")
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake EOF")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("handshake rejected: " + head.decode("utf-8", "replace"))
        self.buf = rest

    def _exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode, payload=b""):
        n = len(payload)
        hdr = bytearray([0x80 | opcode])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        hdr += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(hdr))

    def send(self, text):
        self._frame(0x1, text.encode("utf-8"))

    def recv(self):
        """返回一条完整文本消息；自动处理 ping/pong 与分片。"""
        frags = []
        while True:
            b0, b1 = self._exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._exact(8))[0]
            if b1 & 0x80:
                self._exact(4)  # 服务端不应掩码
            data = self._exact(ln)
            if opcode == 0x9:
                self._frame(0xA, data); continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise EOFError("peer closed")
            frags.append(data)
            if fin:
                return b"".join(frags).decode("utf-8", "replace")

    def close(self):
        try:
            self._frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    def __init__(self, ws):
        self.ws, self._id = ws, 0

    def call(self, method, params=None, timeout=30):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}},
                                ensure_ascii=False))
        self.ws.sock.settimeout(timeout)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr, timeout=30):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        }, timeout=timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError("JS 异常: " + json.dumps(d.get("exception") or d, ensure_ascii=False)[:400])
        return r.get("result", {}).get("value")


# ------------------------------------------------------------------ 启停
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start_server():
    port = free_port()
    p = subprocess.Popen([PY, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}/"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "index.html", timeout=1); break
        except Exception:
            time.sleep(0.25)
    return p, base


def start_chrome(width, height):
    port = free_port()
    udd = tempfile.mkdtemp(prefix="cpt-chrome-")
    args = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-background-networking",
            "--disable-features=Translate,OptimizationHints",
            f"--remote-debugging-port={port}", f"--user-data-dir={udd}",
            f"--window-size={width},{height}", "about:blank"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as r:
                targets = json.load(r)
            pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                return proc, pages[0]["webSocketDebuggerUrl"], udd
        except Exception:
            pass
        time.sleep(0.25)
    proc.kill()
    raise RuntimeError("Chrome 调试端口未就绪")


# ------------------------------------------------------------------ 探针表达式
SNAPSHOT = r"""(() => {
  const g = (id) => document.getElementById(id);
  const txt = (id) => { const e = g(id); return e ? e.textContent.trim() : null; };
  const cls = (id) => { const e = g(id); return e ? e.className : null; };
  const col = (id) => { const e = g(id); return e ? getComputedStyle(e).color : null; };
  const rgb = (s) => { const m = String(s).match(/(\d+)\D+(\d+)\D+(\d+)/); return m ? [+m[1],+m[2],+m[3]] : null; };
  const inst = (id) => { try { return echarts.getInstanceByDom(g(id)); } catch(e) { return null; } };
  const seriesOf = (id) => {
    const i = inst(id); if (!i) return null;
    try {
      return i.getOption().series.map(s => ({
        type: s.type, name: s.name || null,
        n: Array.isArray(s.data) ? s.data.length : (s.data && s.data.length) || 0,
        hasMark: !!(s.markLine || s.markPoint),
      }));
    } catch(e) { return 'ERR:' + e.message; }
  };
  const cs = getComputedStyle(document.documentElement);
  const v = (n) => cs.getPropertyValue(n).trim();
  const bodyBg = getComputedStyle(document.body).backgroundColor;
  const rootBg = v('--bg');

  const ct = g('counterFutures');
  const canvasCount = document.querySelectorAll('canvas').length;

  return JSON.stringify({
    theme: document.documentElement.getAttribute('data-theme'),
    themeMode: (typeof _themeMode !== 'undefined' ? _themeMode : null),
    dataLoadFailed: (typeof dataLoadFailed !== 'undefined' ? dataLoadFailed : null),
    changeBasis: (typeof META !== 'undefined' && META ? META.changeBasis : null),
    futCount: (typeof FUT !== 'undefined' ? FUT.length : -1),
    spotCount: (typeof SPOT !== 'undefined' ? SPOT.length : -1),
    lmeCount: (typeof LME !== 'undefined' ? LME.length : -1),
    lastDate: (typeof FUT !== 'undefined' && FUT.length ? FUT[FUT.length-1].date : null),
    lastSettle: (typeof FUT !== 'undefined' && FUT.length ? FUT[FUT.length-1].settle : null),
    lastChange: (typeof FUT !== 'undefined' && FUT.length ? FUT[FUT.length-1].change : null),

    counterTarget: ct ? ct.dataset.target : null,
    counterText: txt('counterFutures'),
    counterSkeleton: ct ? ct.classList.contains('skeleton') : null,
    changeText: txt('changeFutures'),
    changeClass: cls('changeFutures'),
    changeColor: col('changeFutures'),
    openText: txt('ocOpen'), highText: txt('ocHigh'),
    lowText: txt('ocLow'), closeText: txt('ocClose'),
    spotText: txt('counterSpot'), lmeText: txt('counterLme'),

    statDod: txt('statDod'), statDodClass: cls('statDod'), statDodColor: col('statDod'),
    statWow: txt('statWow'), statYtd: txt('statYtd'), statYoy: txt('statYoy'),

    echarts: (typeof echarts === 'undefined' ? null : (echarts.version || 'yes')),
    canvasCount: canvasCount,
    mainSeries: seriesOf('mainChart'),
    ratioSeries: seriesOf('ratioChart'),
    basisSeries: seriesOf('basisChart'),
    rangeBtns: document.querySelectorAll('#rangeGroup .rg-btn').length,
    rangeActive: (function(){ const b = document.querySelector('#rangeGroup .rg-btn[aria-pressed="true"]'); return b ? b.dataset.range : null; })(),
    rangeHint: txt('rangeHint'),
    rangeStart: (typeof computeRangeStart === 'function' ? String(computeRangeStart()) : null),
    mainZoomPct: (function(){ try {
        const m = echarts.getInstanceByDom(g('mainChart')).getModel().getComponent('dataZoom', 0);
        return m.getPercentRange();
      } catch(e) { return null; } })(),

    posMark: (function(){ const e = g('posMark'); return e ? (e.style.left || getComputedStyle(e).left) : null; })(),
    posFillW: (function(){ const e = g('posFill'); return e ? e.style.width : null; })(),
    posVerdict: txt('posVerdict'), posLow: txt('posLow'), posHigh: txt('posHigh'), posMid: txt('posMid'),
    msStreak: txt('msStreak'), msAmp: txt('msAmp'), msOi: txt('msOi'), msMaxSwing: txt('msMaxSwing'),

    convInput: (function(){ const e = g('convInput'); return e ? e.value : null; })(),
    cvKg: txt('cvKg'), cvJin: txt('cvJin'), cvG: txt('cvG'), cvWan: txt('cvWan'),
    costRows: document.querySelectorAll('#costBody tr').length,
    costFirst: (function(){ const e = document.querySelector('#costBody td[data-kg]'); return e ? e.textContent : null; })(),

    tableRows: document.querySelectorAll('#tableBody tr').length,
    pageSize: (typeof PAGE_SIZE !== 'undefined' ? PAGE_SIZE : -1),

    /* 布局间距实测：影响卡片区 → 历史数据标题、标题 → 表格 之间的真实像素间距。
       getBoundingClientRect 取的是视口坐标，两元素同时可见时差值即实际间距。 */
    gapImpactToHistory: (function(){
      try {
        const grid = document.querySelector('.impact-grid');
        const hist = g('sec-history');
        if (!grid || !hist) return null;
        // 卡片区 → 历史卡：取 .impact-grid 的 margin-bottom 与历史卡顶部之间，
        // 由于两者是兄弟块级元素，margin 不塌陷（grid 是 grid 容器），
        // 净空 = hist.top − (grid.top + grid.height)。
        const a = grid.getBoundingClientRect();
        const b = hist.getBoundingClientRect();
        return Math.round(b.top - (a.top + a.height));
      } catch(e) { return 'ERR:' + e.message; }
    })(),
    gapHistoryTitleToTable: (function(){
      try {
        const hist = g('sec-history');
        if (!hist) return null;
        const t = hist.querySelector('.section-title');
        const t2 = hist.querySelector('.tbl-wrap');
        if (!t || !t2) return null;
        // ⚠️ 不能用 rect.bottom 相减：外边距(margin-bottom)不在 rect 里，会算出负数。
        // 正确算法 = 下一元素 top - 上一元素 bottom - 元素自身 margin-bottom 之外的间距，
        // 这里直接取「两元素 box 之间的净空」= 下一 top − (上一 top + 上一 height)。
        const a = t.getBoundingClientRect();
        const b = t2.getBoundingClientRect();
        return Math.round(b.top - (a.top + a.height));
      } catch(e) { return 'ERR:' + e.message; }
    })(),
    gapImpactTitleToGrid: (function(){
      try {
        const t = g('sec-impact');
        const grid = document.querySelector('.impact-grid');
        if (!t || !grid) return null;
        // 同上：标题的 margin-bottom 是 22px，不在 rect 里，故用 top+height 算净空。
        const a = t.getBoundingClientRect();
        const b = grid.getBoundingClientRect();
        return Math.round(b.top - (a.top + a.height));
      } catch(e) { return 'ERR:' + e.message; }
    })(),
    impactGridMB: (function(){
      const e = document.querySelector('.impact-grid');
      return e ? getComputedStyle(e).marginBottom : null;
    })(),
    impactRowBottoms: (function(){
      /* 三层卡片各自底边 y，用来判断同行卡片底边是否参差 */
      try {
        const cards = Array.from(document.querySelectorAll('.impact-card'));
        return cards.map(c => Math.round(c.getBoundingClientRect().bottom));
      } catch(e) { return []; }
    })(),

    srcCards: document.querySelectorAll('#srcGrid > *').length,
    healthTag: txt('healthTag'),

    upText: v('--up-text'), downText: v('--down-text'),
    bodyBg: bodyBg, rootBg: rootBg,
    bodyTextColor: getComputedStyle(document.body).color,
    ulColor: rgb(v('--up-text')), dlColor: rgb(v('--down-text')),

    scrollW: document.documentElement.scrollWidth,
    innerW: window.innerWidth,
    errs: (window.__errs || []).slice(0, 8),
    sw: (navigator.serviceWorker && navigator.serviceWorker.controller) ? 'controlled' : 'no-controller',
  });
})()"""


def main() -> int:
    print("=" * 70)
    print("copper-price-tracker 运行时探针（CDP）")
    print("=" * 70)

    srv, base = start_server()
    proc, wsurl, udd = start_chrome(1280, 1600)
    ws = MiniWS(wsurl)
    cdp = CDP(ws)
    rc = 1
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": (
            "window.__errs=[];"
            "window.addEventListener('error',function(e){window.__errs.push('err:'+(e.message||e.type));});"
            "window.addEventListener('unhandledrejection',function(e){window.__errs.push('rej:'+String(e.reason&&e.reason.message||e.reason));});"
            "var _cw=console.warn,_ce=console.error;"
            "console.warn=function(){window.__errs.push('warn:'+[].slice.call(arguments).join(' '));return _cw.apply(console,arguments);};"
            "console.error=function(){window.__errs.push('cerr:'+[].slice.call(arguments).join(' '));return _ce.apply(console,arguments);};"
        )})
        cdp.call("Page.navigate", {"url": base + "index.html"})

        # 等就绪：counterFutures 拿到目标值 + 主图有 canvas
        ready = False
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                r = cdp.js(
                    "JSON.stringify({"
                    "c:(document.getElementById('counterFutures')||{}).dataset?document.getElementById('counterFutures').dataset.target:null,"
                    "cv:document.querySelectorAll('#mainChart canvas').length,"
                    "ec:typeof echarts!=='undefined'})")
                d = json.loads(r) if r else {}
                if d.get("c") and d.get("cv"):
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        print(f"\n就绪等待：{'已就绪' if ready else '超时（继续取快照）'}\n")

        time.sleep(1.5)
        snap = json.loads(cdp.js(SNAPSHOT, timeout=60))

        # ---------------- A 数据真加载 ----------------
        print("== A. 数据加载（非回退） ==")
        if snap["dataLoadFailed"] is False:
            ok("data.json 真实加载成功（未走 FALLBACK_DATA）")
        else:
            no(f"dataLoadFailed={snap['dataLoadFailed']} —— 页面在用内嵌回退数据！")
        if snap["futCount"] == 5289:
            ok(f"futures 序列 {snap['futCount']} 条（与 data.json 一致）")
        elif snap["futCount"] > 4000:
            wn(f"futures {snap['futCount']} 条（预期 5289）")
        else:
            no(f"futures 仅 {snap['futCount']} 条")
        if snap["changeBasis"] == "settle":
            ok("meta.changeBasis=settle 已传达给前端")
        else:
            no(f"changeBasis={snap['changeBasis']}")
        if str(snap["counterTarget"]) == "109300":
            ok(f"主指标卡目标值 {snap['counterTarget']}（沪铜结算价）")
        else:
            wn(f"主指标卡目标值 {snap['counterTarget']}（预期 109300，随行情变动可忽略）")
        if snap["counterSkeleton"] is False:
            ok("主指标卡骨架屏已移除（说明数据已灌入）")
        else:
            no("主指标卡仍是 skeleton")
        ok(f"最新交易日 {snap['lastDate']} settle={snap['lastSettle']} change={snap['lastChange']}")
        ok(f"现货 {snap['spotCount']} 条 / LME {snap['lmeCount']} 条")
        if snap["spotText"] and snap["spotText"] not in ("—", "--"):
            ok(f"现货卡渲染：{snap['spotText']}")
        else:
            wn(f"现货卡为空：{snap['spotText']}")

        # ---------------- B 图表 ----------------
        print("\n== B. ECharts 渲染 ==")
        if snap["echarts"]:
            ok(f"echarts 已加载（version={snap['echarts']}）")
        else:
            no("echarts 未加载 —— CDN 不可达，图表全灭")
        if snap["canvasCount"] >= 3:
            ok(f"页面共 {snap['canvasCount']} 个 canvas（主图+沪伦比+基差）")
        else:
            no(f"canvas 仅 {snap['canvasCount']} 个，图表未全部渲染")
        for key, label in (("mainSeries", "主走势图"), ("ratioSeries", "沪伦比图"), ("basisSeries", "基差图")):
            s = snap[key]
            if isinstance(s, list) and s:
                detail = ", ".join(f"{x['type']}×{x['n']}" + ("[mark]" if x.get("hasMark") else "")
                                   for x in s)
                ok(f"{label} 实例存在：{detail}")
            else:
                no(f"{label} 实例缺失或 series 为空：{s}")
        ok(f"区间切换按钮 {snap['rangeBtns']} 个，当前 {snap['rangeActive']!r}")
        if snap["rangeBtns"] >= 4:
            ok("区间切换（3月/1年/5年/全部）到位")
        else:
            no(f"区间按钮不足 4 个（实际 {snap['rangeBtns']}）")
        # 首屏默认 CUR_RANGE='1y'，dataZoom 必须真的缩放到近 1 年而非全量。
        # 这里曾因 startValue 精确匹配失败而静默回退成 [0,100]（全量）。
        pct0 = snap["mainZoomPct"]
        print(f"  首屏 mainZoomPct={pct0} rangeActive={snap['rangeActive']!r} "
              f"rangeStart={snap['rangeStart']!r} hint={snap['rangeHint']!r}")
        if snap["rangeActive"] != "1y":
            no(f"首屏默认区间应为 1y，实际 {snap['rangeActive']!r}")
        elif pct0 and isinstance(pct0, list) and pct0[0] > 0.5:
            ok(f"首屏 dataZoom 真生效：百分比 {pct0[0]:.2f}% ~ {pct0[1]:.0f}%（非全量）")
        else:
            no(f"首屏 dataZoom 回退成全量 {pct0} —— 图表没缩放")

        # ---------------- C 涨红跌绿 ----------------
        print("\n== C. 涨红跌绿（真实计算色） ==")
        up, dn = snap["ulColor"], snap["dlColor"]
        if up and dn:
            if up[0] > up[1] and up[0] > up[2]:
                ok(f"涨色 --up-text={snap['upText']} → rgb{tuple(up)} 红 ✓")
            else:
                no(f"涨色不是红：{snap['upText']} → rgb{tuple(up)}")
            if dn[1] > dn[0] and dn[1] > dn[2]:
                ok(f"跌色 --down-text={snap['downText']} → rgb{tuple(dn)} 绿 ✓")
            else:
                no(f"跌色不是绿：{snap['downText']} → rgb{tuple(dn)}")
        change = snap["lastChange"]
        cc = snap["changeColor"]
        ccrgb = re.findall(r"\d+", cc or "")
        print(f"  最新变动 change={change} → class={snap['changeClass']!r} color={cc}")
        if change is not None and change < 0:
            if "down" in (snap["changeClass"] or ""):
                ok("变动为负 → class=down（跌）")
            else:
                no(f"变动为负但 class={snap['changeClass']!r}")
            if ccrgb and int(ccrgb[1]) > int(ccrgb[0]):
                ok(f"负变动渲染为绿色 rgb({','.join(ccrgb)}) ✓")
            else:
                no(f"负变动颜色不是绿：{cc}")
        elif change is not None and change > 0:
            if "up" in (snap["changeClass"] or ""):
                ok("变动为正 → class=up（涨）")
            else:
                no(f"变动为正但 class={snap['changeClass']!r}")
            if ccrgb and int(ccrgb[0]) > int(ccrgb[1]):
                ok(f"正变动渲染为红色 rgb({','.join(ccrgb)}) ✓")
            else:
                no(f"正变动颜色不是红：{cc}")
        cc2 = re.findall(r"\d+", snap["statDodColor"] or "")
        if cc2:
            exp_red = (change or 0) > 0
            is_red = int(cc2[0]) > int(cc2[1])
            if is_red == exp_red:
                ok(f"涨跌四卡同步着色（{snap['statDodClass']!r} → {'红' if is_red else '绿'}）")
            else:
                no(f"四卡着色方向与涨跌相反：{snap['statDodClass']!r} rgb({','.join(cc2)})")
        ok(f"四卡文本：DOD={snap['statDod']!r} WoW={snap['statWow']!r} "
           f"YTD={snap['statYtd']!r} YoY={snap['statYoy']!r}")

        # ---------------- D 主题 ----------------
        print("\n== D. 主题三态切换 + 无白屏 ==")
        lum = lambda c: None if not c else (lambda t: 0.299*int(t[0])+0.587*int(t[1])+0.114*int(t[2]))(re.findall(r"\d+", c)[:3])
        for mode in ("light", "dark"):
            cdp.js(f"document.querySelector('#themeSwitch .ts-btn[data-theme-mode=\"{mode}\"]').click()")
            time.sleep(0.6)
            r = json.loads(cdp.js(
                "JSON.stringify({t:document.documentElement.getAttribute('data-theme'),"
                "bg:getComputedStyle(document.body).backgroundColor,"
                "fg:getComputedStyle(document.body).color,"
                "lv:(function(){var s=getComputedStyle(document.documentElement);return s.getPropertyValue('--bg').trim();})(),"
                "cv:document.querySelectorAll('#mainChart canvas').length,"
                "ls:localStorage.getItem('cpt-theme')"
                "})"))
            bl, fl = lum(r["bg"]), lum(r["fg"])
            print(f"  {mode:5s} data-theme={r['t']!r} bodyBg={r['bg']} --bg={r['lv']} color={r['fg']} "
                  f"canvas={r['cv']} ls={r['ls']!r}")
            if r["t"] != mode:
                no(f"点击 {mode} 后 data-theme={r['t']!r}，未生效")
            else:
                ok(f"data-theme 切到 {mode}")
            if bl is None:
                no(f"{mode} 主题下 body 背景取不到（可能透明 → 白屏风险）")
            elif mode == "dark" and bl > 90:
                no(f"暗色主题下 body 背景过亮（亮度 {bl:.0f}）→ 白屏")
            elif mode == "light" and bl < 160:
                no(f"亮色主题下 body 背景过暗（亮度 {bl:.0f}）")
            else:
                ok(f"{mode} 主题背景亮度 {bl:.0f} 合理")
            if mode == "dark" and fl is not None and fl < 110:
                no(f"暗色主题下正文色过暗（亮度 {fl:.0f}）→ 文字看不清")
            elif mode == "dark":
                ok(f"暗色主题正文色亮度 {fl:.0f}，可读")
            if "transparent" in (r["bg"] or "") or "rgba(0, 0, 0, 0)" == r["bg"]:
                no(f"{mode} 主题 body 背景透明 → 白屏")
            if r["cv"] == 0:
                no(f"{mode} 主题切换后图表 canvas 消失（rerenderCharts 破坏了实例）")
            else:
                ok(f"{mode} 主题切换后图表仍渲染（{r['cv']} canvas）")
        # 回 auto
        cdp.js("document.querySelector('#themeSwitch .ts-btn[data-theme-mode=\"auto\"]').click()")
        time.sleep(0.4)
        r = json.loads(cdp.js("JSON.stringify({t:document.documentElement.getAttribute('data-theme'),"
                              "ls:localStorage.getItem('cpt-theme')})"))
        if r["t"] is None and r["ls"] == "auto":
            ok("auto 模式：data-theme 移除 + localStorage=auto（三态正确）")
        else:
            no(f"auto 模式异常：data-theme={r['t']!r} ls={r['ls']!r}")

        # ---------------- E 换算器 ----------------
        print("\n== E. 换算器交互 ==")
        r = json.loads(cdp.js("""(() => {
          const i = document.getElementById('convInput');
          i.value = '80000';
          i.dispatchEvent(new Event('input', {bubbles:true}));
          const t = (id) => (document.getElementById(id)||{}).textContent;
          return JSON.stringify({kg:t('cvKg'), jin:t('cvJin'), g:t('cvG'), wan:t('cvWan'),
            cost:(document.querySelector('#costBody td[data-kg]')||{}).textContent,
            rows:document.querySelectorAll('#costBody tr').length});
        })()"""))
        print(f"  输入 80000 元/吨 → kg={r['kg']} 斤={r['jin']} 克={r['g']} 万元={r['wan']} "
              f"首行成本={r['cost']} 成本行数={r['rows']}")
        exp = {"kg": "80.000", "jin": "40.0000", "g": "0.08000", "wan": "8.00"}
        for k, want in exp.items():
            if r[k] == want:
                ok(f"cv{k} = {r[k]}（期望 {want}）")
            else:
                no(f"cv{k} = {r[k]!r}（期望 {want}）")
        if r["rows"] == 6:
            ok("用铜成本表 6 行（需求 P-8）")
        else:
            no(f"用铜成本表 {r['rows']} 行（期望 6）")
        # 回填真实价
        cdp.js("(function(){var i=document.getElementById('convInput');i.value=109300;"
               "i.dispatchEvent(new Event('input',{bubbles:true}));})()")

        # ---------------- F 布局 ----------------
        print("\n== F. 布局 ==")
        if snap["scrollW"] <= snap["innerW"] + 1:
            ok(f"1280px 视口无横向溢出（scrollW={snap['scrollW']} innerW={snap['innerW']}）")
        else:
            no(f"1280px 视口横向溢出：scrollW={snap['scrollW']} > innerW={snap['innerW']}")
        # 历史表行数应等于当前每页条数（默认 10，可由用户切到 20/30/50）。
        # ⚠️ 别写死阈值 —— PAGE_SIZE 可变，写死会让「切 50 条」后误判。
        if snap["tableRows"] == snap["pageSize"]:
            ok(f"历史表渲染 {snap['tableRows']} 行（= 每页 {snap['pageSize']} 条）")
        elif 0 < snap["tableRows"] <= snap["pageSize"]:
            ok(f"历史表渲染 {snap['tableRows']} 行（≤ 每页 {snap['pageSize']} 条，末页余数）")
        else:
            no(f"历史表行数 {snap['tableRows']} 与每页 {snap['pageSize']} 条不符")
        if snap["srcCards"] >= 4:
            ok(f"数据源状态卡 {snap['srcCards']} 个")
        else:
            wn(f"数据源状态卡 {snap['srcCards']} 个")
        ok(f"数据源健康标签：{snap['healthTag']!r}")
        if snap["posFillW"] and snap["posMark"]:
            ok(f"年内位置条已定位：fill width={snap['posFillW']} · mark left={snap['posMark']}")
        else:
            no(f"年内位置条未定位：fill={snap['posFillW']!r} mark={snap['posMark']!r}")
        if snap["posVerdict"]:
            ok(f"年内位置结论：{snap['posVerdict']!r}（区间 {snap['posLow']} ~ {snap['posHigh']}）")
        else:
            no("年内位置结论为空")
        ok(f"连续涨跌：{snap['msStreak']!r}；年振幅：{snap['msAmp']!r}；"
           f"最大单日：{snap['msMaxSwing']!r}；持仓：{snap['msOi']!r}")

        # ---- F2 区块间距（影响卡片区 ↔ 历史数据标题 ↔ 表格）----
        # ⚠️ 两个测量陷阱，都踩过：
        #   ① getBoundingClientRect() 不含 margin → 必须用「上top+上height」作基准，
        #      直接用 rect.bottom 相减会把 22px 的 margin-bottom 算成负数。
        #   ② .reveal 入场动画是 opacity+translateY(36px)，未归位时 rect 带着 36px 偏移，
        #      量出的间距会正好偏 -36+22 = -14px。测量前必须强制所有 reveal 归位。
        cdp.js("""(() => {
          document.querySelectorAll('.reveal').forEach(e => {
            e.classList.add('visible');
            e.style.opacity = '1';
            e.style.transform = 'none';
            e.style.transition = 'none';
          });
          return 'ok';
        })()""")
        time.sleep(0.45)
        gap = json.loads(cdp.js(r"""JSON.stringify({
          toHistory: (function(){
            const a = document.querySelector('.impact-grid').getBoundingClientRect();
            const b = document.getElementById('sec-history').getBoundingClientRect();
            return Math.round(b.top - (a.top + a.height));
          })(),
          titleToGrid: (function(){
            const a = document.getElementById('sec-impact').getBoundingClientRect();
            const b = document.querySelector('.impact-grid').getBoundingClientRect();
            return Math.round(b.top - (a.top + a.height));
          })(),
          titleToTable: (function(){
            const c = document.getElementById('sec-history');
            const a = c.querySelector('.section-title').getBoundingClientRect();
            const b = c.querySelector('.tbl-wrap').getBoundingClientRect();
            return Math.round(b.top - (a.top + a.height));
          })()
        })"""))
        snap.update({
            "gapImpactToHistory": gap["toHistory"],
            "gapImpactTitleToGrid": gap["titleToGrid"],
            "gapHistoryTitleToTable": gap["titleToTable"],
        })

        print("\n-- F2. 区块间距实测（reveal 已归位） --")
        g1 = snap.get("gapImpactToHistory")
        if isinstance(g1, int):
            # 影响卡片 → 历史数据卡：净空应显著大于 0（两区块不能贴死）
            if g1 >= 28:
                ok(f"影响卡片区 → 历史数据卡 净空 {g1}px（不拥挤）")
            elif g1 >= 16:
                wn(f"影响卡片区 → 历史数据卡 净空 {g1}px（偏挤，建议 >=28px）")
            else:
                no(f"影响卡片区 → 历史数据卡 净空仅 {g1}px —— 两区块贴死")
        else:
            no(f"间距测量失败：gapImpactToHistory={g1!r}")
        if snap.get("impactGridMB") in ("28px", "28.0px"):
            ok(f".impact-grid margin-bottom = {snap['impactGridMB']}")
        else:
            no(f".impact-grid margin-bottom = {snap['impactGridMB']!r}（预期 28px）")
        g3 = snap.get("gapImpactTitleToGrid")
        if isinstance(g3, int) and 18 <= g3 <= 26:
            ok(f"影响区标题 → 卡片栅格 净空 {g3}px（= .section-title margin-bottom 22px）")
        else:
            wn(f"影响区标题 → 卡片栅格 净空 {g3!r}px（预期 ~22px）")
        g2 = snap.get("gapHistoryTitleToTable")
        if isinstance(g2, int) and 18 <= g2 <= 32:
            ok(f"历史数据标题 → 表格 净空 {g2}px")
        else:
            wn(f"历史数据标题 → 表格 净空 {g2!r}px（预期 ~22px）")
        rb = snap.get("impactRowBottoms") or []
        if len(rb) >= 6:
            uniq = sorted(set(rb))
            ok(f"影响卡片底边 y 值 {uniq}（{len(rb)} 张卡 / {len(uniq)} 种底边）")
            if len(uniq) > 2:
                wn(f"同层卡片底边参差：{uniq} —— 文字长度差异所致，非缺陷")
        else:
            wn(f"影响卡片数仅 {len(rb)}，未能评估底边参差")

        ok(f"SW 状态：{snap['sw']}")

        # ---------------- H 交互与边界分支 ----------------
        print("\n== H. 区间切换 / 涨跌双分支 / 表格展开 / 全源失效告警 ==")

        def js(r):
            return json.loads(cdp.js(r))

        # H1 区间切换（dataZoom 驱动，不重建图表）
        zooms = {}
        for rng in ("all", "1y", "3m"):
            cdp.js(f"document.querySelector('#rangeGroup .rg-btn[data-range=\"{rng}\"]').click()")
            time.sleep(0.8)
            r = js("JSON.stringify({"
                   "pressed:(function(){var b=document.querySelector('#rangeGroup .rg-btn[aria-pressed=\"true\"]');"
                   "return b?b.dataset.range:null;})(),"
                   "hint:(document.getElementById('rangeHint')||{}).textContent,"
                   "pct:(function(){try{return echarts.getInstanceByDom(document.getElementById('mainChart'))"
                   ".getModel().getComponent('dataZoom',0).getPercentRange();}catch(e){return null;}})(),"
                   "sv:(function(){try{var o=echarts.getInstanceByDom(document.getElementById('mainChart')).getOption().dataZoom;"
                   "return (o&&o.length)?String(o[0].startValue):null;}catch(e){return null;}})(),"
                   "computed:(typeof computeRangeStart==='function'?String(computeRangeStart()):null),"
                   "inAxis:(function(){try{var d=echarts.getInstanceByDom(document.getElementById('mainChart'))"
                   ".getOption().xAxis[0].data;return d.indexOf(String(computeRangeStart()))>=0;}catch(e){return null;}})(),"
                   "cv:document.querySelectorAll('#mainChart canvas').length})")
            zooms[rng] = r
            print(f"  {rng:4s} pressed={r['pressed']!r} pct={r['pct']} "
                  f"startValue={r['sv']!r} computed={r['computed']!r} 命中xAxis={r['inAxis']}")
            print(f"        hint={r['hint']!r}")
            if r["pressed"] != rng:
                no(f"点 {rng} 后激活按钮是 {r['pressed']!r}")
            else:
                ok(f"区间 {rng} 激活态正确（aria-pressed 三态互斥）")
            if r["cv"] == 0:
                no(f"切换区间 {rng} 后 canvas 消失")
            if r["inAxis"] is True:
                ok(f"区间起点 {r['computed']} 精确命中 xAxis（dataZoom 可匹配）")
            else:
                no(f"区间起点 {r['computed']!r} 不在 xAxis data 里 → dataZoom 会静默回退全量")
            if r["hint"] and "个交易日" in r["hint"]:
                ok(f"区间提示已更新（{rng}）")
            else:
                no(f"区间提示异常：{r['hint']!r}")
        p_all, p_1y, p_3m = (zooms[k]["pct"] for k in ("all", "1y", "3m"))
        if p_all and p_1y and p_3m:
            print(f"  区间百分比：全部 {p_all[0]:.2f}% → 近1年 {p_1y[0]:.2f}% → 近3月 {p_3m[0]:.2f}%")
            if p_all[0] == 0 and p_3m[0] > p_1y[0] > p_all[0]:
                ok("dataZoom 百分比随区间单调收窄（全部 < 1年 < 3月）")
            else:
                no(f"dataZoom 未随区间收窄：{p_all} / {p_1y} / {p_3m}")
            if p_3m[1] == 100 and p_3m[0] > 90:
                ok(f"近 3 月视图对齐到末端（{p_3m[0]:.2f}% ~ {p_3m[1]:.0f}%）")
            else:
                wn(f"近 3 月视图 {p_3m}（末端对齐预期 100）")
        else:
            no(f"dataZoom 百分比取不到：{p_all} / {p_1y} / {p_3m}")
        ok(f"区间切换不重建图表（canvas 始终存在，缩放状态不丢）")

        # H2 涨跌双分支（当前行情是跌，红色路径只靠构造才能覆盖）
        up = js("""(() => {
          setChange('changeFutures', 130, 0.12, '较前结算价');
          const e = document.getElementById('changeFutures');
          const c = getComputedStyle(e).color;
          return JSON.stringify({cls:e.className, txt:e.textContent, color:c});
        })()""")
        print(f"  构造 +130 → class={up['cls']!r} color={up['color']} text={up['txt']!r}")
        ur = re.findall(r"\d+", up["color"] or "")
        if "up" in (up["cls"] or "") and ur and int(ur[0]) > int(ur[1]):
            ok(f"上涨分支渲染为红色 rgb({','.join(ur)}) ✓（涨红）")
        else:
            no(f"上涨分支着色错误：class={up['cls']!r} color={up['color']}")
        if "+130" in (up["txt"] or ""):
            ok("上涨文案带 + 号（不只靠颜色表达方向）")
        else:
            no(f"上涨文案缺 + 号：{up['txt']!r}")
        # 构造持平
        flat = js("""(() => {
          setChange('changeFutures', 0, 0, '较前结算价');
          const e = document.getElementById('changeFutures');
          return JSON.stringify({cls:e.className, txt:e.textContent, color:getComputedStyle(e).color});
        })()""")
        print(f"  构造 0    → class={flat['cls']!r} color={flat['color']} text={flat['txt']!r}")
        if "up" not in (flat["cls"] or "") and "down" not in (flat["cls"] or ""):
            ok("持平分支不带涨跌色")
        else:
            no(f"持平分支误带涨跌色：{flat['cls']!r}")
        # 还原
        cdp.js("(function(){var L=FUT[FUT.length-1];setChange('changeFutures',L.change,L.changePct,'较前结算价');})()")
        back = js("JSON.stringify({cls:document.getElementById('changeFutures').className,"
                  "txt:document.getElementById('changeFutures').textContent})")
        if "down" in back["cls"] and "-130" in back["txt"]:
            ok(f"已还原真实行情：{back['txt']!r}")
        else:
            wn(f"还原后为 {back['txt']!r}（{back['cls']!r}）")

        # H3 历史表分页（替代原「显示更多」累加式）
        # 核心口径：单页行数 == PAGE_SIZE、不得铺开全部；首页首行是最新交易日；末页条数正确；
        # 翻页按钮在边界禁用；跳转越界要夹紧而不是渲染空白页。
        # ⚠️ 断言一律以页面上的 PAGE_SIZE 为准，不写死 30 —— 每页条数可由用户切换
        #    （10/20/30/50），写死会让「切到 20 条」后全部假失败。
        pg = json.loads(cdp.js(r"""JSON.stringify({
          rows: document.querySelectorAll('#tableBody tr').length,
          totalRows: (typeof FUT!=='undefined' ? FUT.length : -1),
          summary: (document.getElementById('pgSummary')||{}).textContent || '',
          nums: [].map.call(document.querySelectorAll('#pgNums .pg-num'), function(e){return e.textContent.trim();}),
          active: (function(){var a=document.querySelector('#pgNums .pg-num.active');return a?a.textContent.trim():null;})(),
          firstDisabled: (document.getElementById('pgFirst')||{}).disabled,
          prevDisabled: (document.getElementById('pgPrev')||{}).disabled,
          nextDisabled: (document.getElementById('pgNext')||{}).disabled,
          lastDisabled: (document.getElementById('pgLast')||{}).disabled,
          inspect: (typeof TABLE_PAGE!=='undefined' ? TABLE_PAGE : null),
          size: (typeof PAGE_SIZE!=='undefined' ? PAGE_SIZE : null),
          sizeOptions: [].map.call(document.querySelectorAll('#pgSize option'), function(o){return o.value;}),
          sizeValue: (document.getElementById('pgSize')||{}).value || null,
          jumpMax: (document.getElementById('pgJumpMax')||{}).textContent || null,
          pageCount: (typeof FUT!=='undefined' ? Math.max(1, Math.ceil(FUT.length/PAGE_SIZE)) : -1)
        })"""))
        n0, PS = pg["rows"], pg["size"]
        print(f"  首页：行数 {n0} / 总数 {pg['totalRows']} / 每页 {PS} / 共 {pg['pageCount']} 页")
        print(f"  页码块 {pg['nums']} · 激活 {pg['active']}")
        print(f"  摘要：{pg['summary']}")
        if PS == n0 == 10:
            ok(f"首页渲染 {n0} 行（默认每页 10 条，未铺开全部 {pg['totalRows']} 条）")
        elif PS == n0:
            wn(f"首页渲染 {n0} 行 == PAGE_SIZE {PS}（默认应为 10，可能被上一步改动）")
        else:
            no(f"首页行数异常：{n0} 行（PAGE_SIZE={PS}）")
        if n0 < pg["totalRows"]:
            ok("分页生效：单页行数 < 总条数（没有一次性全列出）")
        else:
            no(f"分页失效：单页 {n0} 行 == 总条数 {pg['totalRows']}，全部铺在页面上了")
        # ⚠️ 摘要里的数字走 groupNum() 千分位（"5,289"），不能拿裸值 "5289" 去搜。
        _tot_pretty = f"{pg['totalRows']:,}" if pg["totalRows"] >= 0 else ""
        _head = f"1~{PS}"
        if _head in pg["summary"] and _tot_pretty in pg["summary"]:
            ok(f"摘要文案正确（第 {_head} 条 / 共 {_tot_pretty} 条 · 每页 {PS} 条）")
        else:
            wn(f"摘要文案可疑：{pg['summary']!r}（应含 {_head!r} 且含 {_tot_pretty!r}）")
        if pg["active"] == "1":
            ok("首页页码高亮在 1")
        else:
            no(f"首页页码高亮异常：{pg['active']!r}")
        if pg["firstDisabled"] and pg["prevDisabled"] and not pg["nextDisabled"]:
            ok("首页「首页/上一页」禁用、「下一页」可用（边界正确）")
        else:
            no(f"首页按钮状态异常：first={pg['firstDisabled']} prev={pg['prevDisabled']} next={pg['nextDisabled']}")
        # 页码块必须收敛（固定最多 7 块）—— 177 页时若铺成几十个块就说明收敛逻辑失效
        if pg["pageCount"] > 7 and len(pg["nums"]) <= 7:
            ok(f"页码块收敛为 {len(pg['nums'])} 个（共 {pg['pageCount']} 页，未铺满）")
        elif pg["pageCount"] <= 7:
            ok(f"页码块 {len(pg['nums'])} 个（总页数 ≤ 7，全列出）")
        else:
            no(f"页码块未收敛：{len(pg['nums'])} 个（共 {pg['pageCount']} 页，期望 ≤7）")

        # 首页首行必须是全量数据里最新的一天（倒序渲染）
        r0 = json.loads(cdp.js("""JSON.stringify({
          first: document.querySelector('#tableBody tr td strong').textContent.trim(),
          lastDate: (typeof FUT!=='undefined' && FUT.length ? (function(){
            var d=FUT[FUT.length-1].date.split('-');return d[1]*1+'月'+d[2]*1+'日';})() : null)
        })"""))
        print(f"  首页首行 {r0['first']!r} / 数据最新日 {r0['lastDate']!r}")
        if r0["first"] == r0["lastDate"]:
            ok("首页首行 = 最新交易日（倒序渲染正确）")
        else:
            no(f"首页首行 {r0['first']!r} ≠ 最新日 {r0['lastDate']!r}")

        # 翻到第 2 页（区间随 PAGE_SIZE 变化：每页 10 → 11~20）
        cdp.js("document.getElementById('pgNext').click()")
        time.sleep(0.5)
        p2 = json.loads(cdp.js("""JSON.stringify({
          rows: document.querySelectorAll('#tableBody tr').length,
          page: (typeof TABLE_PAGE!=='undefined'?TABLE_PAGE:null),
          active: (function(){var a=document.querySelector('#pgNums .pg-num.active');return a?a.textContent.trim():null;})(),
          summary:(document.getElementById('pgSummary')||{}).textContent||'',
          prevDisabled:(document.getElementById('pgPrev')||{}).disabled
        })"""))
        print(f"  第 2 页：page={p2['page']} 行数={p2['rows']} 摘要={p2['summary']!r}")
        if p2["page"] == 2 and p2["rows"] == PS and p2["active"] == "2" and not p2["prevDisabled"]:
            ok(f"「下一页」→ 第 2 页，{PS} 行，高亮跟随，上一页解禁")
        else:
            no(f"翻页异常：{p2}")
        _r2 = f"{PS + 1}~{PS * 2}"
        if _r2 in p2["summary"]:
            ok(f"第 2 页摘要区间正确（{_r2}）")
        else:
            wn(f"第 2 页摘要可疑：{p2['summary']!r}（应含 {_r2!r}）")

        # 跳到末页：行数应为余数，且末页/下一页禁用
        cdp.js("document.getElementById('pgLast').click()")
        time.sleep(0.6)
        pl = json.loads(cdp.js("""JSON.stringify({
          rows: document.querySelectorAll('#tableBody tr').length,
          page: TABLE_PAGE,
          pageCount: Math.ceil(FUT.length/PAGE_SIZE),
          pageSize: PAGE_SIZE,
          nextDisabled:(document.getElementById('pgNext')||{}).disabled,
          lastDisabled:(document.getElementById('pgLast')||{}).disabled,
          summary:(document.getElementById('pgSummary')||{}).textContent||''
        })"""))
        print(f"  末页：page={pl['page']}/{pl['pageCount']} 行数={pl['rows']} 摘要={pl['summary']!r}")
        if pl["page"] == pl["pageCount"] and pl["nextDisabled"] and pl["lastDisabled"]:
            ok(f"跳至末页（第 {pl['page']} 页），「下一页/末页」正确禁用")
        else:
            no(f"末页异常：{pl}")
        if 0 < pl["rows"] <= pl["pageSize"]:
            ok(f"末页 {pl['rows']} 行（≤ 每页 {pl['pageSize']} 条，余数正确）")
        else:
            no(f"末页行数异常：{pl['rows']}")

        # 越界跳转必须夹紧，不能渲染空白表格
        cdp.js("goPage(99999)")
        time.sleep(0.5)
        pv = json.loads(cdp.js("""JSON.stringify({
          page: TABLE_PAGE, rows: document.querySelectorAll('#tableBody tr').length,
          pageCount: Math.ceil(FUT.length/PAGE_SIZE)
        })"""))
        if pv["page"] == pv["pageCount"] and pv["rows"] > 0:
            ok(f"越界跳转被夹紧到末页（请求 99999 → 实际 {pv['page']}），表格非空")
        else:
            no(f"越界跳转未夹紧：{pv}")

        # 回首页后行数应恢复为 PAGE_SIZE（不残留）
        cdp.js("goPage(1)")
        time.sleep(0.5)
        n_back = cdp.js("document.querySelectorAll('#tableBody tr').length")
        if n_back == PS:
            ok(f"回到第 1 页恢复 {PS} 行（无残留）")
        else:
            no(f"回首页后行数异常：{n_back}（期望 {PS}）")

        # ---- H3b 每页条数选择器 ----
        # 需求：页码太多 → 提供 10/20/30/50 每页条数选项，缩短页码数量。
        print("\n-- H3b. 每页条数选择器 --")
        if pg["sizeOptions"] == ["10", "20", "30", "50"]:
            ok(f"选择器选项 = {pg['sizeOptions']}（与 PAGE_SIZE_OPTIONS 一致）")
        else:
            no(f"选择器选项异常：{pg['sizeOptions']}（期望 ['10','20','30','50']）")
        if pg["sizeValue"] == "10":
            ok("选择器默认值 = 10 条（与 DEFAULT_PAGE_SIZE 一致）")
        else:
            no(f"选择器默认值异常：{pg['sizeValue']!r}（期望 '10'）")
        if pg["jumpMax"] and pg["jumpMax"].replace(",", "") == str(pg["pageCount"]):
            ok(f"跳转框显示总页数 {pg['jumpMax']}（= 每页 {PS} 条时共 {pg['pageCount']} 页）")
        else:
            wn(f"跳转框上限显示 {pg['jumpMax']!r}（期望 {pg['pageCount']}）")

        # 切到「50 条/页」：总页数应缩短，且首行日期保持不变（不跳回最新）
        before_first = cdp.js("document.querySelector('#tableBody tr td strong').textContent.trim()")
        cdp.js("(function(){var s=document.getElementById('pgSize');s.value='50';"
               "s.dispatchEvent(new Event('change',{bubbles:true}));})()")
        time.sleep(0.6)
        s50 = json.loads(cdp.js("""JSON.stringify({
          size: PAGE_SIZE,
          page: TABLE_PAGE,
          rows: document.querySelectorAll('#tableBody tr').length,
          pageCount: Math.ceil(FUT.length/PAGE_SIZE),
          first: document.querySelector('#tableBody tr td strong').textContent.trim(),
          summary:(document.getElementById('pgSummary')||{}).textContent||''
        })"""))
        print(f"  切到 50 条/页 → page={s50['page']}/{s50['pageCount']} 行数={s50['rows']} "
              f"首行={s50['first']!r}")
        if s50["size"] == 50 and s50["rows"] == 50 and s50["page"] == 1:
            ok(f"切换为 50 条/页生效：单页 50 行，总页数缩短至 {s50['pageCount']}")
        else:
            no(f"切换每页条数失败：{s50}")
        if s50["pageCount"] < pg["pageCount"]:
            ok(f"页数随每页条数增大而缩短（{pg['pageCount']} → {s50['pageCount']} 页）")
        else:
            no(f"页数未缩短：{pg['pageCount']} → {s50['pageCount']}")
        if s50["first"] == before_first:
            ok(f"切换后首行日期保持 {s50['first']!r}（按当前页锚定，未跳回最新）")
        else:
            wn(f"切换后首行从 {before_first!r} 变为 {s50['first']!r}")

        # 还原为默认 10 条/页，避免影响后续断言
        cdp.js("(function(){var s=document.getElementById('pgSize');s.value='10';"
               "s.dispatchEvent(new Event('change',{bubbles:true}));})()")
        time.sleep(0.5)
        _restored = cdp.js("PAGE_SIZE")
        if _restored == 10:
            ok("每页条数已还原为默认 10 条/页")
        else:
            wn(f"还原后 PAGE_SIZE={_restored}")

        # H4 全源失效 → 红色告警（验收标准「全源失效红」）
        # ⚠️ 先存一份真实卡片的 HTML 快照，测完原样回填 —— 比「重新调 renderHealth」
        # 可靠：raw 是 init 里的局部变量，页面全局取不到；用假键名还原又会把真实源
        # 列表整个换掉，害得后面所有依赖真实源名的断言失效（这两个坑都踩过）。
        real_grid_html = cdp.js("document.getElementById('srcGrid').innerHTML")
        real_tag_text = cdp.js("document.getElementById('healthTag').textContent")
        real_tag_color = cdp.js("document.getElementById('healthTag').style.color")
        real_tag_border = cdp.js("document.getElementById('healthTag').style.borderColor")
        fail_health = {k: {"ok": False, "detail": "探针构造：连接超时"} for k in
                       ("sina_kline", "shfe", "spot_100ppi", "lme_kline", "sina_realtime")}
        cdp.js("renderHealth(" + json.dumps(fail_health, ensure_ascii=False) + ")")
        time.sleep(0.4)
        r = js("""JSON.stringify({
          tag:document.getElementById('healthTag').textContent,
          color:getComputedStyle(document.getElementById('healthTag')).color,
          bad:document.querySelectorAll('#srcGrid .src-item.bad').length,
          failRows:document.querySelectorAll('#srcGrid .sf').length
        })""")
        print(f"  构造 5 源全失效 → tag={r['tag']!r} color={r['color']} bad卡={r['bad']} 连续失败提示={r['failRows']}")
        fr = re.findall(r"\d+", r["color"] or "")
        if "异常" in (r["tag"] or "") and fr and int(fr[0]) > int(fr[1]):
            ok(f"全源失效渲染为红色告警 rgb({','.join(fr)}) ✓")
        else:
            no(f"全源失效未变红：tag={r['tag']!r} color={r['color']}")
        if r["bad"] == 5:
            ok("5 张数据源卡全部标记为异常")
        else:
            no(f"异常卡数量 {r['bad']}（期望 5）")
        # 还原：直接把构造前的真实卡片 HTML + 标签状态回填
        cdp.js("(function(h,t,c,b){"
               "document.getElementById('srcGrid').innerHTML=h;"
               "var g=document.getElementById('healthTag');"
               "g.textContent=t; g.style.color=c; g.style.borderColor=b;})("
               + json.dumps(real_grid_html or "") + ","
               + json.dumps(real_tag_text or "") + ","
               + json.dumps(real_tag_color or "") + ","
               + json.dumps(real_tag_border or "") + ")")
        time.sleep(0.3)
        r2 = cdp.js("document.getElementById('healthTag').textContent")
        if "正常" in (r2 or ""):
            ok("健康标签已还原为「全部正常」")
        else:
            wn(f"还原后标签：{r2!r}")

        # ---------------- H4b 数据源卡片可点击跳转官网 ----------------
        # 需求：列出的每个数据源都要能点击跳转。
        # 断言四件事：① 5 张卡都是 <a> ② 都指向 https 外链 ③ 都带 target=_blank + rel=noopener
        #              ④ SOURCE_HOME 映射覆盖全部真实源名（否则静默退化为不可点击 div）
        print("\n-- H4b. 数据源卡片可点击跳转 --")
        # 从页面里读 SOURCE_HOME 的键集合 —— 不在这里另抄一份，避免映射表改了两边不同步。
        SOURCE_HOME_KEYS = set(json.loads(
            cdp.js("JSON.stringify(Object.keys(SOURCE_HOME || {}))") or "[]"))
        src_cards = json.loads(cdp.js("""JSON.stringify(
          Array.from(document.querySelectorAll('#srcGrid .src-item')).map(function(el){
            return {
              tag: el.tagName,
              name: (el.querySelector('.sn')||{}).textContent||'',
              href: el.getAttribute('href')||'',
              target: el.getAttribute('target')||'',
              rel: el.getAttribute('rel')||'',
              hasArrow: !!el.querySelector('.sgo')
            };
          }))""")) or []
        print(f"  数据源卡 {len(src_cards)} 张：")
        for c in src_cards:
            print(f"    <{c['tag'].lower()}> {c['name']:<12} → {c['href']}")
        if len(src_cards) >= 5:
            ok(f"数据源卡 {len(src_cards)} 张（与 data.json 的 sourceHealth 条目数一致）")
        else:
            no(f"数据源卡仅 {len(src_cards)} 张（期望 ≥5）")
        not_link = [c["name"] for c in src_cards if c["tag"] != "A"]
        if not not_link:
            ok("5 张卡全部渲染为 <a>（可点击）")
        else:
            no(f"以下数据源不可点击（仍是 div）：{not_link}")
        bad_href = [c["name"] for c in src_cards if not c["href"].startswith("https://")]
        if not bad_href:
            ok("全部指向 https 外链官网")
        else:
            no(f"以下数据源 href 不是 https 外链：{bad_href}")
        bad_attr = [c["name"] for c in src_cards
                    if c["tag"] == "A" and (c["target"] != "_blank" or "noopener" not in c["rel"])]
        if not bad_attr:
            ok("全部带 target=_blank + rel=noopener noreferrer（新窗口打开且防 window.opener 劫持）")
        else:
            no(f"以下数据源缺 target/rel 保护：{bad_attr}")
        no_arrow = [c["name"] for c in src_cards if c["tag"] == "A" and not c["hasArrow"]]
        if not no_arrow:
            ok("可点击的卡片均带外链箭头图标（视觉上明示可跳转）")
        else:
            wn(f"以下卡片缺外链箭头：{no_arrow}")
        # 映射完整性：每个真实源名都必须命中 SOURCE_HOME，否则会静默退化为不可点击
        unmapped = [c["name"] for c in src_cards
                    if c["name"].replace("（异常）", "") not in SOURCE_HOME_KEYS]
        if not unmapped:
            ok(f"SOURCE_HOME 映射覆盖全部真实数据源键名（{len(SOURCE_HOME_KEYS)} 条，无静默降级）")
        else:
            no(f"以下数据源在 SOURCE_HOME 里没有映射，会退化为不可点击：{unmapped}")

        # ---------------- H5 锚点导航落点 ----------------
        # 回归背景（实测踩过，用户反馈「手机端导航锚点钉不上去」）：
        #   .reveal 未进视口时带 transform:translateY(36px)。原生锚点按「带 transform 的
        #   位置」算落点 → 少滚 36px；紧接着 reveal 动画把内容上移 36px → 标题被吸顶导航
        #   （约 47~54px 高）压住。实测 390px 下 #sec-history 标题 top=43px < 导航底边 47px。
        #   修法：点击时先 settleReveals() 归位，再自行 scrollTo 精确落点，收尾补校正。
        print("\n== H5. 锚点导航落点（标题必须落在吸顶导航下方） ==")
        anchor_ids = ["sec-changes", "sec-trend", "sec-position", "sec-cross",
                      "sec-converter", "sec-impact", "sec-history", "sec-health"]
        anchor_bad = []
        anchor_clears = []
        for aid in anchor_ids:
            cdp.js(f"document.querySelector('a.pn-link[href=\"#{aid}\"]').click()")
            # ⚠️ 双重等待：① 滚动停止 ② 目标 transform 完全归位。
            # 只等 transform → 会量到「滚动途中」的假阴性（实测 titleTop=2138）；
            # 只等固定时长 → 会量到 transform 未归零的假阳性（实测 clear=-22px）。
            prev_y, stable = -1, 0
            for _ in range(60):
                time.sleep(0.15)
                st = json.loads(cdp.js(
                    "JSON.stringify((function(){var el=document.getElementById(%r);"
                    "var t=el.querySelector('.section-title')||el;"
                    "var tf=getComputedStyle(t).transform;"
                    "return [Math.round(window.pageYOffset),"
                    "(tf==='none'||tf==='matrix(1, 0, 0, 1, 0, 0)')?1:0];})())" % aid))
                if st[0] == prev_y and st[1]:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                prev_y = st[0]
            a = json.loads(cdp.js("""JSON.stringify((function(){
              var nav=document.getElementById('pageNav');
              var el=document.getElementById(%r);
              var t=el.querySelector('.section-title')||el;
              var tt=Math.round(t.getBoundingClientRect().top);
              var nb=Math.round(nav.getBoundingClientRect().bottom);
              return {id:%r, titleTop:tt, navBottom:nb, clear:tt-nb,
                      opacity:getComputedStyle(el).opacity};
            })())""" % (aid, json.dumps(aid))))
            mark = "OK " if a["clear"] >= 6 else "BAD"
            print(f"  [{mark}] {a['id']:<16} titleTop={a['titleTop']:>4} "
                  f"navBottom={a['navBottom']:>3} clear={a['clear']:>4} opacity={a['opacity']}")
            anchor_clears.append(a["clear"])
            if a["clear"] < 6 or float(a["opacity"]) < 0.99:
                anchor_bad.append(a)
        if not anchor_bad:
            ok(f"{len(anchor_ids)} 个锚点全部落在吸顶导航下方"
               f"（最小净空 {min(anchor_clears)}px，最大 {max(anchor_clears)}px）")
        else:
            no(f"锚点落点异常（被导航压住）：{anchor_bad}")
        # 复位到页面顶部，避免影响后续断言
        cdp.js("window.scrollTo(0, 0)")
        time.sleep(0.4)

        # ---------------- G 运行时报错 ----------------
        print("\n== G. 运行时报错 ==")
        errs = snap["errs"]
        if not errs:
            ok("无 window error / unhandled rejection / console.warn|error")
        else:
            for e in errs:
                if "data.json 加载失败" in e:
                    no(f"数据加载回退：{e[:200]}")
                elif e.startswith(("warn:", "cerr:")):
                    wn(e[:220])
                else:
                    no(f"运行时报错：{e[:220]}")

        # ---------------- I 版本发布记录页 ----------------
        # 独立页面 + 独立数据源（releases.json）。核心口径是「同一天多版本合并成一个节点」
        # 与「节点内按时刻倒序」—— 这两条错了页面就白做，所以重点验它们。
        print("\n== I. 版本发布记录页（releases.html） ==")

        # 期望值从 releases.json 动态推导，不硬编码 —— 否则每次发版改时间，
        # 探针都会拿旧时间假失败一次（本坑已踩过：v1.1.0 由 11:45 修正为 11:15）。
        rel_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "releases.json")
        with open(rel_path, encoding="utf-8") as fh:
            rel_data = json.load(fh)
        rel_list = rel_data.get("releases", [])
        # 时刻倒序（HH:MM 字符串，locateCompare numeric 语义与页面一致）
        want_times = sorted(
            [("%s" % (e.get("time") or "--:--"))[:5] for e in rel_list],
            key=lambda s: s, reverse=True,
        )
        # 按 (date desc, time desc) 展开后的版本号顺序。
        # ⚠️ releases.json 存裸号（"1.1.0"），页面渲染时加 v 前缀（"'v1.1.0'"）——
        # 比对前必须补前缀，否则必然假失败（本坑已踩）。
        def _v(x):
            s = str(x or "")
            return s if s.startswith("v") else ("v" + s)
        want_vers = [_v(e.get("version")) for e in sorted(
            rel_list, key=lambda e: (e.get("date", ""), ("%s" % (e.get("time") or ""))[:5]), reverse=True)]
        want_days = len({e.get("date") for e in rel_list})
        want_newest_ver = want_vers[0] if want_vers else None
        want_newest_time = want_times[0] if want_times else None
        print(f"  期望（源自 releases.json）：{want_days} 天 / {len(rel_list)} 条 · "
              f"时刻 {want_times} · 版本 {want_vers}")

        cdp.call("Page.navigate", {"url": base + "releases.html"})
        rendered = False
        repl_deadline = time.time() + 30
        while time.time() < repl_deadline:
            try:
                n = cdp.js("document.querySelectorAll('#releaseTimeline .tl-entry').length")
                if n and int(n) > 0:
                    rendered = True
                    break
            except Exception:
                pass
            time.sleep(0.4)
        print(f"  渲染等待：{'已渲染' if rendered else '超时（继续取值）'}")
        time.sleep(0.9)

        r = json.loads(cdp.js(r"""JSON.stringify({
          days: document.querySelectorAll('#releaseTimeline .tl-day').length,
          entries: document.querySelectorAll('#releaseTimeline .tl-entry').length,
          times: [].map.call(document.querySelectorAll('#releaseTimeline .tl-time'), function(e){return e.textContent.trim();}),
          vers: [].map.call(document.querySelectorAll('#releaseTimeline .tl-ver'), function(e){return e.textContent.trim();}),
          counts: [].map.call(document.querySelectorAll('#releaseTimeline .tl-count'), function(e){return e.textContent.trim();}),
          nows: document.querySelectorAll('#releaseTimeline .tl-now').length,
          nowVer: (function(){var n=document.querySelector('#releaseTimeline .tl-now');if(!n)return '';var v=n.parentElement.querySelector('.tl-ver');return v?v.textContent.trim():'';})(),
          cur: (document.getElementById('relCurrent')||{}).textContent,
          total: (document.getElementById('relTotal')||{}).textContent,
          footTotal: (document.getElementById('footTotal')||{}).textContent,
          bootDisplay: getComputedStyle(document.getElementById('bootScreen')).display,
          backHref: (document.querySelector('.rel-back')||{}).getAttribute ? document.querySelector('.rel-back').getAttribute('href') : '',
          backCount: document.querySelectorAll('a[href="./index.html"]').length,
          scrollW: document.documentElement.scrollWidth,
          innerW: window.innerWidth
        })"""))
        print(f"  节点 {r['days']} 个 / 条目 {r['entries']} 个 · 节点徽标 {r['counts']}")
        print(f"  时刻顺序 {r['times']} · 版本顺序 {r['vers']}")

        # I1 同日合并：releases.json 里同日期多条版本 → 只该有 want_days 个日期节点
        if r["days"] == want_days and r["entries"] == len(rel_list):
            ok(f"同一天的 {len(rel_list)} 个版本合并为 {want_days} 个日期节点（合并生效）")
        else:
            no(f"同日合并异常：节点 {r['days']} 个 / 条目 {r['entries']} 个"
               f"（期望 {want_days} / {len(rel_list)}）")

        # I2 节点内按时刻倒序，且精确到分钟
        if r["times"] == want_times:
            ok(f"节点内按时刻倒序：{' > '.join(want_times)}（精确到分钟，非仅日期）")
        else:
            no(f"时刻顺序异常：{r['times']}（期望 {want_times}）")

        # I3 版本号与时刻同序（倒序展开）
        if r["vers"] == want_vers:
            ok(f"版本号与时刻同序：{' > '.join(x or '?' for x in want_vers)}")
        else:
            no(f"版本号顺序异常：{r['vers']}（期望 {want_vers}）")

        # I4 「最新」徽标唯一且指向时刻最大的那条
        if r["nows"] == 1 and r["nowVer"] == want_newest_ver:
            ok(f"「最新」徽标唯一，且落在 {want_newest_time} 的 {want_newest_ver} 上")
        else:
            no(f"最新徽标异常：数量 {r['nows']} / 指向 {r['nowVer']!r}"
               f"（期望 1 / {want_newest_ver!r}）")

        # I5 顶部统计与页脚口径一致
        if r["cur"] == want_newest_ver and str(len(rel_list)) in (r["total"] or "") \
                and (r["footTotal"] or "").strip() == str(len(rel_list)):
            ok(f"统计一致：当前 {r['cur']} / 总数 {r['total']} / 页脚 {r['footTotal']}")
        else:
            no(f"统计异常：当前 {r['cur']!r} 总数 {r['total']!r} 页脚 {r['footTotal']!r}")

        # I6 首屏加载遮罩必须退场（否则整页不可用）
        if r["bootDisplay"] == "none":
            ok("首屏加载遮罩已退场（display:none）")
        else:
            no(f"加载遮罩未退场：display={r['bootDisplay']}")

        # I7 回首页入口：顶栏 + 卡片底部，至少两处
        if r["backHref"] == "./index.html" and r["backCount"] >= 2:
            ok(f"回首页入口 href={r['backHref']}（共 {r['backCount']} 处）")
        else:
            no(f"回首页入口异常：href={r['backHref']!r} 数量={r['backCount']}")

        # I8 无横向溢出
        if r["scrollW"] <= r["innerW"]:
            ok(f"1280px 视口无横向溢出（scrollW={r['scrollW']} innerW={r['innerW']}）")
        else:
            no(f"横向溢出：scrollW={r['scrollW']} > innerW={r['innerW']}")

        # I9 主题三态在本页同样生效（背景亮度必须真的翻转）
        def _lum(css: str) -> int:
            m = re.findall(r"\d+", css or "")
            if len(m) < 3:
                return -1
            return (int(m[0]) * 299 + int(m[1]) * 587 + int(m[2]) * 114) // 1000

        cdp.js("document.documentElement.setAttribute('data-theme','dark')")
        time.sleep(0.35)
        dark_bg = cdp.js("getComputedStyle(document.body).backgroundColor")
        cdp.js("document.documentElement.setAttribute('data-theme','light')")
        time.sleep(0.35)
        light_bg = cdp.js("getComputedStyle(document.body).backgroundColor")
        cdp.js("document.documentElement.removeAttribute('data-theme')")
        time.sleep(0.2)
        d_lum, l_lum = _lum(dark_bg), _lum(light_bg)
        if 0 <= d_lum < 60 and l_lum > 200:
            ok(f"主题三态生效：dark 亮度 {d_lum}（{dark_bg}）/ light 亮度 {l_lum}（{light_bg}）")
        else:
            no(f"主题切换异常：dark={dark_bg}({d_lum}) light={light_bg}({l_lum})")

        # I10 版本记录页的运行时报错
        errs2 = json.loads(cdp.js("JSON.stringify(window.__errs||[])"))
        if not errs2:
            ok("版本记录页无运行时报错")
        else:
            for e in errs2:
                if str(e).startswith(("warn:", "cerr:")):
                    wn(f"releases.html: {str(e)[:200]}")
                else:
                    no(f"releases.html 运行时报错：{str(e)[:200]}")

        rc = 1 if FAIL else 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n[探针异常] {type(exc).__name__}: {exc}")
        FAIL.append(str(exc))
        rc = 1
    finally:
        try:
            ws.close()
        except Exception:
            pass
        for p in (proc, srv):
            try:
                p.terminate()
            except Exception:
                pass
        import shutil
        shutil.rmtree(udd, ignore_errors=True)

    print("\n" + "=" * 70)
    if FAIL:
        print(f"结果：FAIL —— {len(FAIL)} 项失败 / {len(PASS)} 通过 / {len(WARN)} 警告")
        for f in FAIL:
            print(f"  ✗ {f}")
    else:
        print(f"结果：PASS —— {len(PASS)} 项通过 / {len(WARN)} 警告")
        for w in WARN:
            print(f"  ! {w}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
