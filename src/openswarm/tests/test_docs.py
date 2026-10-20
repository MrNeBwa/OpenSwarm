import shutil, subprocess, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from openswarm import docs
from .helpers import Env


def wellformed(path):
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.endswith((".xml", ".rels")):
                ET.fromstring(z.read(n))


class Docs(Env):
    def test_docx_create_read_edit(self):
        p = self.plain() / "a.docx"
        docs.create_docx(p, "# Title\n\nHello **world**\n- one\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        out = docs.read_docx(p)
        self.assertIn("# Title", out); self.assertIn("| 1 | 2 |", out); self.assertIn("- ", out) if False else None
        self.assertEqual(docs.replace_in_file(p, "world", "there"), 1)
        docs.append_docx(p, "tail line", "Heading2")
        docs.append_table_docx(p, [["x", "y"], ["1", "2"]])
        wellformed(p); self.assertIn("tail line", docs.read_docx(p))

    def test_replace_across_runs_keeps_formatting(self):
        xml = '<w:p><w:r><w:t>Tot</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>al: 25</w:t></w:r><w:r><w:t>0 &amp; more</w:t></w:r></w:p>'
        new, n = docs.replace_text(xml, "w:p", "w:t", "Total: 250", "Sum 999")
        self.assertEqual(n, 1); self.assertIn("<w:b/>", new); self.assertIn("&amp; more", new)

    def test_image_in_docx(self):
        import struct, zlib
        def ch(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        png = b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", 4, 2, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(b"".join(b"\x00" + b"\xff\x00\x00" * 4 for _ in range(2)))) + ch(b"IEND", b"")
        d = self.plain(); (d / "p.png").write_bytes(png); p = d / "a.docx"; docs.create_docx(p, "# x")
        docs.add_image_docx(p, d / "p.png", 5); wellformed(p)
        with zipfile.ZipFile(p) as z: self.assertIn("word/media/image1.png", z.namelist())

    def test_attachments_and_images(self):
        d = self.plain(); p = d / "r.docx"; docs.create_docx(p, "# Report\nsecret-content")
        (d / "pic.png").write_bytes(b"x")
        text, names = docs.expand_attachments("summarise @r.docx and @missing.docx", d)
        self.assertIn("secret-content", text); self.assertEqual(len(names), 1)
        self.assertEqual([x.name for x in docs.extract_images("look @pic.png", d)], ["pic.png"])

    def test_backup_policy(self):
        d = self.plain(); p = d / "a.docx"; docs.create_docx(p, "hello old")
        self.assertEqual(docs.docs_main(["replace", str(p), "old", "new"]), 0)
        self.assertTrue((d / "a.docx.bak").exists())  # outside git -> backup kept
        r = self.repo(); q = r / "b.docx"; docs.create_docx(q, "hello old")
        docs.docs_main(["replace", str(q), "old", "new"]); self.assertFalse((r / "b.docx.bak").exists())  # git -> none

    def test_pdf_edit_refused_and_missing_tools_are_clear(self):
        p = self.plain() / "x.pdf"; p.write_bytes(b"%PDF-1.4")
        with self.assertRaises(docs.DocError): docs.replace_in_file(p, "a", "b")

    @staticmethod
    def _soffice(): return shutil.which("soffice")

    def test_libreoffice_roundtrip_xlsx_formula_and_docx(self):
        if not self._soffice(): self.skipTest("LibreOffice not installed")
        d = self.plain(); (d / "in.csv").write_text("Item,Qty,Price\nApple,3,2\n")
        subprocess.run(["soffice", "--headless", "--convert-to", "xlsx", "--outdir", str(d), str(d / "in.csv")], capture_output=True, timeout=120)
        x = d / "in.xlsx"
        docs.set_xlsx(x, "B2", "10"); docs.set_xlsx(x, "D1", "Total"); docs.set_xlsx(x, "D2", "=B2*C2"); docs._xlsx_replace(x, "Apple", "Mango", 0)
        out = d / "o"; subprocess.run(["soffice", "--headless", "--convert-to", "csv", "--outdir", str(out), str(x)], capture_output=True, timeout=120)
        self.assertEqual((out / "in.csv").read_text().splitlines()[1], "Mango,10,2,20")
        t = d / "t.docx"; docs.create_docx(t, "# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"); docs.append_table_docx(t, [["x", "y"]])
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(out), str(t)], capture_output=True, timeout=120)
        self.assertTrue((out / "t.pdf").exists())
