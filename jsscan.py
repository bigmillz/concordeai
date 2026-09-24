"""jsscan: names the page script uses but never declares (dev tool, not shipped).

The page's JavaScript lives inside HTML_CONTENT in millenai.py, and a name
it never declares throws a ReferenceError only when that line runs. That
is how `perf` threw every 1.5 s for months (perf mode became Visual
effects in 6b292 and one reader was missed) and how 6b310's Contribute
removal left the Zito board reading a deleted `peers`.

Scope-insensitive on purpose: it tokenises the script (comments, strings,
template literals with ${}, regex literals), collects every binding made
anywhere (function and class names, let/const/var targets including
destructuring, parameters of functions, methods and arrows, catch
bindings), and reports every identifier used as a value that is none of
those and not a keyword. Over-collecting declarations can hide a bug; it
never invents one. The caller filters out the host's globals.

    python3 jsscan.py          # scans millenai.py's page
"""
import re
import sys

KEYWORDS = set("""break case catch class const continue debugger default delete do
else export extends finally for function if import in instanceof let new of return
super switch this throw try typeof var void while with yield async await static get
set true false null undefined arguments NaN Infinity eval""".split())
PUNCT_BEFORE_REGEX = set("( , = : [ ! & | ? { } ; + - * % < > ~ ^ => && || ?? == === != !== <= >= += -= *= /= %= **".split())
KW_BEFORE_REGEX = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw", "instanceof", "yield", "await"}


def tokenize(src):
    toks, i, n = [], 0, len(src)
    ident = re.compile(r"[A-Za-z_$][\w$]*")
    num = re.compile(r"(?:0[xXoObB][\da-fA-F_]+|\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)n?")
    puncts = sorted("=> === !== **= ... ?. ?? && || == != <= >= += -= *= /= %= ++ -- ** << >> ( ) [ ] { } ; , < > + - * / % & | ^ ! ~ ? : = .".split(), key=len, reverse=True)
    stack = []  # template nesting: count of open braces inside ${}

    def prev_sig():
        return toks[-1] if toks else ("p", ";")

    def read_template(i):
        # at a backtick or after a closing } of ${}: read chars to ` or ${
        while i < n:
            c = src[i]
            if c == "\\":
                i += 2
                continue
            if c == "`":
                return i + 1, False
            if c == "$" and i + 1 < n and src[i + 1] == "{":
                return i + 2, True
            i += 1
        return i, False

    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            toks.append(("s", ""))
            i = j + 1
            continue
        if c == "`":
            i, opened = read_template(i + 1)
            toks.append(("s", ""))
            if opened:
                stack.append(0)
                toks.append(("p", "("))
            continue
        if c == "}" and stack and stack[-1] == 0:
            stack.pop()
            toks.append(("p", ")"))
            i, opened = read_template(i + 1)
            if opened:
                stack.append(0)
                toks.append(("p", "("))
            continue
        if c == "/":
            k, v = prev_sig()
            is_regex = (k == "p" and v in PUNCT_BEFORE_REGEX) or (k == "i" and v in KW_BEFORE_REGEX)
            if is_regex:
                j, cls = i + 1, False
                while j < n:
                    d = src[j]
                    if d == "\\":
                        j += 2
                        continue
                    if d == "[":
                        cls = True
                    elif d == "]":
                        cls = False
                    elif d == "/" and not cls:
                        break
                    elif d == "\n":
                        break
                    j += 1
                j += 1
                while j < n and (src[j].isalpha()):
                    j += 1
                toks.append(("r", ""))
                i = j
                continue
        m = ident.match(src, i)
        if m:
            toks.append(("i", m.group(0)))
            i = m.end()
            continue
        m = num.match(src, i)
        if m and m.end() > i:
            toks.append(("n", m.group(0)))
            i = m.end()
            continue
        for p in puncts:
            if src.startswith(p, i):
                if p == "{" and stack:
                    stack[-1] += 1
                if p == "}" and stack:
                    stack[-1] -= 1
                toks.append(("p", p))
                i += len(p)
                break
        else:
            i += 1
    return toks


