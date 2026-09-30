#!/usr/bin/env python3
"""静态校验：JSON / 内联 JS 语法 / DOM id 闭环 / 资源路径存在。

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


# ---------------------------------------------------------------- 1. JSON
def check_json() -> None:
    print("\n== 1. JSON 可解析性 ==")
    for name in ("data.json", "manifest.json"):
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
    with open(os.path.join(ROOT, "data.json"), "r", encoding="utf-8") as fh:
        data = json.load(fh)
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


# --------------------------------------------------------- 2. 内联 JS 语法
def check_inline_js() -> None:
    print("\n== 2. 内联 JS 语法（node --check） ==")
    with open(os.path.join(ROOT, "index.html"), "r", encoding="utf-8") as fh:
        html = fh.read()

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
        fail("未找到内联 <script> 块")
        return
    tmpdir = tempfile.mkdtemp(prefix="copper-js-")
    for i, code in enumerate(blocks, 1):
        if not code.strip():
            continue
        # type="application/json" 之类的非 JS 块跳过
        path = os.path.join(tmpdir, f"block{i}.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(code)
        proc = subprocess.run(
            [NODE, "--check", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            fail(f"第 {i} 个内联 JS 块语法错误：\n{proc.stderr.strip()[:900]}")
        else:
            ok(f"第 {i} 个内联 JS 块语法通过（{len(code):,} 字符）")
    print(f"  共 {len(blocks)} 个内联 script 块")


# ------------------------------------------------------- 3. DOM id 引用闭环
def check_dom_ids() -> None:
    print("\n== 3. DOM id 引用闭环 ==")
    with open(os.path.join(ROOT, "index.html"), "r", encoding="utf-8") as fh:
        html = fh.read()

    # 声明的 id
    declared = set(re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', html))
    # 所有内联 JS 内被引用的 id
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
            fail(f"JS 引用了不存在的 id: #{mid}")
    else:
        ok(f"JS 引用的 {len(refs)} 个 id 全部在 DOM 中存在")
    if unused:
        warn(f"{len(unused)} 个 id 声明但 JS 未引用（可能仅 CSS 用）: {', '.join(unused[:20])}")
    else:
        ok("无孤立 id")

    # 关键 id 必须存在（需求 P-1/P-2/P-3）
    required = [
        "counterFutures", "counterSpot", "counterLme",
        "statDod", "statWow", "statYtd", "statYoy",
        "mainChart", "rangeGroup",
        "ratioChart", "basisChart",
        "convInput", "cvKg", "cvJin", "cvG", "cvWan",
        "tableBody", "srcGrid",
    ]
    absent = [r for r in required if r not in declared]
    if absent:
        fail(f"关键 id 缺失: {', '.join(absent)}")
    else:
        ok(f"需求要求的关键 id 全部到位（{len(required)} 个）")


# --------------------------------------------------------- 4. 资源路径存在
def check_assets() -> None:
    print("\n== 4. 资源路径存在性 ==")
    with open(os.path.join(ROOT, "index.html"), "r", encoding="utf-8") as fh:
        html = fh.read()

    refs: set[str] = set()
    refs |= set(re.findall(r'<link[^>]+href\s*=\s*["\']([^"\']+)["\']', html, re.I))
    refs |= set(re.findall(r'<script[^>]+src\s*=\s*["\']([^"\']+)["\']', html, re.I))
    refs |= set(re.findall(r'<img[^>]+src\s*=\s*["\']([^"\']+)["\']', html, re.I))
    refs |= set(re.findall(r'["\'](\.?\.?/[\w./-]+\.(?:js|css|png|svg|json|ico|webp))["\']', html, re.I))

    local = []
    for r in refs:
        if r.startswith(("http://", "https://", "//", "data:", "#", "mailto:")):
            continue
        local.append(r)
    if not local:
        warn("未发现本地资源引用")
        return
    for r in sorted(set(local)):
        p = os.path.normpath(os.path.join(ROOT, r.lstrip("./")))
        if os.path.exists(p):
            ok(f"{r} → 存在")
        else:
            fail(f"{r} → 文件不存在")

    # manifest / sw 引用
    for name in ("manifest.json", "sw.js"):
        if os.path.exists(os.path.join(ROOT, name)):
            ok(f"{name} 存在")
        else:
            fail(f"{name} 缺失")
    # manifest 内图标
    with open(os.path.join(ROOT, "manifest.json"), "r", encoding="utf-8") as fh:
        mani = json.load(fh)
    for icon in mani.get("icons", []):
        src = icon.get("src", "").lstrip("./")
        if os.path.exists(os.path.join(ROOT, src)):
            ok(f"manifest icon {src} 存在")
        else:
            fail(f"manifest icon {src} 不存在")
    # sw.js CORE_ASSETS
    with open(os.path.join(ROOT, "sw.js"), "r", encoding="utf-8") as fh:
        sw = fh.read()
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


def main() -> int:
    print("=" * 68)
    print("copper-price-tracker 静态校验")
    print("=" * 68)
    check_json()
    check_inline_js()
    check_dom_ids()
    check_assets()
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
