"""참고 사진: 응답 풀기와 저장 모양을 네트워크 없이 검증한다."""

import unittest

from agent import photos

SAMPLE = {"query": {"pages": {
    "2": {"index": 2, "title": "File:Diagram.svg", "imageinfo": [{"mime": "image/svg+xml", "thumburl": "https://x/t2.png"}]},
    "1": {"index": 1, "title": "File:Die casting cell.jpg", "imageinfo": [{
        "mime": "image/jpeg", "thumburl": "https://upload.example/thumb/1.jpg", "url": "https://upload.example/1.jpg",
        "descriptionurl": "https://commons.example/wiki/File:1.jpg",
        "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}, "Artist": {"value": '<a href="//x">Kim &amp; Lee</a>'}}}]},
    "3": {"index": 3, "title": "File:No thumb.jpg", "imageinfo": [{"mime": "image/jpeg"}]},
}}}


class PhotosTest(unittest.TestCase):
    def test_parse_keeps_photos_with_source_and_license(self):
        rows = photos.parse(SAMPLE)
        self.assertEqual(len(rows), 1)                                       # 그림(SVG)과 미리보기 없는 것은 뺀다
        r = rows[0]
        self.assertEqual((r["title"], r["license"], r["credit"], r["origin"]), ("Die casting cell", "CC BY-SA 4.0", "Kim & Lee", "Wikimedia Commons"))
        self.assertTrue(r["source"].startswith("https://commons.example/"))
        self.assertEqual(photos.parse({}), [])

    def test_clean_requires_urls_and_caps_the_count(self):
        ok = {"thumb": "https://a/t.jpg", "source": "https://a/page", "title": "t"}
        rows = photos.clean([ok] * 10, "이정인")
        self.assertEqual((len(rows), rows[0]["added_by"], rows[0]["license"]), (photos.MAX_PHOTOS, "이정인", ""))
        with self.assertRaises(ValueError):
            photos.clean([{"thumb": "javascript:alert(1)", "source": "https://a"}])
        with self.assertRaises(ValueError):
            photos.search("  ")


    def test_only_photos_matching_the_query_and_only_news_naming_the_company(self):
        self.assertTrue(photos.relevant({"title": "Magnesium die casting machine"}, "aluminium die casting housing"))
        self.assertFalse(photos.relevant({"title": "Combat inspection performed"}, "machine vision inspection"))
        self.assertFalse(photos.relevant({"title": "Aluminium bar surface etched"}, "aluminium die casting housing"))
        xml = ("<rss><channel><item><title>한빛정밀, 스마트공장 구축 - 경남신문</title><link>https://n/1</link><source>경남신문</source>"
               "<pubDate>Fri, 09 Oct 2026 10:00:00 GMT</pubDate></item>"
               "<item><title>소공인 판로 확보 - 서울경제</title><link>https://n/2</link><source>서울경제</source></item></channel></rss>")
        rows = photos.parse_news(xml)
        self.assertEqual((rows[0]["title"], rows[0]["source"], rows[0]["date"]), ("한빛정밀, 스마트공장 구축", "경남신문", "2026-10-09"))
        self.assertEqual(len(rows), 2)                                       # 이름이 제목에 있는 것만 남기는 일은 news()가 한다


if __name__ == "__main__":
    unittest.main()
