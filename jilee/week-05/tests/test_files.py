"""첨부파일(올리기 · 목록 · 내려받기 위치 · 지우기 · 이름 정리 · 형식과 크기 제한)을 임시 폴더에서 검증한다."""

import json
import shutil
import unittest
from unittest import mock

from agent import files
from tests.helpers import CID, PID, make_project

PDF = b"%PDF-1.4 sample"


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.patches = make_project()
        for p in self.patches:
            p.start()
        self.root = self.tmp / "customers" / CID / "projects" / PID / "files"

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_list_path_remove(self):
        e = files.save(CID, PID, 1, "견적서 v2.PDF", PDF, who="이정인", note="1차 견적")
        self.assertEqual((e["name"], e["size"], e["kind"], e["meeting_no"], e["uploaded_by"], e["note"]),
                         ("견적서 v2.pdf", len(PDF), "document", 1, "이정인", "1차 견적"))
        self.assertEqual((self.root / "01" / "견적서 v2.pdf").read_bytes(), PDF)
        self.assertEqual([f["name"] for f in files.listing(CID, PID, 1)], ["견적서 v2.pdf"])
        self.assertEqual(files.path(CID, PID, 1, "견적서 v2.pdf").read_bytes(), PDF)
        self.assertEqual(files.content_type("견적서 v2.pdf"), "application/pdf")

        files.remove(CID, PID, 1, "견적서 v2.pdf", "이정인")
        self.assertEqual(files.listing(CID, PID, 1), [])
        self.assertFalse((self.root / "01" / "견적서 v2.pdf").exists())
        removed = json.loads((self.root / "01" / "index.json").read_text(encoding="utf-8"))["removed"]
        self.assertEqual((removed[0]["name"], removed[0]["removed_by"]), ("견적서 v2.pdf", "이정인"))  # 누가 지웠는지 남는다
        with self.assertRaises(FileNotFoundError):
            files.path(CID, PID, 1, "견적서 v2.pdf")

    def test_meeting_and_project_files_are_kept_apart(self):
        files.save(CID, PID, 1, "명함.jpg", b"\xff\xd8jpeg")
        files.save(CID, PID, 2, "현장.png", b"\x89PNG")
        files.save(CID, PID, None, "사양서.docx", b"PKdocx")
        self.assertEqual([f["name"] for f in files.listing(CID, PID, 1)], ["명함.jpg"])
        self.assertEqual([(f["name"], f["meeting_no"], f["kind"]) for f in files.listing(CID, PID, None)], [("사양서.docx", None, "document")])
        self.assertEqual({f["name"] for f in files.listing(CID, PID)}, {"명함.jpg", "현장.png", "사양서.docx"})
        self.assertTrue((self.root / "project" / "사양서.docx").exists())
        self.assertEqual(files.listing(CID, "P2"), [])  # 다른 프로젝트에는 없다
        with self.assertRaises(FileNotFoundError):
            files.path(CID, PID, 2, "명함.jpg")           # 1차에 붙은 파일은 2차에서 찾지 못한다

    def test_names_are_cleaned_and_cannot_escape_the_folder(self):
        self.assertEqual(files.save(CID, PID, 1, "..\\..\\x.pdf", PDF)["name"], "x.pdf")
        self.assertEqual(files.save(CID, PID, 1, "a/b.pdf", PDF)["name"], "b.pdf")
        self.assertEqual(files.save(CID, PID, 1, "  회의\x00 사진<1>.PNG ", b"\x89PNG")["name"], "회의 사진_1_.png")
        long = files.save(CID, PID, 1, "가" * 300 + ".pdf", PDF)["name"]
        self.assertEqual((len(long), long[-4:]), (files.MAX_NAME, ".pdf"))
        self.assertEqual({p.parent.name for p in self.root.rglob("*.pdf")}, {"01"})  # 전부 그 회차 폴더 안에만 있다
        for bad in ("", "   ", ".pdf", "..", "이름없음"):
            with self.assertRaises(ValueError):
                files.save(CID, PID, 1, bad, PDF)
        for escape in ("../01/x.pdf", "..\\project\\x.pdf", "index.json", ".."):
            with self.assertRaises(FileNotFoundError):
                files.path(CID, PID, 1, escape)

    def test_same_name_gets_a_number(self):
        names = [files.save(CID, PID, 1, "견적서.pdf", PDF)["name"] for _ in range(3)]
        self.assertEqual(names, ["견적서.pdf", "견적서 (2).pdf", "견적서 (3).pdf"])
        self.assertEqual([f["name"] for f in files.listing(CID, PID, 1)], ["견적서 (3).pdf", "견적서 (2).pdf", "견적서.pdf"])  # 최근 것부터

    def test_only_documents_images_audio_and_never_executables(self):
        for name in ("x.exe", "x.pdf.exe", "x.exe.pdf", "run.bat", "page.html", "a.zip", "스크립트.ps1.txt"):
            with self.assertRaises(ValueError, msg=name):
                files.save(CID, PID, 1, name, PDF)
        with self.assertRaisesRegex(ValueError, "실행 파일"):
            files.save(CID, PID, 1, "견적서.pdf", b"MZ\x90\x00 pretend exe")  # 확장자만 바꾼 실행 파일
        with self.assertRaises(ValueError):
            files.save(CID, PID, 1, "빈파일.pdf", b"")
        self.assertEqual(files.save(CID, PID, 1, "녹음.M4A", b"audio")["kind"], "audio")
        self.assertEqual(files.content_type("녹음.m4a"), "audio/mp4")
        self.assertEqual(files.content_type("알수없음.xyz"), "application/octet-stream")
        self.assertEqual([f["name"] for f in files.listing(CID, PID)], ["녹음.m4a"])  # 거부된 것은 아무것도 남지 않는다

    def test_size_limits(self):
        with mock.patch.object(files, "MAX_FILE_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "너무 큽니다"):
                files.save(CID, PID, 1, "큰파일.pdf", b"x" * 11)
        with mock.patch.object(files, "MAX_PROJECT_BYTES", 25):
            files.save(CID, PID, 1, "a.pdf", b"x" * 10)
            files.save(CID, PID, None, "b.pdf", b"x" * 10)
            with self.assertRaisesRegex(ValueError, "첨부 용량"):   # 회차와 프로젝트 첨부를 합쳐서 센다
                files.save(CID, PID, 2, "c.pdf", b"x" * 10)
            files.remove(CID, PID, 1, "a.pdf", "이정인")
            self.assertEqual(files.save(CID, PID, 2, "c.pdf", b"x" * 10)["name"], "c.pdf")  # 지우면 자리가 난다

    def test_bad_meeting_number(self):
        with self.assertRaises(ValueError):
            files.save(CID, PID, 0, "a.pdf", PDF)


if __name__ == "__main__":
    unittest.main()