def match_close(toks, i):
    """toks[i] is an opener; return the index of its closer."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    o = toks[i][1]
    c = pairs[o]
    d = 0
    for j in range(i, len(toks)):
        if toks[j] == ("p", o):
            d += 1
        elif toks[j] == ("p", c):
            d -= 1
            if d == 0:
                return j
    return len(toks) - 1


def match_open(toks, i):
    pairs = {")": "(", "]": "[", "}": "{"}
    c = toks[i][1]
    o = pairs[c]
    d = 0
    for j in range(i, -1, -1):
        if toks[j] == ("p", c):
            d += 1
        elif toks[j] == ("p", o):
            d -= 1
            if d == 0:
                return j
    return 0


def idents_in(toks, a, b):
    return {v for k, v in toks[a:b + 1] if k == "i"}


def analyse(src):
    toks = tokenize(src)
    declared, used = set(), {}
    n = len(toks)
    for i, (k, v) in enumerate(toks):
        if k != "i":
            continue
        nxt = toks[i + 1] if i + 1 < n else ("p", ";")
        prv = toks[i - 1] if i else ("p", ";")
        if v in ("function", "class"):
            if nxt[0] == "i":
                declared.add(nxt[1])
            # parameters
            j = i + 1 + (1 if nxt[0] == "i" else 0)
            if j < n and toks[j] == ("p", "("):
                declared |= idents_in(toks, j, match_close(toks, j))
            continue
        if v in ("let", "const", "var"):
            j = i + 1
            while j < n:
                t = toks[j]
                if t[0] == "i":
                    declared.add(t[1])
                    j += 1
                elif t in (("p", "{"), ("p", "[")):
                    e = match_close(toks, j)
                    declared |= idents_in(toks, j, e)
                    j = e + 1
                else:
                    break
                # skip an initialiser to the next top-level comma
                if j < n and toks[j] == ("p", "="):
                    d = 0
                    j += 1
                    while j < n:
                        t = toks[j]
                        if t[0] == "p" and t[1] in "([{":
                            d += 1
                        elif t[0] == "p" and t[1] in ")]}":
                            if d == 0:
                                break
                            d -= 1
                        elif d == 0 and t in (("p", ","), ("p", ";")):
                            break
                        elif d == 0 and t[0] == "i" and t[1] in ("of", "in"):
                            break
                        j += 1
                if j < n and toks[j] == ("p", ","):
                    j += 1
                    continue
                break
            continue
        if v == "catch" and nxt == ("p", "("):
            declared |= idents_in(toks, i + 1, match_close(toks, i + 1))
            continue
        if v in KEYWORDS:
            continue
        # arrow parameter: x=>
        if nxt == ("p", "=>"):
            declared.add(v)
            continue
        if prv in (("p", "."), ("p", "?.")):
            continue                               # a property
        if nxt == ("p", ":") and prv in (("p", "{"), ("p", ",")):
            continue                               # an object key
        if nxt == ("p", "(") and prv in (("p", "{"), ("p", ","), ("p", "}"), ("p", ";")) \
                or (prv[0] == "i" and prv[1] in ("get", "set", "static", "async")):
            e = match_close(toks, i + 1)
            if e + 1 < n and toks[e + 1] == ("p", "{") and prv[1] not in (";", "}") :
                declared |= idents_in(toks, i + 1, e)  # method params
                continue                               # a method name
        used.setdefault(v, 0)
        used[v] += 1
    # arrow functions with a parenthesised parameter list
    for i, t in enumerate(toks):
        if t == ("p", "=>") and i and toks[i - 1] == ("p", ")"):
            a = match_open(toks, i - 1)
            declared |= idents_in(toks, a, i - 1)
    return declared, used


def page_script(millenai_src):
    """Every <script> in millenai.py's HTML_CONTENT, joined."""
    a = millenai_src.index('HTML_CONTENT = r"""')
    b = millenai_src.index('"""', a + 20)
    page = millenai_src[a:b]
    return "\n;\n".join(
        re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", page, re.S))


def undeclared(js):
    """{name: uses} for names used but declared nowhere (host globals
    included: the caller filters those)."""
    declared, used = analyse(js)
    return {k: v for k, v in used.items()
            if k not in declared and k not in KEYWORDS}


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "millenai.py"
    for name, uses in sorted(undeclared(page_script(open(path).read())).items()):
        print(name, uses)
