#!/usr/bin/env python3
"""Check a review's stage files, then build the Word memo and one-slide summary.

Usage:
    python build_deliverables.py <review_dir>                 check, then build
    python build_deliverables.py <review_dir> --check         check only
    python build_deliverables.py <review_dir> --force         build despite errors
    python build_deliverables.py <review_dir> --only docx|pptx

Reads   <review_dir>/memo.md, the stage-output JSON files,
        <review_dir>/extract/hidden-text.json, and optionally
        <review_dir>/company-logo.png (the reviewed company's logo, slide header)

Writes  <review_dir>/memo.docx          full memorandum, Word format
        <review_dir>/summary.pptx       single 16:9 slide: investability gauge,
                                        AI-usage and hidden-text chips, votes,
                                        gaps, questions

The check runs first. It confirms every file name and key the deliverables
need, prints the facts behind the recommendation, and fails a recommendation
the evidence doesn't allow. With errors nothing is built, so the deliverables
are built once, from a complete review.

Zero dependencies - standard library only. Both formats are emitted as raw
OOXML, so this runs unchanged on a machine with no python-docx, no python-pptx,
no pandoc and no LibreOffice. Do not add third-party imports to this file.

Colours are brand-neutral: greys for ink and hairlines, and a fixed status
palette (critical / warning / good) for the gauge zones, checked for
colour-vision deficiency (worst adjacent pair dE 11.3). Warning yellow sits
below 3:1 on white, so zone labels are always shown, in ink, and no status is
ever carried by colour alone.
"""

import json
import math
import os
import re
import sys
import zipfile
from datetime import date

# ---------------------------------------------------------------- palette

INK = "0B0B0B"        # primary text
INK2 = "52514E"       # secondary text and small labels
MUTED = "898781"      # ticks and keys
RULE = "E1E0D9"       # hairlines
BASELINE = "C3C2B7"   # heavier rules
PANEL = "F9F9F7"      # panel fill
WHITE = "FFFFFF"

GOOD = "0CA30C"
WARNING = "FAB219"
SERIOUS = "EC835A"
CRITICAL = "D03B3B"
NEUTRAL = "C3C2B7"    # not run / not checked

FONT = "Arial"
PRODUCER = "VC Review"

EMU_IN = 914400
SLIDE_W = 12192000    # 13.333in
SLIDE_H = 6858000     # 7.5in

# investability bands on 0-100, from the rubric anchors
BANDS = [(0, 45, "PASS", CRITICAL),
         (45, 70, "FURTHER DD", WARNING),
         (70, 101, "INVEST", GOOD)]

STAGE_WEIGHTS = {
    "pre-seed": {"team": .30, "problem-idea": .15, "market-timing": .20,
                 "product-tech": .10, "business-model": .05, "competition-moat": .10,
                 "traction": .05, "scalability-ops": .025, "financials-ask": .025},
    "seed": {"team": .25, "problem-idea": .10, "market-timing": .20,
             "product-tech": .10, "business-model": .10, "competition-moat": .10,
             "traction": .10, "scalability-ops": .025, "financials-ask": .025},
    "series-a": {"team": .15, "problem-idea": .05, "market-timing": .15,
                 "product-tech": .10, "business-model": .20, "competition-moat": .10,
                 "traction": .20, "scalability-ops": .025, "financials-ask": .025},
}
STAGE_WEIGHTS["later"] = STAGE_WEIGHTS["series-a"]
DIMENSIONS = list(STAGE_WEIGHTS["seed"])

THESIS_PERSONAS = ["network-hunter", "tech-oracle", "monopoly-maker",
                   "unit-master", "value-investor"]
FIN_VERDICTS = {"sound", "repairable", "unreliable", "absent"}
IM_VERDICTS = {"institutional-grade", "adequate-with-fixes", "not-fit-for-purpose", "absent"}
PERSONA_VERDICTS = {"strong-yes", "yes", "no", "strong-no"}
REC_RE = re.compile(r"^#\s*(PASS|FURTHER DUE DILIGENCE|INVEST)\s*$", re.M | re.I)

AVG_GLYPH = 0.52       # average Arial glyph width, as a fraction of the point size
MAX_SLIDE_ITEM = 600   # hard cap before the slide fits text to its box


def band_c_layout(has_votes, has_assumption):
    """Inches (width, height) the slide gives the fatal gaps and the founder
    questions. Mirrors the geometry in build_summary_slide."""
    fy = 1.10 + 1.86 + 0.20 + 0.22 + (0.44 if has_votes else 0.24) + 0.24
    fh = SLIDE_H / EMU_IN - fy - 0.60
    colw = (SLIDE_W / EMU_IN - 1.0 - 0.30) / 2
    gaps_h = fh * 0.56 if has_assumption else fh - 0.24
    return {"gaps": (colw - 0.1875, gaps_h), "questions": (colw, fh - 0.24)}


