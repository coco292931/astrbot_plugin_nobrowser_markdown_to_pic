"""
pillowmd 运行时自愈补丁。

pillowmd <= 0.7.3 存在一个公式渲染 BUG：当某个行内/行间公式渲染出的子图
数量恰好等于它在源码中占用的字符数时（如 "x"、"y=x" 这类简单公式），
绘制阶段的 `nowlatexImageIdx >= len(images)` 分支永不触发，导致
`del latexs[0]` 不执行、过期条目滞留在 latexs[0]，其后所有公式都会
匹配不到绘制窗口、退化为字面文本。

本模块在 import pillowmd 之前就地修补已安装的源码（幂等、可安全重复调用）。
上游修复见 fork：CustomMarkdownImage fix/latex-formula-poisoning。

设计原则：
- 必须在 import pillowmd 之前调用，首次运行即生效、无需重启
- 幂等：靠 sentinel 标记，已打过补丁则直接跳过
- 安全：找不到文件 / 不可写 / 匹配不到目标 / 已是修复版，一律静默跳过，
  绝不抛异常影响插件启动
"""

import importlib.util

SENTINEL = "# __PATCHED_LATEX_POISONING__"

# 待匹配的原始锚点（绘制循环开头）
_ANCHOR = (
    '        islatex = False\n'
    '\n'
    '        if latexs and latexs[0]["begin"]< idx <latexs[0]["end"]:'
)

# 注入后的内容
_REPLACEMENT = (
    '        islatex = False\n'
    '\n'
    f'        {SENTINEL}\n'
    '        # 丢弃已走过的 latex 条目：当公式"子图数==源码字符数"时，\n'
    '        # `>= len(images)` 分支永不触发、del latexs[0] 不执行，\n'
    '        # 过期条目会毒化其后所有公式，使之退化为字面文本。\n'
    '        while latexs and idx >= latexs[0]["end"]:\n'
    '            del latexs[0]\n'
    '            nowlatexImageIdx = -1\n'
    '\n'
    '        if latexs and latexs[0]["begin"]< idx <latexs[0]["end"]:'
)


def _find_renderer_path():
    """定位 pillowmd 的 CustomMarkdownRenderer.py，不触发模块执行。"""
    import os

    spec = importlib.util.find_spec("pillowmd")
    if spec is None or not spec.submodule_search_locations:
        return None
    for base in spec.submodule_search_locations:
        candidate = os.path.join(base, "CustomMarkdownRenderer.py")
        if os.path.isfile(candidate):
            return candidate
    return None


def apply_patch(logger=None):
    """就地修补已安装的 pillowmd。必须在 import pillowmd 之前调用。

    返回值（仅用于日志/测试）：
        "patched"        本次成功打补丁
        "already"        已打过补丁，跳过
        "not_found"      未找到 pillowmd 源文件
        "anchor_missing" 源码与预期不符（可能已被上游修复或版本变动），跳过
        "error:<msg>"    写入失败等异常，已安全跳过
    """
    def _log(msg):
        if logger is not None:
            try:
                logger.info(msg)
            except Exception:
                pass

    path = _find_renderer_path()
    if not path:
        _log("pillowmd 补丁：未找到源文件，跳过")
        return "not_found"

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        _log(f"pillowmd 补丁：读取失败，跳过（{e}）")
        return f"error:{e}"

    if SENTINEL in content:
        return "already"

    if _ANCHOR not in content:
        # 源码与预期锚点不符：可能上游已修复或版本不同，安全跳过
        _log("pillowmd 补丁：未匹配到锚点（可能已是修复版本），跳过")
        return "anchor_missing"

    patched = content.replace(_ANCHOR, _REPLACEMENT, 1)
    try:
        import os
        import tempfile

        dir_name = os.path.dirname(path)
        tmp_path = None
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=dir_name) as tf:
            tf.write(patched)
            tmp_path = tf.name
        os.replace(tmp_path, path)
    except Exception as e:
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        _log(f"pillowmd 补丁：写入失败，跳过（{e}）")
        return f"error:{e}"

    _log("pillowmd 补丁：已修复公式渲染 BUG（latex-formula-poisoning）")
    return "patched"


