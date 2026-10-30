#!/usr/bin/env python3
"""
Automated tests for parse_schedule.py.
Generates test PDF files using pycairo and tests all extraction modes.
"""

import json
import os
import subprocess
import unittest
import cairo

from parse_schedule import (
    parse_schedule_pdf,
    parse_all_groups_from_pdf,
    collect_pdf_files,
    parse_multiple_pdfs,
    extract_date_from_text,
    extract_group_from_text,
    parse_cell_lesson,
    format_time_interval,
)


def create_sample_column_group_pdf(filepath: str):
    """Creates a sample PDF with multi-group column layout."""
    surface = cairo.PDFSurface(filepath, 595, 842)
    cr = cairo.Context(surface)
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(11)

    cr.move_to(50, 40)
    cr.show_text("Расписание учебных занятий на 30.10.2026")

    cols = [50, 90, 190, 340, 490]
    rows = [60, 85, 145, 205, 265]

    # Grid lines
    for y in rows:
        cr.move_to(cols[0], y)
        cr.line_to(cols[-1], y)
    cr.stroke()

    for x in cols:
        cr.move_to(x, rows[0])
        cr.line_to(x, rows[-1])
    cr.stroke()

    # Headers
    headers = ["№", "Время", "3421", "3422"]
    for i, h in enumerate(headers):
        cr.move_to(cols[i] + 5, rows[0] + 16)
        cr.show_text(h)

    # Row 1
    cr.move_to(55, 105)
    cr.show_text("1")
    cr.move_to(95, 105)
    cr.show_text("08:30 - 10:00")

    cr.move_to(195, 100)
    cr.show_text("Разработка ПО")
    cr.move_to(195, 115)
    cr.show_text("Смирнов А.В.")
    cr.move_to(195, 130)
    cr.show_text("каб. 402")

    cr.move_to(345, 100)
    cr.show_text("Философия")
    cr.move_to(345, 115)
    cr.show_text("Ковалев Д.М.")
    cr.move_to(345, 130)
    cr.show_text("ауд. 110")

    # Row 2 (with subgroups in 3421)
    cr.move_to(55, 165)
    cr.show_text("2")
    cr.move_to(95, 165)
    cr.show_text("10:10 - 11:40")

    cr.move_to(195, 160)
    cr.show_text("1 п/г Сети Васильев 305")
    cr.move_to(195, 180)
    cr.show_text("2 п/г БД Кузнецова 201")

    # Row 3 (window for 3421, lesson for 3422)
    cr.move_to(55, 225)
    cr.show_text("3")
    cr.move_to(95, 225)
    cr.show_text("12:10 - 13:40")

    cr.move_to(195, 235)
    cr.show_text("-")

    cr.move_to(345, 225)
    cr.show_text("История")
    cr.move_to(345, 240)
    cr.show_text("Сидоров С.С. 105")

    surface.finish()


def create_sample_row_lessons_pdf(filepath: str):
    """Creates a sample PDF with single-group row layout."""
    surface = cairo.PDFSurface(filepath, 595, 842)
    cr = cairo.Context(surface)
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(11)

    cr.move_to(50, 40)
    cr.show_text("Расписание занятий группы 3421 на 30 октября 2026")

    cols = [50, 80, 160, 310, 440, 520]
    rows = [60, 85, 115, 145]

    for y in rows:
        cr.move_to(cols[0], y)
        cr.line_to(cols[-1], y)
    cr.stroke()

    for x in cols:
        cr.move_to(x, rows[0])
        cr.line_to(x, rows[-1])
    cr.stroke()

    headers = ["№", "Время", "Дисциплина", "Преподаватель", "Аудитория"]
    for i, h in enumerate(headers):
        cr.move_to(cols[i] + 5, rows[0] + 16)
        cr.show_text(h)

    data = [
        ["1", "08:30 - 10:00", "Математический анализ", "Иванов И.И.", "каб. 301"],
        ["2", "10:10 - 11:40", "Физическая культура", "Петров П.П.", "спортзал"]
    ]

    for r_idx, row in enumerate(data):
        for c_idx, val in enumerate(row):
            cr.move_to(cols[c_idx] + 5, rows[r_idx + 1] + 18)
            cr.show_text(val)

    surface.finish()


def create_sample_text_pdf(filepath: str):
    """Creates a borderless, text-based PDF schedule."""
    surface = cairo.PDFSurface(filepath, 595, 842)
    cr = cairo.Context(surface)
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(12)

    cr.move_to(50, 50)
    cr.show_text("Расписание: группа 3421, дата 2026-10-30")

    cr.move_to(50, 90)
    cr.show_text("1 08:30 - 10:00 Архитектура ЭВМ Николаев Н.Н. каб. 501")

    cr.move_to(50, 120)
    cr.show_text("2 10:10 - 11:40 Операционные системы Сергеев С.С. ауд. 202")

    surface.finish()