def fit_items(items, box, size=9.0, before=4.5):
    """Shorten items so they fit a text box, sharing its lines between them.
    Drops to one point smaller only when the longest item wouldn't fit.
    Returns (items, font size, character limit per item)."""
    width, height = box
    n = max(len(items), 1)
    options = []
    for pt in (size, size - 1):
        per_line = int(width * 72 / (pt * AVG_GLYPH) * 0.9)
        lines = int((height * 72 - n * before) / (pt * 1.2))
        options.append((pt, max(40, lines // n * per_line)))
    longest = max((len(" ".join(str(s).split())) for s in items), default=0)
    pt, limit = options[0] if longest <= options[0][1] else options[1]
    return [truncate(s, limit, sentence=True) for s in items], pt, limit


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def band_for(score100):
    for lo, hi, name, col in BANDS:
        if lo <= score100 < hi:
            return name, col
    return BANDS[-1][2], BANDS[-1][3]


def on_fill(colour):
    return WHITE if colour == CRITICAL else INK


def ai_deduction(score):
    """Points off the 0-100 investability score for materials visibly thrown
    together with AI (rubric: AI usage). Scores of 0-5 cost nothing."""
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return 0
    if score >= 9:
        return 5
    return {6: 2, 7: 3, 8: 4}.get(int(score), 0)


def ai_colour(score):
    if score <= 2:
        return GOOD
    if score <= 5:
        return WARNING
    if score <= 7:
        return SERIOUS
    return CRITICAL


# ------------------------------------------------------------- markdown parsing

INLINE = re.compile(r"(\*\*.+?\*\*|\*[^*]+?\*|`[^`]+?`)", re.S)


def runs(text):
    out = []
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append((part[2:-2], True, False, False))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            out.append((part[1:-1], False, False, True))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out.append((part[1:-1], False, True, False))
        else:
            out.append((part, False, False, False))
    return out or [(text, False, False, False)]


def parse_md(md):
    blocks, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if not ln.strip():
            i += 1
            continue
        if set(ln.strip()) <= {"-", "*"} and len(ln.strip()) >= 3:
            blocks.append(("hr",))
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            blocks.append(("h", len(m.group(1)), m.group(2).strip()))
            i += 1
            continue
        if ln.lstrip().startswith("|") and "|" in ln[1:]:
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= {"-", ":", " "} and c for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue
        if ln.lstrip().startswith(">"):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            blocks.append(("quote", " ".join(buf).strip()))
            continue
        m = re.match(r"^\s*(?:[-*]|(\d+)\.)\s+(.*)$", ln)
        if m:
            blocks.append(("li", m.group(2).strip(), bool(m.group(1))))
            i += 1
            continue
        buf = [ln.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^\s*(#{1,6}\s|[-*]\s|\d+\.\s|\||>)", lines[i]) and \
                not (set(lines[i].strip()) <= {"-", "*"} and len(lines[i].strip()) >= 3):
            buf.append(lines[i].strip())
            i += 1
        blocks.append(("p", " ".join(buf)))
    return blocks


# --------------------------------------------------------------------- docx out

def _r(text, bold=False, italic=False, mono=False, color=None, size=None):
    rpr = []
    if bold:
        rpr.append("<w:b/>")
    if italic:
        rpr.append("<w:i/>")
    if mono:
        rpr.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
    if color:
        rpr.append(f'<w:color w:val="{color}"/>')
    if size:
        rpr.append(f'<w:sz w:val="{int(size*2)}"/><w:szCs w:val="{int(size*2)}"/>')
    pr = f"<w:rPr>{''.join(rpr)}</w:rPr>" if rpr else ""
    return f'<w:r>{pr}<w:t xml:space="preserve">{esc(text)}</w:t></w:r>'


def _p(content, space_after=120, space_before=0, ind=0, keep=False):
    ppr = ["<w:keepNext/>"] if keep else []
    if ind:
        ppr.append(f'<w:ind w:left="{ind}"/>')
    ppr.append(f'<w:spacing w:before="{space_before}" w:after="{space_after}"'
               ' w:line="264" w:lineRule="auto"/>')
    return f"<w:p><w:pPr>{''.join(ppr)}</w:pPr>{content}</w:p>"


def md_to_docx_body(blocks):
    out = []
    for b in blocks:
        kind = b[0]
        if kind == "h":
            lvl, text = b[1], b[2]
            size = {1: 22, 2: 15, 3: 12.5, 4: 11.5}.get(lvl, 11)
            out.append(_p(_r(text, bold=True, color=INK, size=size),
                          space_before=280 if lvl > 1 else 0, space_after=140,
                          keep=True))
            if lvl == 1:
                out.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="12" '
                           f'w:space="1" w:color="{BASELINE}"/></w:pBdr>'
                           '<w:spacing w:after="200"/></w:pPr></w:p>')
        elif kind == "p":
            out.append(_p("".join(_r(t, bold=bo, italic=it, mono=mo)
                                  for t, bo, it, mo in runs(b[1]))))
        elif kind == "li":
            body = "".join(_r(t, bold=bo, italic=it, mono=mo)
                           for t, bo, it, mo in runs(b[1]))
            out.append(_p(_r("•  ", color=INK2, bold=True) + body,
                          ind=360, space_after=80))
        elif kind == "quote":
            body = "".join(_r(t, bold=bo, italic=it, mono=mo)
                           for t, bo, it, mo in runs(b[1]))
            out.append(f'<w:p><w:pPr><w:ind w:left="360"/><w:pBdr>'
                       f'<w:left w:val="single" w:sz="18" w:space="8" w:color="{BASELINE}"/>'
                       f'</w:pBdr><w:shd w:val="clear" w:fill="{PANEL}"/>'
                       f'<w:spacing w:before="140" w:after="180"/></w:pPr>{body}</w:p>')
        elif kind == "hr":
            out.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" '
                       f'w:space="1" w:color="{RULE}"/></w:pBdr>'
                       '<w:spacing w:before="160" w:after="160"/></w:pPr></w:p>')
        elif kind == "table":
            out.append(_table(b[1]))
    return "".join(out)


def _table(rows):
    ncol = max(len(r) for r in rows)
    total = 9360
    w = total // ncol
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for _ in range(ncol))
    trs = []
    for ri, row in enumerate(rows):
        head = ri == 0
        tcs = []
        for ci in range(ncol):
            cell = row[ci] if ci < len(row) else ""
            body = "".join(_r(t, bold=(bo or head), italic=it, mono=mo, color=INK, size=9)
                           for t, bo, it, mo in runs(cell))
            shd = f'<w:shd w:val="clear" w:fill="{RULE if head else WHITE}"/>'
            tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>{shd}'
                       '<w:tcMar><w:top w:type="dxa" w:w="70"/>'
                       '<w:left w:type="dxa" w:w="100"/>'
                       '<w:bottom w:type="dxa" w:w="70"/>'
                       '<w:right w:type="dxa" w:w="100"/></w:tcMar>'
                       '<w:vAlign w:val="center"/></w:tcPr>'
                       '<w:p><w:pPr><w:spacing w:after="0" w:line="240" '
                       f'w:lineRule="auto"/></w:pPr>{body or _r("")}</w:p></w:tc>')
        trs.append(f'<w:tr>{"<w:trPr><w:tblHeader/></w:trPr>" if head else ""}'
                   f'{"".join(tcs)}</w:tr>')
    borders = "".join(f'<w:{e} w:val="single" w:sz="4" w:space="0" w:color="{BASELINE}"/>'
                      for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
    return (f'<w:tbl><w:tblPr><w:tblW w:w="{total}" w:type="dxa"/>'
            f'<w:tblBorders>{borders}</w:tblBorders><w:tblLayout w:type="fixed"/>'
            f'</w:tblPr><w:tblGrid>{grid}</w:tblGrid>{"".join(trs)}</w:tbl>'
            '<w:p><w:pPr><w:spacing w:after="160"/></w:pPr></w:p>')


DOCX_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:eastAsia="{FONT}" w:cs="{FONT}"/>
<w:color w:val="{INK}"/><w:sz w:val="21"/><w:szCs w:val="21"/>
</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>
<w:spacing w:after="120" w:line="264" w:lineRule="auto"/>
</w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal">
<w:name w:val="Normal"/></w:style></w:styles>"""


def build_docx(review_dir, memo_md, meta):
    body = md_to_docx_body(parse_md(memo_md))
    footer = ('<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
              + _r(f"{PRODUCER}   ·   Private & Confidential   ·   " + meta["company"],
                   color=INK2, size=8) + "</w:p>")
    doc = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>{body}
<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>
<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"
 w:header="567" w:footer="567" w:gutter="0"/>
<w:footerReference w:type="default" r:id="rId5"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
</w:sectPr></w:body></w:document>"""
    ftr = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'{footer}</w:ftr>')
    ct = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""
    drels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
</Relationships>"""
    core = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>IC Memorandum - {esc(meta['company'])}</dc:title>
<dc:creator>{PRODUCER}</dc:creator>
<cp:lastModifiedBy>{PRODUCER}</cp:lastModifiedBy>
<dcterms:created xsi:type="dcterms:W3CDTF">{meta['date_iso']}T00:00:00Z</dcterms:created>
</cp:coreProperties>"""
    path = os.path.join(review_dir, "memo.docx")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", drels)
        z.writestr("word/styles.xml", DOCX_STYLES)
        z.writestr("word/footer1.xml", ftr)
        z.writestr("docProps/core.xml", core)
    return path


# ------------------------------------------------------------- pptx primitives

def _sp(idx, name, x, y, cx, cy, fill=None, line=None, lw=9525,
        prst="rect", adj=None):
    # Preset geometries name their adjust handles differently: pie/blockArc use
    # adj1..adjN, roundRect uses a single handle called "adj". Pass a dict to
    # name them explicitly; a list is treated as adj1..adjN.
    if isinstance(adj, dict):
        pairs = list(adj.items())
    elif adj:
        pairs = [(f"adj{i+1}", v) for i, v in enumerate(adj)]
    else:
        pairs = []
    av = ("<a:avLst>" + "".join(f'<a:gd name="{n}" fmla="val {v}"/>'
                                for n, v in pairs) + "</a:avLst>"
          ) if pairs else "<a:avLst/>"
    f = f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>' if fill else '<a:noFill/>'
    ln = (f'<a:ln w="{lw}"><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>'
          if line else '<a:ln><a:noFill/></a:ln>')
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{idx}" name="{esc(name)}"/><p:cNvSpPr/>'
            f'<p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{int(x)}" y="{int(y)}"/>'
            f'<a:ext cx="{int(cx)}" cy="{int(cy)}"/></a:xfrm>'
            f'<a:prstGeom prst="{prst}">{av}</a:prstGeom>{f}{ln}</p:spPr></p:sp>')


