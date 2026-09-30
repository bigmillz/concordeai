"""Large block letters for the pairing code on the desktop's screen.
5 rows x 5 columns per glyph; covers Crockford base32 and '-'."""

FONT = {
    "0": [" ### ", "#  ##", "# # #", "##  #", " ### "],
    "1": ["  #  ", " ##  ", "  #  ", "  #  ", " ### "],
    "2": [" ### ", "#   #", "  ## ", " #   ", "#####"],
    "3": ["#### ", "    #", " ### ", "    #", "#### "],
    "4": ["#  # ", "#  # ", "#####", "   # ", "   # "],
    "5": ["#####", "#    ", "#### ", "    #", "#### "],
    "6": [" ### ", "#    ", "#### ", "#   #", " ### "],
    "7": ["#####", "   # ", "  #  ", " #   ", " #   "],
    "8": [" ### ", "#   #", " ### ", "#   #", " ### "],
    "9": [" ### ", "#   #", " ####", "    #", " ### "],
    "A": [" ### ", "#   #", "#####", "#   #", "#   #"],
    "B": ["#### ", "#   #", "#### ", "#   #", "#### "],
    "C": [" ####", "#    ", "#    ", "#    ", " ####"],
    "D": ["#### ", "#   #", "#   #", "#   #", "#### "],
    "E": ["#####", "#    ", "#### ", "#    ", "#####"],
    "F": ["#####", "#    ", "#### ", "#    ", "#    "],
    "G": [" ####", "#    ", "#  ##", "#   #", " ### "],
    "H": ["#   #", "#   #", "#####", "#   #", "#   #"],
    "J": ["  ###", "   # ", "   # ", "#  # ", " ##  "],
    "K": ["#   #", "#  # ", "###  ", "#  # ", "#   #"],
    "M": ["#   #", "## ##", "# # #", "#   #", "#   #"],
    "N": ["#   #", "##  #", "# # #", "#  ##", "#   #"],
    "P": ["#### ", "#   #", "#### ", "#    ", "#    "],
    "Q": [" ### ", "#   #", "# # #", "#  # ", " ## #"],
    "R": ["#### ", "#   #", "#### ", "#  # ", "#   #"],
    "S": [" ####", "#    ", " ### ", "    #", "#### "],
    "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
    "V": ["#   #", "#   #", "#   #", " # # ", "  #  "],
    "W": ["#   #", "#   #", "# # #", "## ##", "#   #"],
    "X": ["#   #", " # # ", "  #  ", " # # ", "#   #"],
    "Y": ["#   #", " # # ", "  #  ", "  #  ", "  #  "],
    "Z": ["#####", "   # ", "  #  ", " #   ", "#####"],
    "-": ["     ", "     ", " ### ", "     ", "     "],
}


def render(text, on="█", off=" ", scale=2):
    """Rows of block text; each font pixel is `scale` columns wide."""
    rows = ["", "", "", "", ""]
    for ch in text.upper():
        g = FONT.get(ch, FONT["-"])
        for i in range(5):
            rows[i] += "".join((on if c == "#" else off) * scale for c in g[i]) + off * scale
    return rows


def render_code(code, width, on="\u2588", height=None):
    """The pairing code (XXXX-XXXX-XXXX) as large as the space allows: one
    line, as wide and tall as fits (up to 6x), else one group per line."""
    for scale in (6, 5, 4, 3, 2, 1):
        rows = render(code, on=on, scale=scale)
        tall = max(1, scale // 2)
        if len(rows[0]) <= width and (height is None or 5 * tall <= height):
            # console cells are about twice as tall as wide: on a big screen
            # make the letters taller too, as far as the height allows
            while height and tall < scale and 5 * tall * 2 <= height:
                tall *= 2
            return [r for r in rows for _ in range(tall)]
    out = []
    for i, group in enumerate(code.split("-")):
        if i:
            out.append("")
        out += render(group, on=on, scale=2 if 4 * 12 <= width else 1)
    return out
