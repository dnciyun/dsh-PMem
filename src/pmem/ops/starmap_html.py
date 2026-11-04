"""星图的离线单文件导出（M9.7b）。

插件页面是实时的（走 ``/dsh-pmem/api/starmap`` + SSE）。但 ``pmem star`` 也该能
产出一份**能直接双击打开**的东西：把渲染器、样式、数据全部塞进一个 HTML，
不联网、不依赖插件、不依赖 DSH。

渲染器和样式是 ``deploy/dsh-pmem/starmap/`` 里那两份真文件——这里只做内联，
不复制一份逻辑，所以插件里看到的和导出的**一定是同一张星图**。

约束：整个页面只能有一个 ``</script>``。数据里的 ``</`` 会被转义成 ``<\\/``，
所以即使记忆原文里写着 ``</script>`` 也提前闭合不了脚本。
"""

from __future__ import annotations

import html as _html
import json
from pathlib import Path
from typing import Any

_SCRIPT_END = "</" + "script>"
_SCRIPT_END_SAFE = "<\\/" + "script>"

# 顺序不能变：space.js 定义 window.PMemStar，ui.js 才能用它。
_ASSETS = ("space.js", "space.css", "ui.js")


def _safe_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("</", "<\\/")


def asset_dir() -> Path | None:
    """找 ``deploy/dsh-pmem/starmap/``：源码树里跑、或者从仓库根跑都行。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "deploy" / "dsh-pmem" / "starmap"
        if (candidate / "space.js").is_file():
            return candidate
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        for parent in (base, *base.parents):
            candidate = parent / "deploy" / "dsh-pmem" / "starmap"
            if (candidate / "space.js").is_file():
                return candidate
    return None


def _shell(title: str, body: str, script: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        '<meta name="color-scheme" content="dark">\n'
        f"<title>{_html.escape(title)}</title>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        f"<script>\n{script}\n</script>\n"
        "</body>\n</html>\n"
    )


def render_html(graph: dict[str, Any]) -> str:
    """把一张星图做成一个可以直接打开的 HTML 文件。"""
    meta = graph.get("meta") or {}
    title = str(meta.get("title") or "PMem · 海马体星图")
    assets = asset_dir()

    if assets is None:
        # 缺资源时不要装作没事：给一张能看懂、能对照的静态页。
        from .starmap_brain import render_svg

        svg = render_svg(graph)
        body = (
            '<div style="font:13px/1.7 system-ui;color:#dbe4f5;background:#05070f;'
            'min-height:100vh;padding:28px">'
            f"<h1 style=\"font-size:20px;margin:0 0 6px\">{_html.escape(title)}</h1>"
            '<p style="color:#7f98ba;margin:0 0 18px">'
            "这里只有静态快照：插件目录里找不到 starmap/space.js，"
            "所以实时渲染器没有被打包进来。用 DSH 设置里的「打开星图」看完整版。</p>"
            f"{svg}</div>"
        )
        return _shell(title, body, "/* 静态快照，无脚本 */")

    css = (assets / "space.css").read_text(encoding="utf-8")
    space_js = (assets / "space.js").read_text(encoding="utf-8")
    ui_js = (assets / "ui.js").read_text(encoding="utf-8")

    for name, source in (("space.js", space_js), ("ui.js", ui_js)):
        if _SCRIPT_END in source:
            raise ValueError(f"{name} 里出现了 {_SCRIPT_END}，内联会提前闭合脚本")

    body = (
        f"<style>\n{css}\n</style>\n"
        '<div id="sm-root" class="sm-shell"></div>\n'
        "<noscript><div style=\"padding:40px;color:#dbe4f5;font:14px system-ui\">"
        "星图需要 JavaScript：它是一张实时绘制的三维星图，不是图片。</div></noscript>"
    )
    script = "\n".join(
        [
            "window.__PMEM_DATA__ = " + _safe_json(graph) + ";",
            space_js,
            ui_js,
        ]
    )
    return _shell(title, body, script)


__all__ = ["render_html", "asset_dir"]
