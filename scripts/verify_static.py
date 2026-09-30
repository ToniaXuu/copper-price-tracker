#!/usr/bin/env python3
"""静态校验：JSON / 内联 JS 语法 / DOM id 闭环 / 资源路径存在 / 两页令牌一致性。

本机 Git Bash 的 ls/head/grep 不可靠，所有检查一律用 Python 完成。
用法：
    python scripts/verify_static.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = r"C:\Users\Tonia\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"

# 站点页面（都做内联 JS / DOM id / 资源路径检查）
PAGES = ("index.html", "releases.html")

# 各页必须存在的关键 id（需求 P-x 与页面功能锚点）
REQUIRED_IDS = {
    "index.html": [
        "counterFutures", "counterSpot", "counterLme",
        "statDod", "statWow", "statYtd", "statYoy",
        "mainChart", "rangeGroup",
        "ratioChart", "basisChart",
        "convInput", "cvKg", "cvJin", "cvG", "cvWan",
        "tableBody", "srcGrid",
    ],
    "releases.html": [
        "releaseTimeline", "relTag", "relCurrent", "relTotal", "relLatest", "relSince",
        "themeSwitch", "bootScreen", "backTop",
    ],
}

FAILURES: list[str] = []
WARNINGS: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  [FAIL] {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  [WARN] {msg}")


def ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- 1. JSON
def check_json() -> None:
    print("\n== 1. JSON 可解析性 ==")
    for name in ("data.json", "manifest.json", "releases.json"):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            fail(f"{name} 不存在")
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            fail(f"{name} 解析失败: {exc}")
            continue
        size = os.path.getsize(path)
        ok(f"{name} 解析通过（{size:,} 字节 / 顶层键 {list(obj)[:8]}）")

    # data.json 结构与关键字段
    data = json.loads(read(os.path.join(ROOT, "data.json")))
    for key in ("meta", "futures", "spot", "lme", "sourceHealth"):
        if key not in data:
            fail(f"data.json 缺少顶层键 {key}")
    meta = data.get("meta", {})
    if meta.get("changeBasis") != "settle":
        fail(f"meta.changeBasis 应为 'settle'，实际 {meta.get('changeBasis')!r}")
    else:
        ok("meta.changeBasis == 'settle'（涨跌口径自证）")
    fut = data.get("futures", [])
    if not fut:
        fail("futures 为空")
    else:
        last = fut[-1]
        ok(f"futures {len(fut)} 条，最新 {last.get('date')} settle={last.get('settle')} change={last.get('change')}")
        # change 必须与结算价差值一致
        if len(fut) >= 2:
            prev = fut[-2]
            base = lambda r: r.get("settle") if r.get("settle") is not None else r.get("close")  # noqa: E731
            expect = round(base(last) - base(prev), 2)
            actual = last.get("change")
            if actual is None or abs(actual - expect) > 0.02:
                fail(f"最新 change={actual} 与结算价差值 {expect} 不符（口径写歪了）")
            else:
                ok(f"最新 change={actual} == 结算价差值 {expect}（口径一致）")
    for seg in ("spot", "lme"):
        ok(f"{seg} {len(data.get(seg, []))} 条")

    # releases.json：版本记录结构与「同日可合并」的前提
    rel = json.loads(read(os.path.join(ROOT, "releases.json")))
    if "meta" not in rel or "releases" not in rel:
        fail("releases.json 缺少 meta / releases")
        return
    items = rel["releases"]
    if not isinstance(items, list) or not items:
        fail("releases.json 的 releases 为空")
        return
    ok(f"releases {len(items)} 条，当前版本 v{rel['meta'].get('current')}")

    bad: list[str] = []
    for r in items:
        ver = r.get("version", "?")
        if not re.fullmatch(r"\d+\.\d+\.\d+", str(ver)):
            bad.append(f"v{ver} 版本号非 semver")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(r.get("date", ""))):
            bad.append(f"v{ver} date 非 YYYY-MM-DD")
        # 时刻必须精确到分钟 —— 页面要显示 HH:MM，缺了就只剩占位符
        if not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", str(r.get("time", ""))):
            bad.append(f"v{ver} time 非 HH:MM")
        if r.get("type") not in ("feat", "fix", "perf", "docs", "data"):
            bad.append(f"v{ver} type 非法: {r.get('type')!r}")
        if not isinstance(r.get("items"), list) or not r["items"]:
            bad.append(f"v{ver} items 为空")
    if bad:
        for b in bad:
            fail(f"releases.json: {b}")
    else:
        ok("每条版本的 version / date / time / type / items 均合法")

    # 同日多版本：页面「合并同日节点」要有真实数据可撑
    days: dict[str, list[str]] = {}
    for r in items:
        days.setdefault(str(r.get("date")), []).append(str(r.get("time")))
    merged = {d: t for d, t in days.items() if len(t) > 1}
    if merged:
        for d, ts in sorted(merged.items()):
            ok(f"{d} 有 {len(ts)} 个版本（{'、'.join(sorted(ts, reverse=True))}）→ 页面会合并为一个节点")
    else:
        warn("没有任何日期包含多个版本，「同日合并」在页面上看不出来")

    # 每个 version 不得重复（同日多版本靠时间区分，版本号仍须唯一）
    vers = [str(r.get("version")) for r in items]
    dup = sorted({v for v in vers if vers.count(v) > 1})
    if dup:
        fail(f"版本号重复: {', '.join(dup)}")
    else:
        ok("版本号唯一")


# --------------------------------------------------------- 2. 内联 JS 语法
def check_inline_js() -> None:
    print("\n== 2. 内联 JS 语法（node --check） ==")
    tmpdir = tempfile.mkdtemp(prefix="copper-js-")
    total = 0
    checked = 0
    for page in PAGES:
        path = os.path.join(ROOT, page)
        if not os.path.exists(path):
            fail(f"{page} 不存在")
            continue
        html = read(path)

        # 只取真正的 JS 块：排除 src=、排除 type="application/ld+json" 等数据块
        blocks = []
        for m in re.finditer(r"<script([^>]*)>(.*?)</script>", html, re.S | re.I):
            attrs, code = m.group(1), m.group(2)
            if re.search(r"\bsrc\s*=", attrs, re.I):
                continue
            t = re.search(r'\btype\s*=\s*["\']([^"\']+)["\']', attrs, re.I)
            if t and t.group(1).lower() not in ("text/javascript", "module", "application/javascript"):
                continue
            blocks.append(code)
        if not blocks:
            fail(f"{page} 未找到内联 <script> 块")
            continue
        total += len(blocks)
        for i, code in enumerate(blocks, 1):
            if not code.strip():
                continue
            js_path = os.path.join(tmpdir, f"{page.replace('.', '_')}_block{i}.js")
            with open(js_path, "w", encoding="utf-8") as fh:
                fh.write(code)
            proc = subprocess.run(
                [NODE, "--check", js_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode != 0:
                fail(f"{page} 第 {i} 个内联 JS 块语法错误：\n{proc.stderr.strip()[:900]}")
            else:
                checked += 1
                ok(f"{page} 第 {i} 个内联 JS 块语法通过（{len(code):,} 字符）")
    print(f"  共 {len(PAGES)} 个页面 / {total} 个内联 script 块（{checked} 个通过）")


# ------------------------------------------------------- 3. DOM id 引用闭环
def check_dom_ids() -> None:
    print("\n== 3. DOM id 引用闭环 ==")
    for page in PAGES:
        path = os.path.join(ROOT, page)
        if not os.path.exists(path):
            fail(f"{page} 不存在")
            continue
        html = read(path)
        print(f"  -- {page} --")

        declared = set(re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', html))
        scripts = "\n".join(
            m.group(1)
            for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)
        )
        refs: set[str] = set()
        for pat in (
            r'\$\(\s*["\']#([A-Za-z0-9_-]+)["\']',
            r'getElementById\(\s*["\']([A-Za-z0-9_-]+)["\']',
            r'querySelector\(\s*["\']#([A-Za-z0-9_-]+)["\']',
        ):
            refs |= set(re.findall(pat, scripts))
        # 部分代码通过字符串参数传 id，例如 set('cvKg', ...) / setCounter('counterFutures', ...)
        # 这类引用靠「任意字符串字面量命中已声明 id」来识别
        lits = set(re.findall(r"""["']([A-Za-z0-9_-]+)["']""", scripts))
        refs |= (lits & declared)

        missing = sorted({r for r in refs} - declared)
        unused = sorted(declared - refs)
        if missing:
            for mid in missing:
                fail(f"{page}: JS 引用了不存在的 id: #{mid}")
        else:
            ok(f"{page}: JS 引用的 {len(refs)} 个 id 全部在 DOM 中存在")
        if unused:
            warn(f"{page}: {len(unused)} 个 id 声明但 JS 未引用（可能仅 CSS 用）: {', '.join(unused[:20])}")
        else:
            ok(f"{page}: 无孤立 id")

        required = REQUIRED_IDS.get(page, [])
        absent = [r for r in required if r not in declared]
        if absent:
            fail(f"{page}: 关键 id 缺失: {', '.join(absent)}")
        else:
            ok(f"{page}: 关键 id 全部到位（{len(required)} 个）")


# --------------------------------------------------------- 4. 资源路径存在
def check_assets() -> None:
    print("\n== 4. 资源路径存在性 ==")
    for page in PAGES:
        path = os.path.join(ROOT, page)
        if not os.path.exists(path):
            fail(f"{page} 不存在")
            continue
        html = read(path)

        refs: set[str] = set()
        refs |= set(re.findall(r'<link[^>]+href\s*=\s*["\']([^"\']+)["\']', html, re.I))
        refs |= set(re.findall(r'<script[^>]+src\s*=\s*["\']([^"\']+)["\']', html, re.I))
        refs |= set(re.findall(r'<img[^>]+src\s*=\s*["\']([^"\']+)["\']', html, re.I))
        refs |= set(re.findall(r'<a[^>]+href\s*=\s*["\']([^"\']+)["\']', html, re.I))
        refs |= set(re.findall(r'["\'](\.?\.?/[\w./-]+\.(?:js|css|png|svg|json|ico|webp|html))["\']', html, re.I))

        local = []
        for r in refs:
            if r.startswith(("http://", "https://", "//", "data:", "#", "mailto:")):
                continue
            local.append(r)
        if not local:
            warn(f"{page}: 未发现本地资源引用")
            continue
        print(f"  -- {page} --")
        for r in sorted(set(local)):
            p = os.path.normpath(os.path.join(ROOT, r.lstrip("./")))
            if os.path.exists(p):
                ok(f"{r} → 存在")
            else:
                fail(f"{page}: {r} → 文件不存在")

    # manifest / sw 引用（全局）
    print("  -- 全局 --")
    for name in ("manifest.json", "sw.js"):
        if os.path.exists(os.path.join(ROOT, name)):
            ok(f"{name} 存在")
        else:
            fail(f"{name} 缺失")
    mani = json.loads(read(os.path.join(ROOT, "manifest.json")))
    for icon in mani.get("icons", []):
        src = icon.get("src", "").lstrip("./")
        if os.path.exists(os.path.join(ROOT, src)):
            ok(f"manifest icon {src} 存在")
        else:
            fail(f"manifest icon {src} 不存在")
    sw = read(os.path.join(ROOT, "sw.js"))
    m = re.search(r"CORE_ASSETS\s*=\s*\[(.*?)\]", sw, re.S)
    if m:
        assets = re.findall(r'["\']([^"\']+)["\']', m.group(1))
        for a in assets:
            if a.startswith(("http://", "https://", "//")):
                ok(f"sw 预缓存 {a}（外部 CDN，跳过本地检查）")
                continue
            p = os.path.normpath(os.path.join(ROOT, a.lstrip("./")))
            if os.path.exists(p):
                ok(f"sw 预缓存 {a} 存在")
            else:
                fail(f"sw 预缓存 {a} 不存在")
        if any("data.json" in a for a in assets):
            warn("sw 预缓存里含 data.json（1MB，会拖慢首装）")
        else:
            ok("sw 预缓存不含 data.json（正确）")
        # 每个页面都该被预缓存，否则离线访问第二页会失败
        for page in PAGES:
            if any(page in a for a in assets):
                ok(f"sw 预缓存已包含 {page}")
            else:
                fail(f"sw 预缓存缺少 {page}（离线打不开）")


# ------------------------------------------------- 5. 两页设计令牌一致性
def extract_vars(html: str) -> dict[str, set[str]]:
    """只抽 `:root` 令牌层里的声明（明色 + 两处暗色覆盖），同名多值收集成集合。

    刻意不扫全文档：组件局部变量（如 .impact-card 的 --ic-accent）随组件存在，
    不属于两页共享的皮肤，把它们的缺失当成漂移是误报。
    """
    out: dict[str, set[str]] = {}
    for block in re.findall(r":root[^{]*\{([^}]*)\}", html):
        for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;{}]+);", block):
            out.setdefault(m.group(1), set()).add(" ".join(m.group(2).split()))
    return out