# ---------------------------------------------------------------------------
# pillowlatex 补丁：补全箭头/花体/间距命令，并修正 Unicode 命令名边界。
#
# pillowlatex <= 0.1.4 的缺陷：
#   1. \leftarrow / \rightarrow 未注册 -> 退化为字面文本
#   2. \mathcal \mathscr \mathbb \mathfrak \mathbf 等花体命令未实现 / 被吞
#   3. \, \: \; \! \quad \qquad 等间距命令未实现 -> 符号挤在一起
#   4. 分词用 str.isalpha() 判断命令名边界，Unicode 数学字母（ℝ 等）也是
#      alpha，导致 "\rightarrowℝ" 紧贴写法整体识别失败
#
# 修复策略（在 import pillowmd 之后、首次渲染之前调用 apply_latex_patch）：
#   - 命令缺失（1/2/3）走内存级 monkey-patch：补 replaces 字典 + 包装
#     GetLaTeXObjs 做花体/间距预处理。无需改源文件，安全可逆。
#   - 边界 bug（4）在 GetLaTeXObjs 内部，无法靠包装绕过，走单行文本补丁。
# 上游修复见 fork：pillowlatex fix/missing-latex-commands。
# ---------------------------------------------------------------------------

LATEX_SENTINEL = "# __PATCHED_CMD_COVERAGE__"

# 数学字母变体 Unicode 映射（letterlike 特例 + 基准码位）
_MA_EXC = {
    "script": {"B": 0x212C, "E": 0x2130, "F": 0x2131, "H": 0x210B, "I": 0x2110,
               "L": 0x2112, "M": 0x2133, "R": 0x211B, "e": 0x212F, "g": 0x210A,
               "o": 0x2134},
    "bb": {"C": 0x2102, "H": 0x210D, "N": 0x2115, "P": 0x2119, "Q": 0x211A,
           "R": 0x211D, "Z": 0x2124},
    "frak": {"C": 0x212D, "H": 0x210C, "I": 0x2111, "R": 0x211C, "Z": 0x2128},
}
_MA_BASE = {"cal": (0x1D49C, 0x1D4B6, "script"), "scr": (0x1D49C, 0x1D4B6, "script"),
            "bb": (0x1D538, 0x1D552, "bb"), "frak": (0x1D504, 0x1D51E, "frak"),
            "bf": (0x1D400, 0x1D41A, None), "sf": (0x1D5A0, 0x1D5BA, None),
            "tt": (0x1D670, 0x1D68A, None)}
_MA_CMD = {"mathcal": "cal", "mathscr": "scr", "mathbb": "bb", "mathfrak": "frak",
           "mathbf": "bf", "boldsymbol": "bf", "mathsf": "sf", "mathtt": "tt"}
_SP_SYMBOL = {",": " ", ":": " ", ";": " ", " ": " ", "!": ""}
_SP_NAMED = {"quad": " ", "qquad": "  ", "thinspace": " ",
             "medspace": " ", "thickspace": " ", "enspace": " ",
             "negthinspace": ""}
_ARROW_FIX = {"leftarrow": "←", "rightarrow": "→"}


def _to_math_alpha(ch, variant):
    base = _MA_BASE.get(variant)
    if base is None:
        return ch
    up, low, exc_key = base
    exc = _MA_EXC.get(exc_key, {}) if exc_key else {}
    if ch in exc:
        return chr(exc[ch])
    if "A" <= ch <= "Z":
        return chr(up + (ord(ch) - 65))
    if "a" <= ch <= "z":
        return chr(low + (ord(ch) - 97))
    return ch


def _preprocess(string):
    """展开花体与间距命令；保留 \\ 换行。与上游 fork 的预处理逻辑等价。"""
    if "\\" not in string:
        return string
    out = []
    i, n = 0, len(string)
    while i < n:
        if string[i] == "\\":
            if i + 1 < n and string[i + 1] == "\\":
                out.append("\\\\")
                i += 2
                continue
            if i + 1 < n and string[i + 1] in _SP_SYMBOL:
                out.append(_SP_SYMBOL[string[i + 1]])
                i += 2
                continue
            j = i + 1
            # 命令名仅取 ASCII 字母：str.isalpha() 会把 Unicode 数学字母（如 ℝ）
            # 也算进命令名，与本补丁试图修复的边界 bug 同源。
            while j < n and (("a" <= string[j] <= "z") or ("A" <= string[j] <= "Z")):
                j += 1
            cmd = string[i + 1:j]
            if cmd in _SP_NAMED:
                out.append(_SP_NAMED[cmd])
                i = j
                continue
            variant = _MA_CMD.get(cmd)
            if variant is not None:
                k = j
                while k < n and string[k] == " ":
                    k += 1
                if k < n and string[k] == "{":
                    depth, m = 1, k + 1
                    while m < n and depth:
                        if string[m] == "{":
                            depth += 1
                        elif string[m] == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        m += 1
                    if depth != 0:
                        # 花括号未闭合：回退为原样输出，避免静默吞掉剩余内容
                        out.append(string[i:])
                        i = n
                        continue
                    out.append("".join(_to_math_alpha(c, variant) for c in string[k + 1:m]))
                    i = m + 1
                    continue
                elif k < n and (("a" <= string[k] <= "z") or ("A" <= string[k] <= "Z")):
                    out.append(_to_math_alpha(string[k], variant))
                    i = k + 1
                    continue
        out.append(string[i])
        i += 1
    return "".join(out)


