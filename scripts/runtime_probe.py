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
        if snap["tableRows"] >= 20:
            ok(f"历史表渲染 {snap['tableRows']} 行")
        else:
            no(f"历史表仅 {snap['tableRows']} 行")
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

        # H3 表格展开 / 收起
        n0 = cdp.js("document.querySelectorAll('#tableBody tr').length")
        cdp.js("document.getElementById('tableMore').click()")
        time.sleep(0.8)
        n1 = cdp.js("document.querySelectorAll('#tableBody tr').length")
        cdp.js("document.getElementById('tableMore').click()")
        time.sleep(0.5)
        n2 = cdp.js("document.querySelectorAll('#tableBody tr').length")
        print(f"  历史表行数：初始 {n0} → 展开 {n1} → 收起 {n2}")
        if n0 == 30 and n1 == 5289 and n2 == 30:
            ok("「显示更多 / 收起」双向正常（30 ↔ 5289）")
        else:
            no(f"表格展开异常：{n0} → {n1} → {n2}")

        # H4 全源失效 → 红色告警（验收标准「全源失效红」）
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
        # 还原
        cdp.js("renderHealth(" + json.dumps({
            k: {"ok": True, "detail": "探针还原"} for k in
            ("sina_kline", "shfe", "spot_100ppi", "lme_kline", "sina_realtime")
        }, ensure_ascii=False) + ")")
        time.sleep(0.3)
        r2 = cdp.js("document.getElementById('healthTag').textContent")
        if "正常" in (r2 or ""):
            ok("健康标签已还原为「全部正常」")
        else:
            wn(f"还原后标签：{r2!r}")

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