def _tx(idx, name, x, y, cx, cy, paras, anchor="t", wrap=True):
    body = ""
    for p in paras:
        text = p.get("t", "")
        sz = int(p.get("sz", 12) * 100)
        col = p.get("c", INK)
        b = "1" if p.get("b") else "0"
        i = "1" if p.get("i") else "0"
        al = p.get("al", "l")
        spc = p.get("spc", 0)
        before = p.get("before", 0)
        bullet = p.get("bullet")
        pre = (f'<a:buClr><a:srgbClr val="{p.get("bc", INK2)}"/></a:buClr>'
               f'<a:buChar char="{esc(bullet)}"/>') if bullet else '<a:buNone/>'
        ind = ' marL="171450" indent="-171450"' if bullet else ' marL="0" indent="0"'
        r = ""
        if text:
            r = (f'<a:r><a:rPr lang="en-GB" sz="{sz}" b="{b}" i="{i}" dirty="0" '
                 f'spc="{spc}"><a:solidFill><a:srgbClr val="{col}"/></a:solidFill>'
                 f'<a:latin typeface="{FONT}"/></a:rPr><a:t>{esc(text)}</a:t></a:r>')
        body += (f'<a:p><a:pPr algn="{al}"{ind}><a:spcBef><a:spcPts val="{before}"/>'
                 f'</a:spcBef>{pre}</a:pPr>{r}</a:p>')
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{idx}" name="{esc(name)}"/>'
            f'<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr>'
            f'<a:xfrm><a:off x="{int(x)}" y="{int(y)}"/>'
            f'<a:ext cx="{int(cx)}" cy="{int(cy)}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
            f'<p:txBody><a:bodyPr wrap="{"square" if wrap else "none"}" '
            f'anchor="{anchor}" lIns="0" tIns="0" rIns="0" bIns="0">'
            '<a:normAutofit/></a:bodyPr><a:lstStyle/>'
            f'{body}</p:txBody></p:sp>')


def _pic(idx, name, rid, x, y, cx, cy):
    return (f'<p:pic><p:nvPicPr><p:cNvPr id="{idx}" name="{esc(name)}"/>'
            '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/>'
            f'</p:nvPicPr><p:blipFill><a:blip r:embed="{rid}"/>'
            '<a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr>'
            f'<a:xfrm><a:off x="{int(x)}" y="{int(y)}"/>'
            f'<a:ext cx="{int(cx)}" cy="{int(cy)}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>')


def _deg60k(d):
    # DrawingML angles are 60000ths of a degree. Clamp rather than wrap: a 360deg
    # end angle must not become 0, which would collapse the sweep.
    return max(0, min(int(round(d * 60000)), 21599999))