class TestScheduleParser(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.col_pdf = "test_col_group.pdf"
        cls.row_pdf = "test_row_lessons.pdf"
        cls.text_pdf = "test_text_schedule.pdf"
        create_sample_column_group_pdf(cls.col_pdf)
        create_sample_row_lessons_pdf(cls.row_pdf)
        create_sample_text_pdf(cls.text_pdf)

    @classmethod
    def tearDownClass(cls):
        for f in [cls.col_pdf, cls.row_pdf, cls.text_pdf, "output_test.json"]:
            if os.path.exists(f):
                os.remove(f)

    def test_extract_date(self):
        self.assertEqual(extract_date_from_text("Занятия 30.10.2026"), "2026-10-30")
        self.assertEqual(extract_date_from_text("30 октября 2026 года"), "2026-10-30")
        self.assertEqual(extract_date_from_text("2026-10-30"), "2026-10-30")

    def test_extract_group(self):
        self.assertEqual(extract_group_from_text("Группа: 3421"), "3421")
        self.assertEqual(extract_group_from_text("Расписание группы 3421 на понедельник"), "3421")
        self.assertEqual(extract_group_from_text("Гр. 3421"), "3421")
        self.assertEqual(extract_group_from_text("3421 группа"), "3421")

    def test_parse_cell_lesson(self):
        res = parse_cell_lesson("Информатика\nИванов И.И.\nкаб. 204")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["subject"], "Информатика")
        self.assertEqual(res[0]["teacher"], "Иванов И.И.")
        self.assertEqual(res[0]["room"], "каб. 204")

        # Subgroups
        sg_res = parse_cell_lesson("1 п/г Информатика Иванов И.И. каб. 204\n2 п/г Физика Петров П.П. каб. 102")
        self.assertEqual(len(sg_res), 2)
        self.assertIn("1 п/г Информатика", sg_res[0]["subject"])
        self.assertEqual(sg_res[0]["teacher"], "Иванов И.И.")
        self.assertIn("2 п/г Физика", sg_res[1]["subject"])
        self.assertEqual(sg_res[1]["teacher"], "Петров П.П.")

    def test_column_group_parser(self):
        data = parse_schedule_pdf(self.col_pdf, group="3421")
        self.assertEqual(data["group"], "3421")
        self.assertEqual(data["date"], "2026-10-30")
        self.assertIn("parsed at", data)

        lessons = data["lessons"]
        self.assertGreaterEqual(len(lessons), 3)

        # Check lesson 1
        l1 = lessons[0]
        self.assertEqual(l1["num"], 1)
        self.assertEqual(l1["time"], "08:30 - 10:00")
        self.assertEqual(l1["subject"], "Разработка ПО")
        self.assertEqual(l1["teacher"], "Смирнов А.В.")
        self.assertEqual(l1["room"], "каб. 402")

    def test_row_lessons_parser(self):
        data = parse_schedule_pdf(self.row_pdf)
        self.assertEqual(data["group"], "3421")
        self.assertEqual(data["date"], "2026-10-30")
        self.assertEqual(len(data["lessons"]), 2)

        l1 = data["lessons"][0]
        self.assertEqual(l1["num"], 1)
        self.assertEqual(l1["subject"], "Математический анализ")
        self.assertEqual(l1["teacher"], "Иванов И.И.")
        self.assertEqual(l1["room"], "каб. 301")

        l2 = data["lessons"][1]
        self.assertEqual(l2["num"], 2)
        self.assertEqual(l2["subject"], "Физическая культура")
        self.assertEqual(l2["room"], "спортзал")

    def test_text_fallback_parser(self):
        data = parse_schedule_pdf(self.text_pdf)
        self.assertEqual(data["group"], "3421")
        self.assertEqual(data["date"], "2026-10-30")
        self.assertEqual(len(data["lessons"]), 2)
        self.assertEqual(data["lessons"][0]["subject"], "Архитектура ЭВМ")
        self.assertEqual(data["lessons"][0]["teacher"], "Николаев Н.Н.")
        self.assertEqual(data["lessons"][0]["room"], "каб. 501")

    def test_real_college_pdf(self):
        real_pdf = "Расписание 3.4 курс на 31.10.2026.pdf"
        if not os.path.exists(real_pdf):
            return

        data = parse_schedule_pdf(real_pdf, group="3421")
        self.assertEqual(data["group"], "3421")
        self.assertEqual(data["date"], "2026-10-31")
        self.assertEqual(len(data["lessons"]), 1)

        l1 = data["lessons"][0]
        self.assertEqual(l1["num"], 0)
        self.assertEqual(l1["time"], "08:00 - 09:10")
        self.assertEqual(l1["subject"], "МДК 02.01 Техн. разраб. ПО")
        self.assertEqual(l1["teacher"], "Гусманова Р.Н.")
        self.assertEqual(l1["room"], "Б303")

    def test_early_class_dynamic_time_interval(self):
        # 1. 8.00 early class with subsequent class at 9.20 -> 08:00 - 09:10
        t1 = format_time_interval("9.20-", "10.40", override_time="8.00")
        self.assertEqual(t1, "08:00 - 09:10")
        self.assertNotIn("(", t1)

        # 2. 8.00 early class with subsequent class at 9.30 -> 08:00 - 09:20
        t2 = format_time_interval("9.30-", "10.50", override_time="8.00")
        self.assertEqual(t2, "08:00 - 09:20")
        self.assertNotIn("(", t2)

        # 3. Regular class -> clean interval without parentheses
        t3 = format_time_interval("10.50-", "12.10")
        self.assertEqual(t3, "10:50 - 12:10")
        self.assertNotIn("(", t3)

    def test_parse_multiple_pdfs(self):
        files = [self.col_pdf, self.row_pdf]
        results = parse_multiple_pdfs(files, group="3421")
        self.assertEqual(len(results), 2)
        for item in results:
            self.assertEqual(item["group"], "3421")
            self.assertIn("date", item)
            self.assertIn("lessons", item)

    def test_cli_execution(self):
        out_file = "output_test.json"
        cmd = [
            "python3", "parse_schedule.py",
            self.col_pdf,
            "--group", "3421",
            "--output", out_file
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertTrue(os.path.exists(out_file))

        with open(out_file, "r", encoding="utf-8") as f:
            parsed = json.load(f)

        self.assertEqual(parsed["group"], "3421")
        self.assertEqual(parsed["date"], "2026-10-30")
        self.assertIn("lessons", parsed)
        self.assertIsInstance(parsed["lessons"], list)


if __name__ == "__main__":
    unittest.main()