def _patch_latex_boundary_in_file(logger_fn):
    """单行文本补丁：把命令名边界判断从 str.isalpha() 改为 ASCII 字母。

    这是唯一无法靠 monkey-patch 绕过的部分（在 GetLaTeXObjs 内部）。
    """
    import os

    spec = importlib.util.find_spec("pillowlatex")
    if spec is None or not spec.submodule_search_locations:
        return
    path = None
    for base in spec.submodule_search_locations:
        cand = os.path.join(base, "latex.py")
        if os.path.isfile(cand):
            path = cand
            break
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return
    if LATEX_SENTINEL in content:
        return
    anchor = "            while end_idx < sz and (string[end_idx].isalpha() or (string[end_idx] in ex_replaces and end_idx == idx + 1)):"
    if anchor not in content:
        return
    helper = (
        "def _pmd_is_ascii_alpha(ch):\n"
        "    return ('a' <= ch <= 'z') or ('A' <= ch <= 'Z')\n"
        "\n"
        "def GetLaTeXObjs("
    )
    new_content = content
    # 注入 ASCII 判断辅助函数（放在 GetLaTeXObjs 定义前）
    new_content = new_content.replace("def GetLaTeXObjs(", helper, 1)
    # 替换两处边界判断
    new_content = new_content.replace(
        "string[end_idx].isalpha()", "_pmd_is_ascii_alpha(string[end_idx])"
    ).replace(
        "string[end_idx-1].isalpha()", "_pmd_is_ascii_alpha(string[end_idx-1])"
    )
    new_content = LATEX_SENTINEL + "\n" + new_content
    try:
        import tempfile
        dir_name = os.path.dirname(path)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=dir_name) as tf:
            tf.write(new_content)
            tmp = tf.name
        os.replace(tmp, path)
        logger_fn("pillowlatex 补丁：已修正 Unicode 命令名边界")
    except Exception as e:
        logger_fn(f"pillowlatex 边界补丁写入失败，跳过（{e}）")


def apply_latex_patch(logger=None):
    """修补已 import 的 pillowlatex。须在 import pillowmd/pillowlatex 之后调用。

    箭头/花体/间距三类命令通过内存级 monkey-patch 修复，首次调用即生效。
    Unicode 命令名边界 bug 在 GetLaTeXObjs 内部、无法靠包装绕过，改为写入
    源文件修复——故对“命令紧贴 Unicode 花体字符且中间无空格”（如
    "\\rightarrowℝ"）这一少见写法，需进程重启后才完全生效；其余写法当次即可。

    返回 "patched" / "already" / "not_found" / "error:<msg>"。
    """
    def _log(msg):
        if logger is not None:
            try:
                logger.info(msg)
            except Exception:
                pass

    try:
        import pillowlatex
    except Exception as e:
        _log(f"pillowlatex 补丁：导入失败，跳过（{e}）")
        return f"error:{e}"

    if getattr(pillowlatex, "_pmd_cmd_patched", False):
        return "already"

    try:
        # 1) 补箭头键（直接改活字典）
        if hasattr(pillowlatex, "replaces") and isinstance(pillowlatex.replaces, dict):
            for k, v in _ARROW_FIX.items():
                pillowlatex.replaces.setdefault(k, v)

        # 2) 包装 GetLaTeXObjs：先做花体/间距预处理
        _orig = pillowlatex.GetLaTeXObjs

        def _wrapped(string, *a, **kw):
            return _orig(_preprocess(string), *a, **kw)

        pillowlatex.GetLaTeXObjs = _wrapped
        pillowlatex._pmd_cmd_patched = True
    except Exception as e:
        _log(f"pillowlatex 补丁：monkey-patch 失败，跳过（{e}）")
        return f"error:{e}"

    # 3) 边界 bug：单行文本补丁（下次进程生效；当前进程已被包装缓解大部分场景）
    _patch_latex_boundary_in_file(_log)

    _log("pillowlatex 补丁：已补全箭头/花体/间距命令")
    return "patched"