def _polar(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


# ------------------------------------------------------------------ the gauge

def gauge(idx0, cx, cy, r, actual, corrected):
    """Semicircular band gauge, 0-100: three labelled zones and two markers.

    Angles run 180deg (west) to 360deg (east) over the top. blockArc is a filled
    band, so no punch-out disc or half-plane mask is needed.
    """
    sh, idx = [], idx0

    def nxt():
        nonlocal idx
        idx += 1
        return idx

    def ang(v):
        return 180 + 1.8 * max(0, min(100, v))

    thick = 27000                      # band thickness, fraction of radius
    gap = 0.9                          # degrees of white between zones
    for k, (lo, hi, name, col) in enumerate(BANDS):
        start = ang(lo) + (gap if k else 0)
        end = ang(min(hi, 100)) - (gap if k < len(BANDS) - 1 else 0)
        sh.append(_sp(nxt(), f"zone-{name}", cx - r, cy - r, 2 * r, 2 * r,
                      fill=col, prst="blockArc",
                      adj=[_deg60k(start), _deg60k(end), thick]))

    # zone labels sit outside the band, in ink, reading outward
    lh = int(0.17 * EMU_IN)
    lw = int(0.92 * EMU_IN)
    for lo, hi, name, col in BANDS:
        mid = (lo + min(hi, 100)) / 2
        mx, my = _polar(cx, cy, r * 1.13, ang(mid))
        if mid < 40:
            bx, al = mx - lw, "r"
        elif mid > 60:
            bx, al = mx, "l"
        else:
            bx, al = mx - lw / 2, "ctr"
        sh.append(_tx(nxt(), f"zl-{name}", bx, my - lh / 2, lw, lh,
                      [{"t": name, "sz": 7, "b": True, "c": INK2, "al": al,
                        "spc": 40}], anchor="ctr"))

    for v, lab, al in ((0, "0", "l"), (100, "100", "r")):
        tx, _ = _polar(cx, cy, r, ang(v))
        w = int(0.34 * EMU_IN)
        sh.append(_tx(nxt(), f"tick{v}", tx - (0 if al == "l" else w),
                      cy + int(0.04 * EMU_IN), w, int(0.15 * EMU_IN),
                      [{"t": lab, "sz": 6.5, "c": MUTED, "al": al}]))

    # hollow = documents-corrected counterfactual, solid = actual, on the band
    mr = r * 0.105
    band_r = r * (1 - thick / 100000.0 / 2)
    for val, solid, nm in ((corrected, False, "corrected"), (actual, True, "actual")):
        px, py = _polar(cx, cy, band_r, ang(val))
        sh.append(_sp(nxt(), f"mk-{nm}", px - mr, py - mr, 2 * mr, 2 * mr,
                      fill=(INK if solid else WHITE), line=INK,
                      lw=19050, prst="ellipse"))

    kd = int(0.055 * EMU_IN)
    ky = cy + int(0.24 * EMU_IN)
    kx = cx - int(0.92 * EMU_IN)
    sh.append(_sp(nxt(), "key-a", kx, ky, kd, kd, fill=INK, line=INK,
                  lw=12700, prst="ellipse"))
    sh.append(_tx(nxt(), "key-al", kx + int(0.09 * EMU_IN), ky - int(0.01 * EMU_IN),
                  int(0.5 * EMU_IN), int(0.14 * EMU_IN),
                  [{"t": "actual", "sz": 6.5, "c": INK2}]))
    kx2 = kx + int(0.62 * EMU_IN)
    sh.append(_sp(nxt(), "key-c", kx2, ky, kd, kd, fill=WHITE, line=INK,
                  lw=12700, prst="ellipse"))
    sh.append(_tx(nxt(), "key-cl", kx2 + int(0.09 * EMU_IN), ky - int(0.01 * EMU_IN),
                  int(1.2 * EMU_IN), int(0.14 * EMU_IN),
                  [{"t": "documents corrected", "sz": 6.5, "c": INK2}]))
    return sh, idx


# ----------------------------------------------------------------- the slide

def build_summary_slide(d, rids):
    sh, idx = [], 10

    def nxt():
        nonlocal idx
        idx += 1
        return idx

    IN = EMU_IN
    M = int(0.5 * IN)
    W = SLIDE_W - 2 * M

    # ---------- header: company, integrity chips, recommendation
    hy = int(0.26 * IN)
    tx = M
    if rids.get("company"):
        lh = int(0.44 * IN)
        lw = lh * d["company_logo_aspect"]
        sh.append(_pic(nxt(), "company-logo", rids["company"], M, hy, lw, lh))
        tx = M + lw + int(0.26 * IN)
    pw, ph = int(2.35 * IN), int(0.42 * IN)
    px = M + W - pw
    cw, cgap = int(1.95 * IN), int(0.12 * IN)
    chip_x = [px - int(0.18 * IN) - 2 * cw - cgap, px - int(0.18 * IN) - cw]
    title_w = max(chip_x[0] - int(0.22 * IN) - tx, int(2.2 * IN))
    sh.append(_tx(nxt(), "co", tx, hy - int(0.02 * IN), title_w, int(0.28 * IN),
                  [{"t": d["company"], "sz": 17, "b": True, "c": INK}]))
    sh.append(_tx(nxt(), "sub", tx, hy + int(0.27 * IN), title_w, int(0.2 * IN),
                  [{"t": f"Investment Committee Summary   ·   {d['stage_short']}"
                         f"   ·   {d['date']}", "sz": 9.5, "c": INK2}]))

    def chip(x, key, label, value, colour, strong):
        y = hy + int(0.02 * IN)
        sh.append(_sp(nxt(), f"chip-{key}", x, y, cw, ph, fill=WHITE,
                      line=colour if strong else RULE, lw=19050 if strong else 9525,
                      prst="roundRect", adj={"adj": 16000}))
        sh.append(_tx(nxt(), f"chipl-{key}", x + int(0.13 * IN), y + int(0.05 * IN),
                      cw - int(0.22 * IN), int(0.14 * IN),
                      [{"t": label, "sz": 6.5, "b": True, "c": INK2, "spc": 40}]))
        dot = int(0.10 * IN)
        sh.append(_sp(nxt(), f"chipd-{key}", x + int(0.13 * IN), y + int(0.24 * IN),
                      dot, dot, fill=colour, line=colour, prst="ellipse"))
        sh.append(_tx(nxt(), f"chipv-{key}", x + int(0.29 * IN), y + int(0.19 * IN),
                      cw - int(0.38 * IN), int(0.2 * IN),
                      [{"t": value, "sz": 9.5, "b": True, "c": INK}], anchor="ctr"))

    chip(chip_x[0], "ai", "AI USAGE", d["ai_value"], d["ai_color"], False)
    chip(chip_x[1], "hidden", d["hidden_label"], d["hidden_value"], d["hidden_color"],
         d["hidden_strong"])
    sh.append(_sp(nxt(), "recpill", px, hy + int(0.02 * IN), pw, ph,
                  fill=d["rec_color"], prst="roundRect", adj={"adj": 22000}))
    sh.append(_tx(nxt(), "rec", px, hy + int(0.12 * IN), pw, int(0.24 * IN),
                  [{"t": d["recommendation"], "sz": 13.5, "b": True,
                    "c": on_fill(d["rec_color"]), "al": "ctr", "spc": 80}]))
    sh.append(_sp(nxt(), "hrule", M, int(0.92 * IN), W, 9525, fill=BASELINE))

    # ---------- band A: gauge panel + stat tiles
    ay, ah = int(1.10 * IN), int(1.86 * IN)
    gw = int(W * 0.50)
    sh.append(_sp(nxt(), "gpanel", M, ay, gw, ah, fill=PANEL, line=RULE,
                  prst="roundRect", adj={"adj": 5000}))
    sh.append(_tx(nxt(), "glab", M + int(0.24 * IN), ay + int(0.16 * IN),
                  int(2.6 * IN), int(0.2 * IN),
                  [{"t": "INVESTABILITY SCORE", "sz": 8.5, "b": True, "c": INK2,
                    "spc": 100}]))
    sh.append(_tx(nxt(), "gbig", M + int(0.24 * IN), ay + int(0.42 * IN),
                  int(2.2 * IN), int(0.62 * IN),
                  [{"t": str(d["score100"]), "sz": 44, "b": True, "c": INK}]))
    sh.append(_tx(nxt(), "gof", M + int(0.24 * IN) + int(1.05 * IN),
                  ay + int(0.78 * IN), int(1.2 * IN), int(0.2 * IN),
                  [{"t": "/ 100", "sz": 11, "c": INK2}]))
    sh.append(_tx(nxt(), "gsub", M + int(0.24 * IN), ay + int(1.16 * IN),
                  int(3.0 * IN), int(0.2 * IN),
                  [{"t": d["score_basis"], "sz": 8.5, "c": INK2}]))
    sh.append(_tx(nxt(), "ghyp", M + int(0.24 * IN), ay + int(1.40 * IN),
                  int(3.0 * IN), int(0.34 * IN),
                  [{"t": d["corrected_line"], "sz": 8.5, "b": True, "c": INK},
                   {"t": d["corrected_note"], "sz": 7.5, "c": INK2, "before": 200}]))
    gr = int(0.56 * IN)
    gcx = M + gw - int(1.34 * IN)
    gcy = ay + int(1.33 * IN)
    gsh, idx = gauge(idx, gcx, gcy, gr, d["score100"], d["corrected100"])
    sh.extend(gsh)

    tx0 = M + gw + int(0.20 * IN)
    tw_all = W - gw - int(0.20 * IN)
    gap = int(0.14 * IN)
    tw = (tw_all - gap) // 2
    th = (ah - gap) // 2
    tiles = [
        ("THESIS PANEL", d["vote_headline"], d["vote_sub"], d["vote_color"]),
        ("PRESENTATION GAPS", str(d["fatal_n"]),
         f"fatal  ·  {d['serious_n']} serious  ·  {d['cosmetic_n']} cosmetic",
         CRITICAL if d["fatal_n"] else GOOD),
        ("FINANCIAL MODEL", d["model_verdict"], d["model_note"], d["model_color"]),
        ("INVESTOR MEMORANDUM", d["im_verdict"], d["im_note"], d["im_color"]),
    ]
    for k, (lab, big, sub, col) in enumerate(tiles):
        x = tx0 + (k % 2) * (tw + gap)
        y = ay + (k // 2) * (th + gap)
        sh.append(_sp(nxt(), f"tile{k}", x, y, tw, th, fill=WHITE, line=RULE,
                      prst="roundRect", adj={"adj": 8000}))
        ix, iw = x + int(0.18 * IN), tw - int(0.36 * IN)
        sh.append(_tx(nxt(), f"tl{k}", ix, y + int(0.13 * IN), iw, int(0.18 * IN),
                      [{"t": lab, "sz": 7.5, "b": True, "c": INK2, "spc": 90}]))
        dot = int(0.12 * IN)
        sh.append(_sp(nxt(), f"td{k}", ix, y + int(0.42 * IN), dot, dot,
                      fill=col, line=col, prst="ellipse"))
        sh.append(_tx(nxt(), f"tb{k}", ix + int(0.21 * IN), y + int(0.33 * IN),
                      iw - int(0.21 * IN), int(0.30 * IN),
                      [{"t": big, "sz": 14, "b": True, "c": INK}]))
        sh.append(_tx(nxt(), f"ts{k}", ix, y + int(0.64 * IN), iw, int(0.2 * IN),
                      [{"t": sub, "sz": 7.5, "c": INK2}]))

    # ---------- band B: persona chips
    by = ay + ah + int(0.20 * IN)
    sh.append(_tx(nxt(), "vh", M, by, int(W * .5), int(0.18 * IN),
                  [{"t": "PANEL VERDICTS", "sz": 7.5, "b": True, "c": INK2,
                    "spc": 90}]))
    cy0 = by + int(0.22 * IN)
    ch = int(0.44 * IN)
    if d["votes"]:
        n = len(d["votes"])
        cwg = int(0.09 * IN)
        cwid = (W - (n - 1) * cwg) // n
        for k, (persona, verdict) in enumerate(d["votes"]):
            x = M + k * (cwid + cwg)
            col = CRITICAL if verdict.endswith("no") else GOOD
            sh.append(_sp(nxt(), f"v{k}", x, cy0, cwid, ch, fill=WHITE, line=RULE,
                          prst="roundRect", adj={"adj": 14000}))
            sh.append(_tx(nxt(), f"vn{k}", x + int(0.10 * IN), cy0 + int(0.07 * IN),
                          cwid - int(0.2 * IN), int(0.16 * IN),
                          [{"t": persona, "sz": 8, "b": True, "c": INK}]))
            dot = int(0.09 * IN)
            sh.append(_sp(nxt(), f"vd{k}", x + int(0.10 * IN), cy0 + int(0.285 * IN),
                          dot, dot, fill=col, line=col, prst="ellipse"))
            lab = verdict.upper().replace("-", " ")
            if persona == "the-skeptic":
                lab += "  (bearish seat)"
            sh.append(_tx(nxt(), f"vv{k}", x + int(0.24 * IN), cy0 + int(0.24 * IN),
                          cwid - int(0.34 * IN), int(0.18 * IN),
                          [{"t": lab, "sz": 7.5, "b": True, "c": INK2}]))
        cy1 = cy0 + ch
    else:
        sh.append(_tx(nxt(), "vnone", M, cy0, W, int(0.22 * IN),
                      [{"t": "Not run (quick depth): no persona panel was convened.",
                        "sz": 9, "i": True, "c": INK2}]))
        cy1 = cy0 + int(0.24 * IN)

    # ---------- band C: gaps | questions
    fy = cy1 + int(0.24 * IN)
    fh = SLIDE_H - fy - int(0.60 * IN)
    colw = (W - int(0.30 * IN)) // 2
    dot = int(0.09 * IN)
    sh.append(_sp(nxt(), "gdot", M, fy + int(0.045 * IN), dot, dot,
                  fill=CRITICAL, line=CRITICAL, prst="ellipse"))
    sh.append(_tx(nxt(), "gh", M + int(0.16 * IN), fy, colw, int(0.18 * IN),
                  [{"t": f"FATAL GAPS  ·  {d['fatal_n']} UNRESOLVED", "sz": 7.5,
                    "b": True, "c": INK2, "spc": 90}]))
    layout = band_c_layout(bool(d["votes"]), bool(d.get("fatal_assumption")))
    gap_text, gap_pt, _ = fit_items(d["fatal_items"], layout["gaps"])
    gp = [{"t": g, "sz": gap_pt, "c": INK, "bullet": "▪", "before": 450} for g in gap_text]
    gh = int(fh * 0.56) if d.get("fatal_assumption") else fh - int(0.24 * IN)
    sh.append(_tx(nxt(), "gaps", M, fy + int(0.24 * IN), colw, gh, gp))
    if d.get("fatal_assumption"):
        ky2 = fy + int(0.24 * IN) + gh
        bh = fh - gh - int(0.28 * IN)
        sh.append(_sp(nxt(), "fabox", M, ky2, colw, bh, fill=PANEL, line=RULE,
                      prst="rect"))
        sh.append(_sp(nxt(), "fabar", M, ky2, int(0.05 * IN), bh, fill=CRITICAL))
        sh.append(_tx(nxt(), "fah", M + int(0.18 * IN), ky2 + int(0.12 * IN),
                      colw - int(0.34 * IN), int(0.16 * IN),
                      [{"t": "THE ASSUMPTION THAT KILLS IT FASTEST", "sz": 7,
                        "b": True, "c": INK2, "spc": 90}]))
        sh.append(_tx(nxt(), "fa", M + int(0.18 * IN), ky2 + int(0.34 * IN),
                      colw - int(0.34 * IN), bh - int(0.38 * IN),
                      [{"t": d["fatal_assumption"], "sz": 8.5, "c": INK}]))
    qx = M + colw + int(0.30 * IN)
    sh.append(_tx(nxt(), "qh", qx, fy, colw, int(0.18 * IN),
                  [{"t": "MAJOR QUESTIONS FOR THE FOUNDERS", "sz": 7.5, "b": True,
                    "c": INK2, "spc": 90}]))
    q_text, q_pt, _ = fit_items(d["questions"], layout["questions"])
    qp = [{"t": f"{i}. {q}", "sz": q_pt, "c": INK, "before": 450}
          for i, q in enumerate(q_text, 1)]
    sh.append(_tx(nxt(), "qs", qx, fy + int(0.24 * IN), colw, fh - int(0.24 * IN), qp))

    # ---------- footer
    fry = SLIDE_H - int(0.50 * IN)
    sh.append(_sp(nxt(), "frule", M, fry, W, 9525, fill=RULE))
    sh.append(_tx(nxt(), "fbrand", M, fry + int(0.16 * IN), int(3.0 * IN),
                  int(0.2 * IN),
                  [{"t": f"Prepared with {PRODUCER}", "sz": 9, "b": True, "c": INK2}]))
    sh.append(_tx(nxt(), "fdisc", M + int(3.4 * IN), fry + int(0.17 * IN),
                  W - int(3.4 * IN), int(0.22 * IN),
                  [{"t": "Private & Confidential. Decision support from an automated "
                         "review process, not investment advice. Verdicts are model "
                         "opinions; dissent is preserved.",
                    "sz": 7, "c": INK2, "al": "r"}]))

    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
            ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
            '<p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/>'
            '<a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/>'
            f'</a:xfrm></p:grpSpPr>{"".join(sh)}</p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')


def _theme():
    font = f'<a:latin typeface="{FONT}"/><a:ea typeface=""/><a:cs typeface=""/>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
            ' name="vc-review"><a:themeElements><a:clrScheme name="vc-review">'
            '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
            '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
            f'<a:dk2><a:srgbClr val="{INK2}"/></a:dk2>'
            f'<a:lt2><a:srgbClr val="{PANEL}"/></a:lt2>'
            '<a:accent1><a:srgbClr val="2A78D6"/></a:accent1>'
            '<a:accent2><a:srgbClr val="EB6834"/></a:accent2>'
            '<a:accent3><a:srgbClr val="1BAF7A"/></a:accent3>'
            '<a:accent4><a:srgbClr val="EDA100"/></a:accent4>'
            f'<a:accent5><a:srgbClr val="{GOOD}"/></a:accent5>'
            f'<a:accent6><a:srgbClr val="{CRITICAL}"/></a:accent6>'
            '<a:hlink><a:srgbClr val="2A78D6"/></a:hlink>'
            f'<a:folHlink><a:srgbClr val="{INK2}"/></a:folHlink></a:clrScheme>'
            f'<a:fontScheme name="vc-review"><a:majorFont>{font}</a:majorFont>'
            f'<a:minorFont>{font}</a:minorFont></a:fontScheme>'
            '<a:fmtScheme name="vc-review">'
            '<a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
            '<a:lnStyleLst><a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
            '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
            '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
            '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle>'
            '<a:effectStyle><a:effectLst/></a:effectStyle>'
            '<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
            '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>'
            '</a:fmtScheme></a:themeElements></a:theme>')


