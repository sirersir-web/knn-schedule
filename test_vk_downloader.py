#!/usr/bin/env python3
"""
test_vk_downloader.py - Unit and integration tests for vk_downloader.py
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_downloader
from vk_downloader import (
    ScheduleNotFoundError,
    VKAPIError,
    VKAuthError,
    VKDownloader,
    fetch_schedule,
    is_doc_up_to_date,
    load_state,
    main,
    matches_keywords,
    normalize_group_id,
    parse_keywords,
    sanitize_filename,
    save_state,
)


class TestHelpers(unittest.TestCase):
    def test_parse_keywords(self):
        self.assertEqual(parse_keywords(None), [])
        self.assertEqual(parse_keywords(""), [])
        self.assertEqual(
            parse_keywords("расписание, 3-4 курс"),
            ["расписание", "3-4 курс"],
        )
        self.assertEqual(
            parse_keywords("расписание; 3-4 курс; 1 корпус"),
            ["расписание", "3-4 курс", "1 корпус"],
        )
        self.assertEqual(
            parse_keywords(["расписание, 3-4 курс", " дневное "]),
            ["расписание", "3-4 курс", "дневное"],
        )

    def test_normalize_group_id(self):
        self.assertIsNone(normalize_group_id(None))
        self.assertIsNone(normalize_group_id(""))
        self.assertEqual(normalize_group_id(12345), -12345)
        self.assertEqual(normalize_group_id(-12345), -12345)
        self.assertEqual(normalize_group_id("12345"), -12345)
        self.assertEqual(normalize_group_id("-12345"), -12345)
        self.assertEqual(normalize_group_id("club12345"), -12345)
        self.assertEqual(normalize_group_id("public98765"), -98765)
        self.assertEqual(normalize_group_id("event555"), -555)
        self.assertIsNone(normalize_group_id("college_official"))

    def test_matches_keywords(self):
        title = "Расписание 3-4 курс на 31.10.2026.pdf"
        self.assertTrue(matches_keywords(title, ["расписание", "3-4 курс"]))
        # Case insensitivity
        self.assertTrue(matches_keywords(title, ["РАСПИСАНИЕ", "3-4 КУРС"]))
        # Dot instead of hyphen separator
        title_dot = "Расписание 3.4 курс на 31.10.2026.pdf"
        self.assertTrue(matches_keywords(title_dot, ["расписание", "3-4 курс"]))
        # Space instead of hyphen separator
        title_space = "Расписание 3 4 курс.pdf"
        self.assertTrue(matches_keywords(title_space, ["расписание", "3-4 курс"]))
        # Mismatched course
        title_12 = "Расписание 1.2 курс на 01.04.2025.pdf"
        self.assertFalse(matches_keywords(title_12, ["расписание", "3-4 курс"]))
        # Missing one keyword
        self.assertFalse(matches_keywords("План занятий 3-4 курс.pdf", ["расписание", "3-4 курс"]))

    def test_matches_regex(self):
        title = "Расписание 3.4 курс на 31.10.2026.pdf"
        self.assertTrue(matches_keywords(title, regex_pattern=r"3[.-]4\s+курс"))
        self.assertFalse(matches_keywords(title, regex_pattern=r"1[.-]2\s+курс"))

    def test_sanitize_filename(self):
        self.assertEqual(
            sanitize_filename("Расписание: 3-4 курс / 31.10.pdf"),
            "Расписание_ 3-4 курс _ 31.10.pdf",
        )
        self.assertEqual(
            sanitize_filename("schedule?file<test>.pdf"),
            "schedule_file_test_.pdf",
        )
        self.assertTrue(sanitize_filename("").endswith(".pdf"))
        self.assertEqual(sanitize_filename("test_file"), "test_file.pdf")


class TestStateManagement(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_load_state(self):
        state_file = Path(self.test_dir) / ".last_vk_doc.json"
        self.assertEqual(load_state(state_file), {})

        data = {"doc_id": 999, "title": "test.pdf"}
        save_state(state_file, data)
        loaded = load_state(state_file)
        self.assertEqual(loaded, data)

    def test_is_doc_up_to_date(self):
        file_path = Path(self.test_dir) / "test.pdf"
        file_path.write_bytes(b"content of 16 bytes")

        state = {
            "doc_id": 100,
            "owner_id": -200,
            "saved_path": str(file_path),
        }

        # Matching doc
        doc = {"id": 100, "owner_id": -200, "size": len(b"content of 16 bytes")}
        self.assertTrue(is_doc_up_to_date(state, doc, file_path))

        # Different doc ID
        doc_diff_id = {"id": 101, "owner_id": -200}
        self.assertFalse(is_doc_up_to_date(state, doc_diff_id, file_path))

        # Size mismatch
        doc_diff_size = {"id": 100, "owner_id": -200, "size": 9999}
        self.assertFalse(is_doc_up_to_date(state, doc_diff_size, file_path))

        # Missing file on disk
        missing_path = Path(self.test_dir) / "missing.pdf"
        self.assertFalse(is_doc_up_to_date(state, doc, missing_path))


class TestVKDownloader(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.token = "test_token_123"
        self.group_id = 987654

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_init_validation(self):
        with self.assertRaises(VKAuthError):
            VKDownloader(token="")

        with self.assertRaises(ValueError):
            VKDownloader(token="abc")

        # Numeric group ID
        dl = VKDownloader(token="abc", group_id=123)
        self.assertEqual(dl.group_id, -123)
        self.assertIsNone(dl.domain)

        # Domain string
        dl2 = VKDownloader(token="abc", domain="@my_college")
        self.assertIsNone(dl2.group_id)
        self.assertEqual(dl2.domain, "my_college")

        # Alphanumeric group_id fallback to domain
        dl3 = VKDownloader(token="abc", group_id="college_domain")
        self.assertIsNone(dl3.group_id)
        self.assertEqual(dl3.domain, "college_domain")

    @patch("requests.Session.get")
    def test_api_call_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": {"items": [{"id": 1}]}}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        dl = VKDownloader(token=self.token, group_id=self.group_id)
        posts = dl.get_wall_posts(count=5)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["id"], 1)

        # Check call arguments
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertIn("wall.get", args[0])
        self.assertEqual(kwargs["params"]["access_token"], self.token)
        self.assertEqual(kwargs["params"]["owner_id"], -self.group_id)
        self.assertEqual(kwargs["params"]["count"], 5)

    @patch("requests.Session.get")
    def test_api_call_vk_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "error": {
                "error_code": 5,
                "error_msg": "User authorization failed: invalid access_token.",
            }
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        dl = VKDownloader(token="bad_token", group_id=self.group_id)
        with self.assertRaises(VKAPIError) as ctx:
            dl.get_wall_posts()
        self.assertEqual(ctx.exception.code, 5)
        self.assertIn("authorization failed", str(ctx.exception))

    def test_find_schedule_doc(self):
        dl = VKDownloader(token=self.token, group_id=self.group_id)
        posts = [
            {
                "id": 101,
                "text": "Новости колледжа",
                "attachments": [
                    {
                        "type": "photo",
                        "photo": {"id": 1},
                    }
                ],
            },
            {
                "id": 102,
                "text": "Расписание занятий",
                "attachments": [
                    {
                        "type": "doc",
                        "doc": {
                            "id": 501,
                            "owner_id": -self.group_id,
                            "title": "Расписание 1.2 курс на 31.10.2026.pdf",
                            "ext": "pdf",
                            "url": "https://vk.com/doc501.pdf",
                        },
                    },
                    {
                        "type": "doc",
                        "doc": {
                            "id": 502,
                            "owner_id": -self.group_id,
                            "title": "Расписание 3.4 курс на 31.10.2026.pdf",
                            "ext": "pdf",
                            "url": "https://vk.com/doc502.pdf",
                        },
                    },
                ],
            },
        ]

        # Find 3-4 курс
        match = dl.find_schedule_doc(posts, keywords=["расписание", "3-4 курс"])
        self.assertIsNotNone(match)
        self.assertEqual(match["doc"]["id"], 502)
        self.assertEqual(match["post"]["id"], 102)

        # Find 1-2 курс
        match_12 = dl.find_schedule_doc(posts, keywords=["расписание", "1-2 курс"])
        self.assertIsNotNone(match_12)
        self.assertEqual(match_12["doc"]["id"], 501)

        # Unmatched course
        match_none = dl.find_schedule_doc(posts, keywords=["расписание", "5 курс"])
        self.assertIsNone(match_none)

    def test_find_schedule_doc_in_repost(self):
        dl = VKDownloader(token=self.token, group_id=self.group_id)
        posts = [
            {
                "id": 201,
                "text": "Репост расписания",
                "attachments": [],
                "copy_history": [
                    {
                        "id": 301,
                        "attachments": [
                            {
                                "type": "doc",
                                "doc": {
                                    "id": 601,
                                    "title": "Расписание 3-4 курс.pdf",
                                    "ext": "pdf",
                                    "url": "https://vk.com/doc601.pdf",
                                },
                            }
                        ],
                    }
                ],
            }
        ]
        match = dl.find_schedule_doc(posts, keywords=["3-4 курс"])
        self.assertIsNotNone(match)
        self.assertEqual(match["doc"]["id"], 601)

    @patch("requests.Session.get")
    def test_fetch_schedule_full_flow(self, mock_get):
        pdf_content = b"%PDF-1.4 Mock Schedule PDF Content"
        target_doc = {
            "id": 777,
            "owner_id": -self.group_id,
            "title": "Расписание 3-4 курс на 31.10.2026.pdf",
            "ext": "pdf",
            "url": "https://vk.com/doc777.pdf",
            "size": len(pdf_content),
        }
        wall_response = {
            "response": {
                "items": [
                    {
                        "id": 900,
                        "attachments": [{"type": "doc", "doc": target_doc}],
                    }
                ]
            }
        }

        def mock_get_handler(url, *args, **kwargs):
            mock = MagicMock()
            if "wall.get" in url:
                mock.json.return_value = wall_response
                mock.raise_for_status = MagicMock()
                return mock
            elif "doc777.pdf" in url:
                mock.raise_for_status = MagicMock()
                mock.iter_content.return_value = [pdf_content]
                mock.__enter__.return_value = mock
                mock.__exit__.return_value = False
                return mock
            raise ValueError(f"Unexpected url: {url}")

        mock_get.side_effect = mock_get_handler

        output_dir = Path(self.test_dir) / "downloads"
        state_file = Path(self.test_dir) / ".last_vk_doc.json"

        dl = VKDownloader(token=self.token, group_id=self.group_id)

        # 1. First run: downloads fresh file
        saved_path, is_fresh = dl.fetch_schedule(
            keywords=["расписание", "3-4 курс"],
            output_dir=output_dir,
            state_file=state_file,
            count=10,
        )

        self.assertTrue(is_fresh)
        self.assertTrue(saved_path.exists())
        self.assertEqual(saved_path.read_bytes(), pdf_content)
        self.assertTrue(state_file.exists())

        # Check state file content
        state = load_state(state_file)
        self.assertEqual(state["doc_id"], 777)
        self.assertEqual(state["post_id"], 900)

        # 2. Second run: already up to date
        saved_path2, is_fresh2 = dl.fetch_schedule(
            keywords=["расписание", "3-4 курс"],
            output_dir=output_dir,
            state_file=state_file,
            count=10,
        )
        self.assertFalse(is_fresh2)
        self.assertEqual(saved_path, saved_path2)

        # 3. Third run with force=True: re-downloads
        saved_path3, is_fresh3 = dl.fetch_schedule(
            keywords=["расписание", "3-4 курс"],
            output_dir=output_dir,
            state_file=state_file,
            count=10,
            force=True,
        )
        self.assertTrue(is_fresh3)


class TestCLIAndMain(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_main_missing_token(self):
        with patch.dict(os.environ, {}, clear=True):
            code = main(["--group-id", "123", "--keywords", "расписание"])
            self.assertEqual(code, 1)

    def test_main_missing_group_id(self):
        with patch.dict(os.environ, {"VK_SERVICE_TOKEN": "token"}, clear=True):
            code = main(["--keywords", "расписание"])
            self.assertEqual(code, 1)

    def test_main_missing_keywords(self):
        with patch.dict(os.environ, {"VK_SERVICE_TOKEN": "token", "VK_GROUP_ID": "123"}, clear=True):
            code = main([])
            self.assertEqual(code, 1)

    @patch("vk_downloader.VKDownloader.fetch_schedule")
    def test_main_success_prints_to_stdout(self, mock_fetch):
        expected_path = Path(self.test_dir) / "downloads" / "schedule.pdf"
        mock_fetch.return_value = (expected_path, True)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            code = main([
                "--token", "my_token",
                "--group-id", "12345",
                "--keywords", "расписание", "3-4 курс",
                "--output-dir", str(Path(self.test_dir) / "downloads"),
            ])

        self.assertEqual(code, 0)
        self.assertEqual(stdout_buf.getvalue().strip(), str(expected_path))

    @patch("vk_downloader.VKDownloader.fetch_schedule")
    def test_main_schedule_not_found(self, mock_fetch):
        mock_fetch.side_effect = ScheduleNotFoundError("Doc not found")

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            code = main([
                "--token", "my_token",
                "--group-id", "12345",
                "--keywords", "расписание",
            ])

        self.assertEqual(code, 3)
        self.assertIn("Doc not found", stderr_buf.getvalue())

    @patch("vk_downloader.VKDownloader.fetch_schedule")
    def test_main_vk_api_error(self, mock_fetch):
        mock_fetch.side_effect = VKAPIError(5, "Auth failed")

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            code = main([
                "--token", "bad_token",
                "--group-id", "12345",
                "--keywords", "расписание",
            ])

        self.assertEqual(code, 2)
        self.assertIn("VK API error 5", stderr_buf.getvalue())

    @patch.dict(os.environ, {
        "VK_SERVICE_TOKEN": "env_token",
        "VK_GROUP_ID": "999",
        "TARGET_KEYWORDS": "расписание, 3-4 курс",
        "OUTPUT_DIR": "/tmp/env_downloads",
    }, clear=True)
    @patch("vk_downloader.VKDownloader.fetch_schedule")
    def test_main_env_vars_fallback(self, mock_fetch):
        expected_path = Path("/tmp/env_downloads/schedule.pdf")
        mock_fetch.return_value = (expected_path, False)

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = main([])

        self.assertEqual(code, 0)
        self.assertEqual(stdout_buf.getvalue().strip(), str(expected_path))
        mock_fetch.assert_called_once_with(
            keywords=["расписание", "3-4 курс"],
            regex_pattern=None,
            output_dir="/tmp/env_downloads",
            state_file=".last_vk_doc.json",
            count=20,
            force=False,
        )


if __name__ == "__main__":
    unittest.main()
