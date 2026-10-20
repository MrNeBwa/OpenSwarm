"""Stdlib-only reader/editor for .docx .xlsx .pptx .pdf (+ plain text) and `@file` prompt attachments.

Exposed to agents as `openswarm doc ...` (they call it through opencode's bash tool).
XML is edited as text (never re-serialised) so Word/Excel/PowerPoint keep every namespace and style.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape, unescape

OFFICE_EXT = {".docx", ".xlsx", ".pptx"}
ATTACH_EXT = OFFICE_EXT | {".pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class DocError(RuntimeError):
    pass


def _un(s: str) -> str:
    return unescape(s, {"&quot;": '"', "&apos;": "'"})


def _esc(s: str) -> str:
    return escape(s)


# ------------------------------------------------------------------ zip plumbing
def _member(z: zipfile.ZipFile, name: str) -> str:
    try:
        return z.read(name).decode("utf-8")
    except KeyError as exc:
        raise DocError(f"{name} not found - is this a valid Office file?") from exc


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, FileNotFoundError, IsADirectoryError) as exc:
        raise DocError(f"cannot open {path}: {exc}") from exc


def _b(x: str | bytes) -> bytes:
    return x if isinstance(x, bytes) else x.encode("utf-8")


def _rewrite(path: Path, changes: dict[str, str | bytes]) -> None:
    """Atomically rewrite a zip, replacing/adding the given members."""
    fd, tmp = tempfile.mkstemp(suffix=path.suffix, dir=str(path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            existing = set(zin.namelist())
            for item in zin.infolist():
                data = _b(changes[item.filename]) if item.filename in changes else zin.read(item.filename)
                zout.writestr(item, data)
            for name, content in changes.items():
                if name not in existing:
                    zout.writestr(name, _b(content))
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _in_git(path: Path) -> bool:
    return any((p / ".git").exists() for p in [path.resolve().parent, *path.resolve().parents])


def _backup(path: Path, mode: str) -> None:
    if mode == "no" or (mode == "auto" and _in_git(path)):
        return
    bak = path.with_name(path.name + ".bak")
    if not bak.exists():
        shutil.copy2(path, bak)


# ---------------------------------------------------- run-aware text replacement
def _replace_runs(texts: list[str], old: str, new: str, limit: int) -> tuple[list[str], int]:
    """Replace `old` across run boundaries; the replacement lands in the run where the match starts."""
    joined = "".join(texts)
    owner = [i for i, t in enumerate(texts) for _ in t]
    out: list[list[str]] = [[] for _ in texts]
    pos = n = 0
    while True:
        idx = joined.find(old, pos)
        if idx == -1 or (limit and n >= limit):
            break
        for k in range(pos, idx):
            out[owner[k]].append(joined[k])
        out[owner[idx]].append(new)
        pos, n = idx + len(old), n + 1
    for k in range(pos, len(joined)):
        out[owner[k]].append(joined[k])
    return ["".join(o) for o in out], n


def replace_text(xml: str, ptag: str, ttag: str, old: str, new: str, count: int = 0, preserve: bool = True) -> tuple[str, int]:
    pre = re.compile(rf"<{ptag}(?:\s[^>]*)?(?<!/)>.*?</{ptag}>", re.S)
    tre = re.compile(rf"(<{ttag}(?:\s[^>]*)?>)(.*?)(</{ttag}>)", re.S)
    total = 0

    def fix(m: re.Match) -> str:
        nonlocal total
        p = m.group(0)
        if count and total >= count:
            return p
        runs = list(tre.finditer(p))
        if not runs:
            return p
        texts = [_un(r.group(2)) for r in runs]
        if old not in "".join(texts):
            return p
        new_texts, n = _replace_runs(texts, old, new, (count - total) if count else 0)
        if n == 0:
            return p
        total += n
        out, last = [], 0
        for r, t in zip(runs, new_texts):
            out.append(p[last:r.start()])
            tag = r.group(1)
            if preserve and "xml:space" not in tag:
                tag = tag[:-1] + ' xml:space="preserve">'
            out.append(tag + _esc(t) + r.group(3))
            last = r.end()
        out.append(p[last:])
        return "".join(out)

    return pre.sub(fix, xml), total


# ----------------------------------------------------------------------- DOCX
_WTOK = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>|(<w:tab\s*/>)|(<w:br\b[^>]*/>)", re.S)
_WP = re.compile(r"<w:p(?:\s[^>]*)?(?<!/)>.*?</w:p>", re.S)


def _ptext(p: str) -> str:
    return "".join(
        _un(m.group(1)) if m.group(1) is not None else ("\t" if m.group(2) else "\n") for m in _WTOK.finditer(p)
    )


def read_docx(path: Path) -> str:
    with _open_zip(path) as z:
        xml = _member(z, "word/document.xml")
    out: list[str] = []
    for part in re.split(r"(<w:tbl(?:\s[^>]*)?>.*?</w:tbl>)", xml, flags=re.S):
        if part.startswith("<w:tbl"):
            for i, row in enumerate(re.findall(r"<w:tr(?:\s[^>]*)?>.*?</w:tr>", part, re.S)):
                cells = []
                for cell in re.findall(r"<w:tc(?:\s[^>]*)?>.*?</w:tc>", row, re.S):
                    txt = " ".join(t for t in (_ptext(p).strip() for p in _WP.findall(cell)) if t)
                    cells.append(txt.replace("|", "\\|"))
                out.append("| " + " | ".join(cells) + " |")
                if i == 0:
                    out.append("|" + "---|" * len(cells))
            out.append("")
            continue
        for p in _WP.findall(part):
            text = _ptext(p).strip()
            if not text:
                continue
            style = re.search(r'<w:pStyle\s+w:val="([^"]+)"', p)
            sname = style.group(1) if style else ""
            h = re.fullmatch(r"[Hh]eading\s?(\d)", sname)
            if h:
                text = "#" * min(int(h.group(1)), 6) + " " + text
            elif sname == "Title":
                text = "# " + text
            elif "<w:numPr>" in p:
                text = "- " + text
            out.append(text)
    return "\n".join(out)


def _docx_replace(path: Path, old: str, new: str, count: int) -> int:
    with _open_zip(path) as z:
        parts = [n for n in z.namelist() if re.fullmatch(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml", n)]
        changes, total = {}, 0
        for name in parts:
            xml, n = replace_text(_member(z, name), "w:p", "w:t", old, new, (count - total) if count else 0)
            if n:
                changes[name], total = xml, total + n
            if count and total >= count:
                break
    if changes:
        _rewrite(path, changes)
    return total


def append_docx(path: Path, text: str, style: str = "") -> int:
    if style and not re.fullmatch(r"[A-Za-z0-9_-]+", style):
        raise DocError("invalid style name")
    with _open_zip(path) as z:
        xml = _member(z, "word/document.xml")
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    lines = text.split("\n")
    block = "".join(f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{_esc(ln)}</w:t></w:r></w:p>' for ln in lines)
    sect, last_end, body_end = xml.rfind("<w:sectPr"), max(xml.rfind("</w:p>"), xml.rfind("</w:tbl>")), xml.rfind("</w:body>")
    at = sect if sect != -1 and sect > last_end else body_end
    if at == -1:
        raise DocError("word/document.xml has no <w:body>")
    _rewrite(path, {"word/document.xml": xml[:at] + block + xml[at:]})
    return len(lines)


_DOCX_STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{W_NS}">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="22"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    + "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{i}"><w:name w:val="heading {i}"/><w:basedOn w:val="Normal"/>'
        f'<w:next w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="{360 - 80 * i}" w:after="120"/><w:outlineLvl w:val="{i - 1}"/></w:pPr>'
        f'<w:rPr><w:b/><w:sz w:val="{40 - 6 * i}"/></w:rPr></w:style>'
        for i in (1, 2, 3)
    )
    + "</w:styles>"
)


def _runs(line: str) -> str:
    out = []
    for chunk in re.split(r"(\*\*.+?\*\*)", line):
        if not chunk:
            continue
        bold = chunk.startswith("**") and chunk.endswith("**") and len(chunk) > 4
        txt = chunk[2:-2] if bold else chunk
        out.append(f'<w:r>{"<w:rPr><w:b/></w:rPr>" if bold else ""}<w:t xml:space="preserve">{_esc(txt)}</w:t></w:r>')
    return "".join(out)


def create_docx(path: Path, text: str) -> int:
    paras = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        i += 1
        if line.lstrip().startswith("|"):  # markdown pipe table
            rows = [line]
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i].rstrip())
                i += 1
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows if not re.fullmatch(r"[\s|:\-]+", r)]
            if cells:
                paras.append(_table_xml(cells, True) + "<w:p/>")
            continue
        if not line.strip():
            continue
        h = re.match(r"(#{1,3})\s+(.*)", line)
        b = re.match(r"\s*[-*]\s+(.*)", line)
        if h:
            paras.append(f'<w:p><w:pPr><w:pStyle w:val="Heading{len(h.group(1))}"/></w:pPr>{_runs(h.group(2))}</w:p>')
        elif b:
            paras.append(f'<w:p><w:pPr><w:ind w:left="360" w:hanging="220"/></w:pPr>{_runs("• " + b.group(1))}</w:p>')
        else:
            paras.append(f"<w:p>{_runs(line)}</w:p>")
    doc = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W_NS}"><w:body>'
        + "".join(paras)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>'
    )
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{R_NS}/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    drels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{R_NS}/styles" Target="styles.xml"/></Relationships>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", drels)
        z.writestr("word/styles.xml", _DOCX_STYLES)
    return len(paras)


# ----------------------------------------------------------------------- XLSX
def _col2n(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n


def _split_ref(ref: str) -> tuple[str, int]:
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", ref.strip())
    if not m:
        raise DocError(f"bad cell reference: {ref!r} (expected e.g. B3 or Sheet1!B3)")
    return m.group(1).upper(), int(m.group(2))


def _xlsx_sheets(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    wb = ET.fromstring(_member(z, "xl/workbook.xml"))
    rels = ET.fromstring(_member(z, "xl/_rels/workbook.xml.rels"))
    rmap = {r.get("Id"): r.get("Target", "") for r in rels}
    out = []
    for s in wb.iter(f"{{{S_NS}}}sheet"):
        tgt = rmap.get(s.get(f"{{{R_NS}}}id"), "")
        out.append((s.get("name", ""), tgt.lstrip("/") if tgt.startswith("/") else "xl/" + tgt))
    return out


def _pick_sheet(z: zipfile.ZipFile, name: str | None) -> tuple[str, str]:
    sheets = _xlsx_sheets(z)
    if not sheets:
        raise DocError("workbook has no sheets")
    if not name:
        return sheets[0]
    for n, part in sheets:
        if n.lower() == name.lower():
            return n, part
    raise DocError(f"sheet {name!r} not found; available: {', '.join(n for n, _ in sheets)}")


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    out = []
    for si in ET.fromstring(_member(z, "xl/sharedStrings.xml")).iter(f"{{{S_NS}}}si"):
        parts = []
        for ch in si:
            if ch.tag == f"{{{S_NS}}}t":
                parts.append(ch.text or "")
            elif ch.tag == f"{{{S_NS}}}r":
                parts += [t.text or "" for t in ch if t.tag == f"{{{S_NS}}}t"]
        out.append("".join(parts))
    return out


def _n2col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def read_xlsx(path: Path, sheet: str | None = None, max_rows: int = 200) -> str:
    with _open_zip(path) as z:
        shared = _shared_strings(z)
        targets = [_pick_sheet(z, sheet)] if sheet else _xlsx_sheets(z)
        blocks = []
        for name, part in targets:
            root = ET.fromstring(_member(z, part))
            grid: dict[int, dict[int, str]] = {}
            for c in root.iter(f"{{{S_NS}}}c"):
                col, row = _split_ref(c.get("r", "A1"))
                t, v, f = c.get("t"), c.find(f"{{{S_NS}}}v"), c.find(f"{{{S_NS}}}f")
                if t == "s" and v is not None and v.text is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{S_NS}}}t"))
                elif t == "b":
                    val = "TRUE" if (v is not None and v.text == "1") else "FALSE"
                else:
                    val = v.text if (v is not None and v.text is not None) else ""
                if f is not None and f.text:
                    val = f"{val} [={f.text}]"
                grid.setdefault(row, {})[_col2n(col)] = re.sub(r"[\t\r\n]+", " ", val)
            if not grid:
                blocks.append(f"## Sheet: {name} (empty)")
                continue
            width = max(max(r) for r in grid.values())
            lines = [f"## Sheet: {name}", "row\t" + "\t".join(_n2col(i) for i in range(1, width + 1))]
            for rn in sorted(grid)[:max_rows]:
                lines.append(f"{rn}\t" + "\t".join(grid[rn].get(i, "") for i in range(1, width + 1)))
            if len(grid) > max_rows:
                lines.append(f"... {len(grid) - max_rows} more rows (use --max-rows)")
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


_ROW_RE = re.compile(r"<row\b([^>]*?)(?:/>|>(.*?)</row>)", re.S)
_CELL_RE = re.compile(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", re.S)


def _set_cell_xml(xml: str, ref: str, val: str, as_text: bool) -> tuple[str, bool]:
    col, rown = _split_ref(ref)
    ref = f"{col}{rown}"
    xml = re.sub(r"<sheetData\s*/>", "<sheetData></sheetData>", xml)
    sd = re.search(r"(<sheetData[^>]*>)(.*?)(</sheetData>)", xml, re.S)
    if not sd:
        raise DocError("worksheet has no <sheetData>")
    rows: list[list] = []  # [rownum, attrs, body, raw|None]
    for m in _ROW_RE.finditer(sd.group(2)):
        rm = re.search(r'\br="(\d+)"', m.group(1))
        if not rm:
            raise DocError("worksheet rows without r= attribute are not supported")
        rows.append([int(rm.group(1)), m.group(1), m.group(2) or "", m.group(0)])
    row = next((r for r in rows if r[0] == rown), None)
    if row is None:
        row = [rown, f' r="{rown}"', "", None]
        rows.append(row)
    cells = []
    for m in _CELL_RE.finditer(row[2]):
        cm = re.search(r'\br="([A-Z]+)\d+"', m.group(1))
        cells.append([_col2n(cm.group(1)) if cm else 0, m.group(1), m.group(0)])
    old = next((c for c in cells if c[0] == _col2n(col)), None)
    style = ""
    if old:
        sm = re.search(r'\ss="(\d+)"', old[1])
        style = f' s="{sm.group(1)}"' if sm else ""
    is_formula = val.startswith("=") and not as_text
    if is_formula:
        new_cell = f'<c r="{ref}"{style}><f>{_esc(val[1:])}</f></c>'
    elif not as_text and re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", val):
        new_cell = f'<c r="{ref}"{style}><v>{val}</v></c>'
    else:
        new_cell = f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">{_esc(val)}</t></is></c>'
    if old:
        old[2] = new_cell
    else:
        cells.append([_col2n(col), "", new_cell])
    cells.sort(key=lambda c: c[0])
    attrs = re.sub(r'\sspans="[^"]*"', "", row[1]).rstrip("/")
    row[3] = f"<row{attrs}>" + "".join(c[2] for c in cells) + "</row>"
    rows.sort(key=lambda r: r[0])
    body = "".join(r[3] for r in rows)
    return xml[: sd.start(2)] + body + xml[sd.end(2):], is_formula


def _force_recalc(wb: str) -> str:
    if "<calcPr" in wb:
        return wb if "fullCalcOnLoad" in wb else re.sub(r"<calcPr\b", '<calcPr fullCalcOnLoad="1"', wb, count=1)
    for anchor in ("</definedNames>", "</sheets>"):
        if anchor in wb:
            return wb.replace(anchor, anchor + '<calcPr fullCalcOnLoad="1"/>', 1)
    return wb


def set_xlsx(path: Path, ref: str, value: str, as_text: bool = False) -> str:
    sheet = None
    if "!" in ref:
        sheet, ref = ref.rsplit("!", 1)
        sheet = sheet.strip("'")
    with _open_zip(path) as z:
        name, part = _pick_sheet(z, sheet)
        xml, formula = _set_cell_xml(_member(z, part), ref, value, as_text)
        changes = {part: xml}
        if formula:
            changes["xl/workbook.xml"] = _force_recalc(_member(z, "xl/workbook.xml"))
    _rewrite(path, changes)
    return name


def _xlsx_replace(path: Path, old: str, new: str, count: int) -> int:
    with _open_zip(path) as z:
        changes, total = {}, 0
        if "xl/sharedStrings.xml" in z.namelist():
            xml, n = replace_text(_member(z, "xl/sharedStrings.xml"), "si", "t", old, new, count)
            if n:
                changes["xl/sharedStrings.xml"], total = xml, n
        for _, part in _xlsx_sheets(z):
            if count and total >= count:
                break
            xml, n = replace_text(_member(z, part), "is", "t", old, new, (count - total) if count else 0)
            if n:
                changes[part], total = xml, total + n
    if changes:
        _rewrite(path, changes)
    return total


# ----------------------------------------------------------------------- PPTX
def _slide_parts(z: zipfile.ZipFile, kind: str) -> list[tuple[int, str]]:
    pat = re.compile(rf"ppt/{kind}/{kind[:-1]}(\d+)\.xml")
    found = [(int(m.group(1)), n) for n in z.namelist() if (m := pat.fullmatch(n))]
    return sorted(found)


def _a_paras(xml: str) -> list[str]:
    out = []
    for p in re.findall(r"<a:p(?:\s[^>]*)?(?<!/)>.*?</a:p>", xml, re.S):
        txt = "".join(_un(t) for t in re.findall(r"<a:t(?:\s[^>]*)?>(.*?)</a:t>", p, re.S))
        if txt.strip():
            out.append(txt)
    return out


def read_pptx(path: Path) -> str:
    with _open_zip(path) as z:
        notes = dict(_slide_parts(z, "notesSlides"))
        blocks = []
        for n, part in _slide_parts(z, "slides"):
            lines = [f"## Slide {n}", *_a_paras(_member(z, part))]
            if n in notes and (nt := _a_paras(_member(z, notes[n]))):
                lines += ["Notes: " + " ".join(nt)]
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _pptx_replace(path: Path, old: str, new: str, count: int) -> int:
    with _open_zip(path) as z:
        parts = [p for _, p in _slide_parts(z, "slides")] + [p for _, p in _slide_parts(z, "notesSlides")]
        changes, total = {}, 0
        for part in parts:
            xml, n = replace_text(_member(z, part), "a:p", "a:t", old, new, (count - total) if count else 0, preserve=False)
            if n:
                changes[part], total = xml, total + n
            if count and total >= count:
                break
    if changes:
        _rewrite(path, changes)
    return total


# ------------------------------------------------------------------ PDF / plain
def read_pdf(path: Path, pages: str | None = None) -> str:
    exe = shutil.which("pdftotext")
    if exe:
        cmd = [exe, "-layout"]
        if pages:
            a, _, b = pages.partition("-")
            cmd += ["-f", a, "-l", b or a]
        r = subprocess.run([*cmd, str(path), "-"], capture_output=True, text=True)
        if r.returncode != 0:
            raise DocError(r.stderr.strip() or "pdftotext failed")
        return r.stdout
    try:
        import pypdf  # type: ignore
    except ImportError as exc:
        raise DocError("PDF reading needs poppler (sudo pacman -S poppler) or `pip install pypdf`") from exc
    reader = pypdf.PdfReader(str(path))
    lo, hi = 1, len(reader.pages)
    if pages:
        a, _, b = pages.partition("-")
        lo, hi = int(a), min(int(b or a), hi)
    return "\n\f".join((reader.pages[i].extract_text() or "") for i in range(lo - 1, hi))


def read_file(path: Path, sheet: str | None = None, pages: str | None = None, max_rows: int = 200) -> str:
    if not path.is_file():
        raise DocError(f"{path}: no such file")
    ext = path.suffix.lower()
    if ext == ".docx":
        return read_docx(path)
    if ext == ".xlsx":
        return read_xlsx(path, sheet, max_rows)
    if ext == ".pptx":
        return read_pptx(path)
    if ext == ".pdf":
        return read_pdf(path, pages)
    return path.read_text(encoding="utf-8", errors="replace")


def replace_in_file(path: Path, old: str, new: str, count: int = 0) -> int:
    if not old:
        raise DocError("OLD must not be empty")
    ext = path.suffix.lower()
    if ext == ".docx":
        return _docx_replace(path, old, new, count)
    if ext == ".xlsx":
        return _xlsx_replace(path, old, new, count)
    if ext == ".pptx":
        return _pptx_replace(path, old, new, count)
    if ext == ".pdf":
        raise DocError("editing PDFs is not supported; edit the source document and re-export")
    text = path.read_text(encoding="utf-8")
    n = text.count(old) if not count else min(text.count(old), count)
    if n:
        path.write_text(text.replace(old, new, count if count else -1), encoding="utf-8")
    return n


def info_file(path: Path) -> str:
    ext = path.suffix.lower()
    size = path.stat().st_size
    if ext == ".docx":
        txt = read_docx(path)
        with _open_zip(path) as z:
            tables = _member(z, "word/document.xml").count("<w:tbl>")
        return f"docx, {size} bytes, {len(txt.splitlines())} blocks, {len(txt.split())} words, {tables} tables"
    if ext == ".xlsx":
        with _open_zip(path) as z:
            return f"xlsx, {size} bytes, sheets: " + ", ".join(n for n, _ in _xlsx_sheets(z))
    if ext == ".pptx":
        with _open_zip(path) as z:
            return f"pptx, {size} bytes, {len(_slide_parts(z, 'slides'))} slides"
    return f"{ext or 'file'}, {size} bytes"


# ------------------------------------------------------- tables / images (DOCX)
def _table_xml(rows: list[list[str]], header: bool = True) -> str:
    cols = max(len(r) for r in rows)
    w = 9000 // cols
    border = "<w:tblBorders>" + "".join(
        f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="999999"/>' for s in ("top", "left", "bottom", "right", "insideH", "insideV")
    ) + "</w:tblBorders>"
    out = [f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>{border}</w:tblPr><w:tblGrid>' + f'<w:gridCol w:w="{w}"/>' * cols + "</w:tblGrid>"]
    for i, row in enumerate(rows):
        head = header and i == 0
        shade = '<w:shd w:val="clear" w:color="auto" w:fill="EEEEEE"/>' if head else ""
        rpr = "<w:rPr><w:b/></w:rPr>" if head else ""
        cells = "".join(
            f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>{shade}</w:tcPr><w:p><w:r>{rpr}<w:t xml:space="preserve">{_esc(c)}</w:t></w:r></w:p></w:tc>'
            for c in (row + [""] * (cols - len(row)))
        )
        out.append(f"<w:tr>{cells}</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def _insert_end(xml: str, block: str) -> str:
    sect, last_end, body_end = xml.rfind("<w:sectPr"), max(xml.rfind("</w:p>"), xml.rfind("</w:tbl>")), xml.rfind("</w:body>")
    at = sect if sect != -1 and sect > last_end else body_end
    if at == -1:
        raise DocError("word/document.xml has no <w:body>")
    return xml[:at] + block + xml[at:]


def append_table_docx(path: Path, rows: list[list[str]], header: bool = True) -> int:
    if not rows or not any(rows):
        raise DocError("no rows given")
    with _open_zip(path) as z:
        xml = _member(z, "word/document.xml")
    _rewrite(path, {"word/document.xml": _insert_end(xml, _table_xml(rows, header) + "<w:p/>")})
    return len(rows)


def _image_size(data: bytes) -> tuple[int, int]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", data[6:10])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise DocError("unsupported image (use PNG, JPEG or GIF)")


def add_image_docx(path: Path, image: Path, width_cm: float = 12.0) -> str:
    if not image.is_file():
        raise DocError(f"{image}: no such file")
    data = image.read_bytes()
    ext = {"png": "png", "gif": "gif", "jpg": "jpeg", "jpeg": "jpeg"}.get(image.suffix.lower().lstrip("."))
    if not ext:
        raise DocError("unsupported image type (png, jpg, gif)")
    pw, ph = _image_size(data)
    cx = int(width_cm * 360000)
    cy = int(cx * ph / pw)
    with _open_zip(path) as z:
        names = z.namelist()
        doc = _member(z, "word/document.xml")
        ct = _member(z, "[Content_Types].xml")
        rels = _member(z, "word/_rels/document.xml.rels") if "word/_rels/document.xml.rels" in names else (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>')
    n = 1 + sum(1 for x in names if x.startswith("word/media/"))
    ids = [int(m) for m in re.findall(r'Id="rId(\d+)"', rels)]
    rid = f"rId{max(ids, default=0) + 1}"
    target = f"media/image{n}.{ext if ext != 'jpeg' else 'jpg'}"
    rels = rels.replace("</Relationships>", f'<Relationship Id="{rid}" Type="{R_NS}/image" Target="{target}"/></Relationships>')
    if f'Extension="{ext}"' not in ct and f'Extension="{"jpg" if ext == "jpeg" else ext}"' not in ct:
        ct = re.sub(r"(<Types\b[^>]*>)", rf'\1<Default Extension="{"jpg" if ext == "jpeg" else ext}" ContentType="image/{ext}"/>', ct, count=1)
    para = (
        '<w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" xmlns:r="{R_NS}">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{n}" name="Picture {n}"/><wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
        f'<pic:nvPicPr><pic:cNvPr id="0" name="{_esc(image.name)}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )
    _rewrite(path, {"word/document.xml": _insert_end(doc, para), "word/_rels/document.xml.rels": rels,
                    "[Content_Types].xml": ct, f"word/{target}": data})
    return f"{pw}x{ph}px -> {width_cm}cm wide"


# ----------------------------------------------------------- export / OCR (external tools)
def _need(tool: str, hint: str) -> str:
    exe = shutil.which(tool)
    if not exe:
        raise DocError(f"{tool} not found: {hint}")
    return exe


def export_file(path: Path, to: str, outdir: Path | None = None) -> Path:
    """Convert a document. Built-in: .md/.txt -> .docx and .docx -> .md. soffice: office -> pdf/docx/...; pandoc: html/pdf/others."""
    to = to.lower().lstrip(".")
    src = path.suffix.lower()
    outdir = (outdir or path.parent).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{path.stem}.{to}"
    if not path.is_file():
        raise DocError(f"{path}: no such file")
    if src in (".md", ".markdown", ".txt") and to == "docx" and not shutil.which("pandoc"):
        create_docx(out, path.read_text(encoding="utf-8"))
        return out
    if src == ".docx" and to in ("md", "txt"):
        out.write_text(read_docx(path), encoding="utf-8")
        return out
    if src in (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".doc", ".xls", ".ppt", ".rtf") and to in ("pdf", "docx", "odt", "html", "txt", "xlsx", "pptx"):
        exe = shutil.which("soffice") or shutil.which("libreoffice") or _need("soffice", "install LibreOffice: sudo pacman -S libreoffice-fresh")
        r = subprocess.run([exe, "--headless", "--convert-to", to, "--outdir", str(outdir), str(path)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not out.exists():
            raise DocError(f"LibreOffice conversion failed: {(r.stderr or r.stdout).strip()[:300]}")
        return out
    if src in (".md", ".markdown", ".txt", ".html", ".docx") and to in ("docx", "pdf", "html", "md", "odt", "epub"):
        exe = _need("pandoc", "sudo pacman -S pandoc (pdf output also needs a PDF engine, or convert to .docx and export that to pdf)")
        r = subprocess.run([exe, str(path), "-o", str(out)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise DocError(f"pandoc failed: {r.stderr.strip()[:300]}")
        return out
    raise DocError(f"don't know how to convert {src} to .{to}")


def ocr_file(path: Path, lang: str = "eng") -> str:
    """Text from an image or scanned PDF via tesseract (+ pdftoppm for PDFs)."""
    if not path.is_file():
        raise DocError(f"{path}: no such file")
    tess = _need("tesseract", "sudo pacman -S tesseract tesseract-data-eng tesseract-data-rus")

    def one(img: Path) -> str:
        r = subprocess.run([tess, str(img), "stdout", "-l", lang], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise DocError(f"tesseract failed: {r.stderr.strip()[:300]}")
        return r.stdout

    if path.suffix.lower() == ".pdf":
        ppm = _need("pdftoppm", "sudo pacman -S poppler")
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([ppm, "-r", "200", "-png", str(path), f"{td}/p"], capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise DocError(f"pdftoppm failed: {r.stderr.strip()[:300]}")
            return "\n\f".join(one(p) for p in sorted(Path(td).glob("p*.png")))
    return one(path)


# ------------------------------------------------------------ prompt attachments
def expand_attachments(prompt: str, base: Path, limit: int = 20_000) -> tuple[str, list[str]]:
    """Inline the text of `@file.docx|xlsx|pptx|pdf` mentions so any model can 'see' them."""
    blocks, names = [], []
    for tok in re.findall(r'(?<!\S)@("[^"]+"|\S+)', prompt):
        raw = tok.strip('"').rstrip(",.;:!?)")
        path = Path(raw).expanduser()
        path = path if path.is_absolute() else base / path
        if path.suffix.lower() not in ATTACH_EXT or not path.is_file() or str(path) in names:
            continue
        try:
            text = read_file(path)
        except (DocError, OSError, ET.ParseError, zipfile.BadZipFile) as exc:
            text = f"(could not read: {exc})"
        if len(text) > limit:
            text = text[:limit] + f"\n... [truncated; use `openswarm doc read {path}` for the rest]"
        names.append(str(path))
        blocks.append(f"### Attached file: {path}\n(edit with `openswarm doc replace|append|set`)\n```\n{text}\n```")
    if not blocks:
        return prompt, []
    return prompt + "\n\n## Attachments\n" + "\n\n".join(blocks), names


def extract_images(prompt: str, base: Path) -> list[Path]:
    """Image files mentioned as @pic.png - passed to the model as real attachments (vision models)."""
    out: list[Path] = []
    for tok in re.findall(r'(?<!\S)@("[^"]+"|\S+)', prompt):
        path = Path(tok.strip('"').rstrip(",.;:!?)")).expanduser()
        path = path if path.is_absolute() else base / path
        if path.suffix.lower() in IMAGE_EXT and path.is_file() and path not in out:
            out.append(path)
    return out


# --------------------------------------------------------------------------- CLI
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openswarm doc", description="Read/edit .docx .xlsx .pptx .pdf from the shell")
    sub = p.add_subparsers(dest="cmd", required=True)

    def backup_args(sp: argparse.ArgumentParser) -> None:
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--backup", dest="backup", action="store_const", const="yes", help="always keep FILE.bak")
        g.add_argument("--no-backup", dest="backup", action="store_const", const="no")
        sp.set_defaults(backup="auto")  # auto = back up only outside git repos

    r = sub.add_parser("read", help="print file contents as text/markdown")
    r.add_argument("file", type=Path)
    r.add_argument("--sheet")
    r.add_argument("--pages", help="PDF page range, e.g. 2-5")
    r.add_argument("--max-rows", type=int, default=200)
    r.add_argument("--max-chars", type=int, default=60_000)
    rp = sub.add_parser("replace", help="replace text (works across formatting runs)")
    rp.add_argument("file", type=Path)
    rp.add_argument("old")
    rp.add_argument("new")
    rp.add_argument("--count", type=int, default=0, help="max replacements (0 = all)")
    backup_args(rp)
    ap = sub.add_parser("append", help="append paragraph(s) to a .docx or a text file")
    ap.add_argument("file", type=Path)
    ap.add_argument("text", nargs="?", help="text (default: stdin)")
    ap.add_argument("--style", default="", help="docx paragraph style, e.g. Heading1")
    backup_args(ap)
    st = sub.add_parser("set", help="set an .xlsx cell: Sheet1!B3 VALUE (=formula, number or text)")
    st.add_argument("file", type=Path)
    st.add_argument("ref")
    st.add_argument("value")
    st.add_argument("--as-text", action="store_true")
    backup_args(st)
    cr = sub.add_parser("create", help="create a .docx from text (# headings, - bullets, **bold**) or a text file")
    cr.add_argument("file", type=Path)
    cr.add_argument("--text", help="content (default: stdin)")
    cr.add_argument("--force", action="store_true", help="overwrite if exists")
    tb = sub.add_parser("table", help="append a table to a .docx from CSV (file, --rows or stdin)")
    tb.add_argument("file", type=Path)
    tb.add_argument("--csv", type=Path, help="CSV file (default: stdin)")
    tb.add_argument("--rows", help='inline rows: "a,b;c,d"')
    tb.add_argument("--no-header", action="store_true")
    backup_args(tb)
    im = sub.add_parser("image", help="append an image (png/jpg/gif) to a .docx")
    im.add_argument("file", type=Path)
    im.add_argument("image", type=Path)
    im.add_argument("--width-cm", type=float, default=12.0)
    backup_args(im)
    ex = sub.add_parser("export", help="convert: md->docx, docx->md (built in); office->pdf (LibreOffice); more via pandoc")
    ex.add_argument("file", type=Path)
    ex.add_argument("--to", required=True)
    ex.add_argument("--outdir", type=Path)
    oc = sub.add_parser("ocr", help="text from an image or scanned PDF (tesseract)")
    oc.add_argument("file", type=Path)
    oc.add_argument("--lang", default="eng", help="e.g. eng, rus, eng+rus")
    inf = sub.add_parser("info")
    inf.add_argument("file", type=Path)
    return p


def docs_main(argv: list[str]) -> int:
    a = _parser().parse_args(argv)
    try:
        if a.cmd == "read":
            text = read_file(a.file, a.sheet, a.pages, a.max_rows)
            if len(text) > a.max_chars:
                text = text[: a.max_chars] + f"\n... [truncated at {a.max_chars} chars; use --max-chars]"
            print(text)
        elif a.cmd == "info":
            print(info_file(a.file))
        elif a.cmd == "replace":
            if not a.file.is_file():
                raise DocError(f"{a.file}: no such file")
            _backup(a.file, a.backup)
            n = replace_in_file(a.file, a.old, a.new, a.count)
            print(f"{n} replacement(s) in {a.file}")
            return 0 if n else 1
        elif a.cmd == "append":
            text = a.text if a.text is not None else sys.stdin.read().rstrip("\n")
            if not a.file.is_file():
                raise DocError(f"{a.file}: no such file")
            _backup(a.file, a.backup)
            if a.file.suffix.lower() == ".docx":
                print(f"appended {append_docx(a.file, text, a.style)} paragraph(s) to {a.file}")
            elif a.file.suffix.lower() in OFFICE_EXT | {".pdf"}:
                raise DocError("append is supported for .docx and text files; use `set` for .xlsx")
            else:
                with a.file.open("a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
                print(f"appended to {a.file}")
        elif a.cmd == "set":
            if a.file.suffix.lower() != ".xlsx" or not a.file.is_file():
                raise DocError("set needs an existing .xlsx file")
            _backup(a.file, a.backup)
            print(f"{set_xlsx(a.file, a.ref, a.value, a.as_text)}: set {a.ref}")
        elif a.cmd == "table":
            if a.file.suffix.lower() != ".docx" or not a.file.is_file():
                raise DocError("table needs an existing .docx file")
            if a.rows:
                rows = [[c.strip() for c in r.split(",")] for r in a.rows.split(";") if r.strip()]
            else:
                raw = a.csv.read_text(encoding="utf-8") if a.csv else sys.stdin.read()
                rows = [r for r in csv.reader(io.StringIO(raw)) if r]
            _backup(a.file, a.backup)
            print(f"table with {append_table_docx(a.file, rows, not a.no_header)} row(s) appended to {a.file}")
        elif a.cmd == "image":
            if a.file.suffix.lower() != ".docx" or not a.file.is_file():
                raise DocError("image needs an existing .docx file")
            _backup(a.file, a.backup)
            print(f"image added ({add_image_docx(a.file, a.image, a.width_cm)})")
        elif a.cmd == "export":
            print(f"created {export_file(a.file, a.to, a.outdir)}")
        elif a.cmd == "ocr":
            print(ocr_file(a.file, a.lang))
        elif a.cmd == "create":
            if a.file.exists() and not a.force:
                raise DocError(f"{a.file} exists (use --force)")
            text = a.text if a.text is not None else sys.stdin.read()
            if a.file.suffix.lower() == ".docx":
                print(f"created {a.file} ({create_docx(a.file, text)} paragraphs)")
            else:
                a.file.parent.mkdir(parents=True, exist_ok=True)
                a.file.write_text(text, encoding="utf-8")
                print(f"created {a.file}")
    except (DocError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