def build_pptx(review_dir, d):
    media, rids, rel_extra = [], {}, []
    logo = d.get("company_logo")
    if logo and os.path.exists(logo):
        rids["company"] = "rId3"
        media.append(("image1.png", logo))
        rel_extra.append('<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/'
                         'officeDocument/2006/relationships/image" Target="../media/image1.png"/>')

    slide = build_summary_slide(d, rids)
    empty = ('<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
             '<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
             '<a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/>'
             '<a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>')
    NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
          'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
    layout = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              f'<p:sldLayout {NS} type="blank" preserve="1">{empty}'
              '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')
    master = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              f'<p:sldMaster {NS}>{empty}'
              '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1"'
              ' accent2="accent2" accent3="accent3" accent4="accent4"'
              ' accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
              '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/>'
              '</p:sldLayoutIdLst></p:sldMaster>')
    pres = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:presentation {NS} saveSubsetFonts="1">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/>'
            '</p:sldMasterIdLst><p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
            f'<p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}"/>'
            f'<p:notesSz cx="{SLIDE_H}" cy="{SLIDE_W}"/></p:presentation>')
    ct = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""
    prels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>
</Relationships>"""
    mrels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>"""
    lrels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>"""
    srels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
             'officeDocument/2006/relationships/slideLayout" '
             'Target="../slideLayouts/slideLayout1.xml"/>'
             + "".join(rel_extra) + '</Relationships>')
    core = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>IC Summary - {esc(d['company'])}</dc:title>
<dc:creator>{PRODUCER}</dc:creator>
<cp:lastModifiedBy>{PRODUCER}</cp:lastModifiedBy>
<dcterms:created xsi:type="dcterms:W3CDTF">{d['date_iso']}T00:00:00Z</dcterms:created>
</cp:coreProperties>"""
    path = os.path.join(review_dir, "summary.pptx")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("ppt/presentation.xml", pres)
        z.writestr("ppt/_rels/presentation.xml.rels", prels)
        z.writestr("ppt/slides/slide1.xml", slide)
        z.writestr("ppt/slides/_rels/slide1.xml.rels", srels)
        z.writestr("ppt/slideLayouts/slideLayout1.xml", layout)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", lrels)
        z.writestr("ppt/slideMasters/slideMaster1.xml", master)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", mrels)
        z.writestr("ppt/theme/theme1.xml", _theme())
        z.writestr("docProps/core.xml", core)
        for name, src in media:
            with open(src, "rb") as f:
                z.writestr(f"ppt/media/{name}", f.read())
    return path