def check_token_parity() -> None:
    print("\n== 5. 设计令牌一致性（index.html ↔ releases.html） ==")
    paths = [os.path.join(ROOT, p) for p in PAGES]
    for p in paths:
        if not os.path.exists(p):
            fail(f"{os.path.basename(p)} 不存在，无法比对")
            return
    a, b = (extract_vars(read(p)) for p in paths)
    name_a, name_b = PAGES

    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    if only_a:
        fail(f"{name_a} 独有令牌（{name_b} 缺）: {', '.join(only_a)}")
    if only_b:
        fail(f"{name_b} 独有令牌（{name_a} 缺）: {', '.join(only_b)}")
    if not only_a and not only_b:
        ok(f"令牌名集合一致（{len(a)} 个）")

    diff = [k for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    if diff:
        for k in diff[:12]:
            fail(f"令牌 {k} 取值不一致：{name_a}={sorted(a[k])} / {name_b}={sorted(b[k])}")
        if len(diff) > 12:
            fail(f"...另有 {len(diff) - 12} 个令牌取值不一致")
    else:
        ok("同名令牌取值全部一致（含明色与暗色两套）")


def main() -> int:
    print("=" * 68)
    print("copper-price-tracker 静态校验")
    print("=" * 68)
    check_json()
    check_inline_js()
    check_dom_ids()
    check_assets()
    check_token_parity()
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"结果：FAIL —— {len(FAILURES)} 项失败，{len(WARNINGS)} 项警告")
        for f in FAILURES:
            print(f"  ✗ {f.splitlines()[0]}")
        return 1
    print(f"结果：PASS —— 全部通过（{len(WARNINGS)} 项警告）")
    for w in WARNINGS:
        print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