# ---------------------------------------------------------------- data assembly

def png_aspect(path, default=1.0):
    """Width/height from the PNG IHDR - no image library needed."""
    try:
        with open(path, "rb") as f:
            head = f.read(26)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return default
        w = int.from_bytes(head[16:20], "big")
        h = int.from_bytes(head[20:24], "big")
        return (w / h) if h else default
    except Exception:
        return default


def load(review_dir, name, default=None):
    p = os.path.join(review_dir, name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"_unreadable": str(exc)}


def as_dict(value):
    return value if isinstance(value, dict) and "_unreadable" not in value else {}


def is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def truncate(s, n, sentence=False):
    """Trim to n chars. With sentence=True, prefer the last full sentence that
    fits - a clean stop reads far better on a slide than a mid-word ellipsis.
    Otherwise cut at the last word boundary, never inside a word or bracket."""
    s = " ".join(str(s).split())
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    if sentence:
        stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        if stop > n * 0.5:
            return cut[:stop + 1]
    space = cut.rfind(" ")
    if space > n * 0.7:
        cut = cut[:space]
    return cut.rstrip(" ,.;:(-") + "…"


def extract(review_dir):
    prof = as_dict(load(review_dir, "deal-profile.json", {}))
    stage = str(prof.get("stage") or "pre-seed").lower()
    weights = STAGE_WEIGHTS.get(stage, STAGE_WEIGHTS["pre-seed"])

    total, wsum = 0.0, 0.0
    for dim, w in weights.items():
        js = as_dict(load(review_dir, f"stage1-{dim}.json"))
        if is_num(js.get("score")):
            total += w * js["score"]
            wsum += w
    composite = (total / wsum) if wsum else 0.0

    ai = as_dict(load(review_dir, "stage1b-ai-usage.json", {}))
    ai_score = ai.get("score") if is_num(ai.get("score")) else None
    deduction = ai_deduction(ai_score)
    score100 = max(0, int(round(composite * 10)) - deduction)
    basis = f"{composite:.1f} / 10 on {stage} weights"
    if deduction:
        basis += f"   ·   AI usage −{deduction}"
    if ai_score is None:
        ai_value, ai_col = "NOT RUN", NEUTRAL
    else:
        ai_value = f"{ai_score:g}/10" + (f"  ·  −{deduction} pts" if deduction else "")
        ai_col = ai_colour(ai_score)

    scen = as_dict(load(review_dir, "stage4-scenario-scores.json", {}))
    corrected100 = as_dict(scen.get("result")).get("corrected_0_100")
    if not is_num(corrected100):
        corrected100 = score100
        corrected_line = "Documents-corrected scenario not computed"
        corrected_note = ""
        corrected_text = "not computed"
    else:
        corrected100 = int(round(corrected100))
        delta = corrected100 - score100
        actual_band, _ = band_for(score100)
        corrected_band, _ = band_for(corrected100)
        corrected_line = (f"If every document defect were fixed:  {corrected100}"
                          f"/100   ({delta:+d})")
        if corrected_band == actual_band:
            corrected_note = (f"Still {corrected_band.lower()}: the gap is substantive, "
                              "not editorial")
        else:
            corrected_note = f"Moves to {corrected_band.lower()}: the paperwork costs a band"
        corrected_text = f"{corrected100}/100 ({delta:+d})"

    votes, yes_votes, no_votes, skeptic = [], 0, 0, ""
    for p in THESIS_PERSONAS + ["the-skeptic"]:
        js = as_dict(load(review_dir, f"stage2-persona-{p}.json"))
        if not js:
            continue
        v = str(js.get("verdict") or "?").lower()
        votes.append((p, v))
        if p == "the-skeptic":
            skeptic = v
        elif v.endswith("yes"):
            yes_votes += 1
        else:
            no_votes += 1
    if not votes:
        vote_col = NEUTRAL
    elif yes_votes == 0:
        vote_col = CRITICAL
    elif yes_votes >= 3:
        vote_col = GOOD
    else:
        vote_col = WARNING

    gaps = as_dict(load(review_dir, "stage3-gap-analysis.json", {}))
    summ = as_dict(gaps.get("summary"))
    fatal_items = [truncate(g, MAX_SLIDE_ITEM) for g in summ.get("fatal_items", [])
                   if isinstance(g, str)][:4]

    qs = as_dict(load(review_dir, "stage3-questions.json", {}))
    questions = [truncate(q.get("q", ""), MAX_SLIDE_ITEM)
                 for q in (qs.get("a_for_the_founders") or []) if isinstance(q, dict)][:5]

    pm = as_dict(load(review_dir, "stage3-premortem.json", {}))
    fatal_assumption = (truncate(pm.get("single_fatal_assumption", ""), 340, sentence=True)
                        if pm.get("single_fatal_assumption") else "")

    fin = as_dict(load(review_dir, "stage1b-financial-model-audit.json", {}))
    im = as_dict(load(review_dir, "stage1b-im-review.json", {}))
    fv = str(fin.get("verdict") or "not run").upper()
    iv = str(im.get("verdict") or "not run").replace("-", " ").upper()
    fcol = {"SOUND": GOOD, "REPAIRABLE": WARNING, "UNRELIABLE": CRITICAL}.get(fv, NEUTRAL)
    icol = {"INSTITUTIONAL GRADE": GOOD, "ADEQUATE WITH FIXES": WARNING,
            "NOT FIT FOR PURPOSE": CRITICAL}.get(iv, NEUTRAL)

    fc = load(review_dir, "stage1b-founder-check.json")
    if fc is None:
        founder_state = "not run"
    elif as_dict(fc).get("degraded"):
        founder_state = "degraded (no web access)"
    else:
        founder_state = "ran"

    ht = load(review_dir, os.path.join("extract", "hidden-text.json"))
    if not as_dict(ht):
        hidden = {"value": "NOT CHECKED", "label": "HIDDEN TEXT", "color": NEUTRAL,
                  "strong": False, "line": "not checked", "instr": 0}
    else:
        s = as_dict(ht.get("summary"))
        serious = int(s.get("high", 0) or 0) + int(s.get("medium", 0) or 0)
        instr = int(s.get("instruction_like", 0) or 0)
        low = int(s.get("low", 0) or 0)
        if serious:
            hidden = {"value": f"FOUND  ·  {serious}",
                      "label": "HIDDEN AI INSTRUCTIONS" if instr else "HIDDEN TEXT",
                      "color": CRITICAL, "strong": True, "instr": instr,
                      "line": f"{serious} finding(s), {instr} read like instructions to an AI"
                              + (f", plus {low} minor" if low else "")}
        else:
            hidden = {"value": "NONE FOUND", "label": "HIDDEN TEXT", "color": GOOD,
                      "strong": False, "instr": 0,
                      "line": "none found" + (f" ({low} minor notes)" if low else "")}

    rec = "NOT STATED"
    mp = os.path.join(review_dir, "memo.md")
    memo_md = ""
    if os.path.exists(mp):
        with open(mp, encoding="utf-8") as f:
            memo_md = f.read()
        m = REC_RE.search(memo_md)
        if m:
            rec = m.group(1).upper()
    rec_color = {"PASS": CRITICAL, "FURTHER DUE DILIGENCE": WARNING,
                 "INVEST": GOOD}.get(rec, NEUTRAL)

    company_logo = os.path.join(review_dir, "company-logo.png")
    company_logo = company_logo if os.path.exists(company_logo) else None

    today = date.today()
    fatal_n = summ.get("fatal", len(fatal_items))
    return {
        "company": prof.get("company", os.path.basename(os.path.abspath(review_dir))),
        "stage_short": stage,
        "depth": str(prof.get("depth") or "full").lower(),
        "date": today.strftime("%d %b %Y"),
        "date_iso": today.isoformat(),
        "composite": f"{composite:.1f}",
        "score100": score100,
        "score_basis": basis,
        "ai_score": ai_score,
        "ai_deduction": deduction,
        "ai_value": ai_value,
        "ai_color": ai_col,
        "corrected100": corrected100,
        "corrected_line": corrected_line,
        "corrected_note": corrected_note,
        "corrected_text": corrected_text,
        "recommendation": rec,
        "rec_color": rec_color,
        "votes": votes,
        "yes_votes": yes_votes,
        "skeptic": skeptic,
        "vote_headline": (f"{yes_votes} yes / {no_votes} no" if votes else "n/a"),
        "vote_sub": ("5 thesis seats; Skeptic separate" if votes
                     else "panel not run at quick depth"),
        "vote_color": vote_col,
        "fatal_n": fatal_n if is_num(fatal_n) else len(fatal_items),
        "serious_n": summ.get("serious", 0),
        "cosmetic_n": summ.get("cosmetic", 0),
        "fatal_items": fatal_items or ["None recorded"],
        "questions": questions or ["None recorded"],
        "fatal_assumption": fatal_assumption,
        "model_verdict": fv,
        "model_note": "cannot support a decision" if fv == "UNRELIABLE" else "",
        "model_color": fcol,
        "im_verdict": iv,
        "im_note": "rewrite before reissue" if "NOT FIT" in iv else "",
        "im_color": icol,
        "founder_state": founder_state,
        "hidden_value": hidden["value"],
        "hidden_label": hidden["label"],
        "hidden_color": hidden["color"],
        "hidden_strong": hidden["strong"],
        "hidden_line": hidden["line"],
        "hidden_instruction_like": hidden["instr"],
        "company_logo": company_logo,
        "company_logo_aspect": png_aspect(company_logo, 2.311) if company_logo else 1,
        "memo_md": memo_md,
    }


# ---------------------------------------------------------------------- check

def check(review_dir, d):
    """(errors, warnings). Errors block the build; warnings are printed."""
    errors, warnings = [], []

    def need(name):
        js = load(review_dir, name)
        if js is None:
            errors.append(f"{name} is missing")
            return None
        if isinstance(js, dict) and "_unreadable" in js:
            errors.append(f"{name} is not valid JSON ({js['_unreadable']})")
            return None
        if not isinstance(js, dict):
            errors.append(f"{name} must hold a JSON object")
            return None
        return js

    depth = d["depth"]
    prof = need("deal-profile.json")
    if prof is not None:
        if not prof.get("company"):
            errors.append("deal-profile.json has no company")
        if str(prof.get("stage", "")).lower() not in STAGE_WEIGHTS:
            errors.append("deal-profile.json stage must be one of "
                          + ", ".join(sorted(STAGE_WEIGHTS)))
        if depth not in ("full", "quick"):
            warnings.append("deal-profile.json depth should be full or quick; treated as full")
    if depth not in ("full", "quick"):
        depth = "full"

    for dim in DIMENSIONS:
        js = need(f"stage1-{dim}.json")
        if js is not None and not (is_num(js.get("score")) and 1 <= js["score"] <= 10):
            warnings.append(f"stage1-{dim}.json has no score from 1 to 10, so the "
                            "composite leaves it out")

    for name, allowed in (("stage1b-financial-model-audit.json", FIN_VERDICTS),
                          ("stage1b-im-review.json", IM_VERDICTS)):
        js = load(review_dir, name)
        if js is None:
            if depth == "full":
                warnings.append(f"{name} not found: shown as NOT RUN")
        elif str(as_dict(js).get("verdict", "")).lower() not in allowed:
            errors.append(f"{name} verdict must be one of {', '.join(sorted(allowed))}")

    fc = load(review_dir, "stage1b-founder-check.json")
    if fc is None and depth == "full":
        warnings.append("stage1b-founder-check.json not found")

    ai = load(review_dir, "stage1b-ai-usage.json")
    if ai is None:
        warnings.append("stage1b-ai-usage.json not found: AI usage shown as NOT RUN")
    elif not (is_num(as_dict(ai).get("score")) and 0 <= ai["score"] <= 10):
        errors.append("stage1b-ai-usage.json score must be a number from 0 to 10")
    elif ai.get("deduction_0_100") not in (None, ai_deduction(ai["score"])):
        warnings.append(f"stage1b-ai-usage.json deduction_0_100 is {ai.get('deduction_0_100')}"
                        f" but the rubric gives {ai_deduction(ai['score'])} for a score of "
                        f"{ai['score']}; the slide uses the rubric")

    if load(review_dir, os.path.join("extract", "hidden-text.json")) is None:
        warnings.append("extract/hidden-text.json not found: hidden text shown as NOT CHECKED")

    if depth == "full":
        for p in THESIS_PERSONAS + ["the-skeptic"]:
            js = need(f"stage2-persona-{p}.json")
            if js is not None and str(js.get("verdict", "")).lower() not in PERSONA_VERDICTS:
                errors.append(f"stage2-persona-{p}.json verdict must be one of "
                              + ", ".join(sorted(PERSONA_VERDICTS)))
        if d["skeptic"] in ("yes", "strong-yes"):
            warnings.append("the Skeptic voted yes, but its seat is mandated bearish")
        pm = need("stage3-premortem.json")
        if pm is not None and not str(pm.get("single_fatal_assumption", "")).strip():
            errors.append("stage3-premortem.json has no single_fatal_assumption")

    gaps = need("stage3-gap-analysis.json")
    if gaps is not None:
        s = gaps.get("summary")
        if not isinstance(s, dict):
            errors.append("stage3-gap-analysis.json needs a summary object")
        else:
            for k in ("fatal", "serious", "cosmetic"):
                if not isinstance(s.get(k), int) or isinstance(s.get(k), bool):
                    errors.append(f"stage3-gap-analysis.json summary.{k} must be a whole number")
            items = s.get("fatal_items")
            if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
                errors.append("stage3-gap-analysis.json summary.fatal_items must be a list of strings")
            else:
                if isinstance(s.get("fatal"), int) and s["fatal"] != len(items):
                    warnings.append(f"summary.fatal is {s['fatal']} but fatal_items lists {len(items)}")
                box = band_c_layout(bool(d["votes"]), bool(d["fatal_assumption"]))["gaps"]
                _, _, limit = fit_items(items[:4], box)
                long_items = sum(len(i) > limit for i in items[:4])
                if long_items:
                    warnings.append(f"{long_items} fatal gap item(s) will be shortened to about "
                                    f"{limit} characters on the slide; the memo keeps the full text")

    qs = need("stage3-questions.json")
    if qs is not None:
        a = qs.get("a_for_the_founders")
        if not (isinstance(a, list) and a and all(isinstance(x, dict) and str(x.get("q", "")).strip()
                                                  for x in a)):
            errors.append("stage3-questions.json a_for_the_founders must be a non-empty list of "
                          "objects with a q")
        else:
            top = [x["q"] for x in a[:5]]
            box = band_c_layout(bool(d["votes"]), bool(d["fatal_assumption"]))["questions"]
            _, _, limit = fit_items(top, box)
            long_q = sum(len(q) > limit for q in top)
            if long_q:
                warnings.append(f"{long_q} of the top five founder questions will be shortened "
                                f"to about {limit} characters on the slide")

    sc = need("stage4-scenario-scores.json")
    if sc is not None:
        v = as_dict(sc.get("result")).get("corrected_0_100")
        if not (is_num(v) and 0 <= v <= 100):
            errors.append("stage4-scenario-scores.json result.corrected_0_100 must be a number "
                          "from 0 to 100")

    if not os.path.exists(os.path.join(review_dir, "memo.md")):
        errors.append("memo.md is missing")
    else:
        found = REC_RE.findall(d["memo_md"])
        if len(found) != 1:
            errors.append("memo.md needs exactly one recommendation heading (# PASS, "
                          f"# FURTHER DUE DILIGENCE or # INVEST); found {len(found)}")

    rec = d["recommendation"]
    if rec == "INVEST":
        if is_num(d["fatal_n"]) and d["fatal_n"] > 0:
            errors.append("INVEST with unresolved fatal gaps")
        if d["model_verdict"] == "UNRELIABLE":
            errors.append("INVEST with an unreliable financial model")
        if depth == "quick":
            errors.append("INVEST at quick depth, where nothing was verified")
        if d["founder_state"] != "ran":
            errors.append("INVEST without a founder check that ran with web access")
        if d["hidden_instruction_like"]:
            errors.append("INVEST while hidden text that reads like instructions to an AI "
                          "is unexplained")
    if rec == "PASS" and d["yes_votes"] == 5:
        warnings.append("PASS against five yes votes: confirm memo §13 rebuts their top reasons")
    return errors, warnings, depth


def report(review_dir, d, errors, warnings, depth):
    print(f"check: {review_dir}")
    rows = [
        ("recommendation", d["recommendation"]),
        ("depth", depth),
        ("investability", f"{d['score100']}/100 (composite {d['composite']} on "
                          f"{d['stage_short']} weights"
                          + (f", AI usage deduction {d['ai_deduction']}" if d["ai_deduction"] else "")
                          + ")"),
        ("documents corrected", d["corrected_text"]),
        ("thesis panel", d["vote_headline"] + (f", skeptic {d['skeptic']}" if d["skeptic"] else "")),
        ("gaps", f"{d['fatal_n']} fatal, {d['serious_n']} serious, {d['cosmetic_n']} cosmetic"),
        ("financial model", d["model_verdict"]),
        ("memorandum", d["im_verdict"]),
        ("founder check", d["founder_state"]),
        ("AI usage", d["ai_value"].replace("  ", " ")),
        ("hidden text", d["hidden_line"]),
    ]
    for k, v in rows:
        print(f"  {k:20} {v}")
    if errors:
        print(f"errors ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
    if warnings:
        print(f"warnings ({len(warnings)}):")
        for w in warnings:
            print(f"  - {w}")
    print("result: " + ("FAIL" if errors else "PASS"))


def main():
    if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    only = None
    if "--only" in argv:
        i = argv.index("--only")
        only = argv[i + 1] if i + 1 < len(argv) else None
        argv = argv[:i] + argv[i + 2:]
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    review_dir = args[0]
    if not os.path.isdir(review_dir):
        print(f"error: not a directory: {review_dir}")
        return 1
    d = extract(review_dir)
    errors, warnings, depth = check(review_dir, d)
    report(review_dir, d, errors, warnings, depth)
    if "--check" in flags:
        return 1 if errors else 0
    if errors and "--force" not in flags:
        print("\nnot built: fix the errors above and run again (or pass --force to build anyway)")
        return 1

    made, failed = [], []

    def attempt(fn, *a):
        try:
            made.append(fn(*a))
        except PermissionError as e:
            failed.append(os.path.basename(getattr(e, "filename", "") or "output"))

    if only in (None, "docx"):
        if not d["memo_md"]:
            print("warning: memo.md not found - skipping memo.docx")
        else:
            attempt(build_docx, review_dir, d["memo_md"], d)
    if only in (None, "pptx"):
        attempt(build_pptx, review_dir, d)

    for p in made:
        print(f"wrote {p}  ({os.path.getsize(p):,} bytes)")
    if failed:
        print(f"\nerror: could not write {', '.join(failed)} - the file is locked.\n"
              "It is almost certainly open in Word or PowerPoint. Close it and re-run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
