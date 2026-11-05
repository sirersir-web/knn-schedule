#!/usr/bin/env python3
"""
PDF Schedule Parser using pdfplumber.

Extracts college / university class schedules from PDF documents into structured JSON:
{
  "group": "3421",
  "date": "2026-10-31",
  "parsed at": "2026-10-30T19:04:50+03:00",
  "lessons": [
    {
      "num": 0,
      "time": "08:00 - 09:10",
      "subject": "МДК 02.01 Техн. разраб. ПО",
      "teacher": "Гусманова Р.Н.",
      "room": "Б303"
    }
  ]
}
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import pdfplumber

RUSSIAN_MONTHS = {
    "января": 1, "январь": 1,
    "февраля": 2, "февраль": 2,
    "марта": 3, "март": 3,
    "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7,
    "августа": 8, "август": 8,
    "сентября": 9, "сентябрь": 9,
    "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11,
    "декабря": 12, "декабрь": 12,
}



ROMAN_NUMERALS = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
    "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10
}


TEACHER_RE = re.compile(
    r"(?:[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.?|"
    r"[А-ЯЁ]\.\s*[А-ЯЁ]\.?\s+[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?|"
    r"(?:Prof\.|Dr\.|Mr\.|Mrs\.|Ms\.)\s+[A-Z][a-z]+)"
)


def clean_cell(c: Any) -> str:
    """Normalize cell value to stripped string."""
    return str(c).strip() if c is not None else ""


def extract_date_from_text(text: str, filename: str = "") -> Optional[str]:
    """
    Extract schedule date from text or filename.
    Supports '31.10.2026', '31 октября 2026', '2026-10-31', etc.
    Returns 'YYYY-MM-DD' or None.
    """
    combined = f"{filename} {text}"

    # Text month: "31 октября 2026"
    month_names = "|".join(RUSSIAN_MONTHS.keys())
    m = re.search(rf"\b(\d{{1,2}})\s+({month_names})(?:\s+(\d{{4}}))?", combined, re.IGNORECASE)
    if m:
        day = int(m.group(1))
        month = RUSSIAN_MONTHS[m.group(2).lower()]
        year = int(m.group(3)) if m.group(3) else datetime.now().year
        return f"{year:04d}-{month:02d}-{day:02d}"

    # dd.mm.yyyy or dd.mm.yy
    m = re.search(r"\b(\d{1,2})[.\/](\d{1,2})[.\/](\d{2,4})\b", combined)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3))
        if year < 100:
            year += 2000
        return f"{year:04d}-{month:02d}-{day:02d}"

    # ISO yyyy-mm-dd
    m = re.search(r"\b(\d{4})[-.\/](\d{1,2})[-.\/](\d{1,2})\b", combined)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"

    return None


def extract_group_from_text(text: str) -> Optional[str]:
    """
    Extract single group name from header/text if present.
    """
    if not text:
        return None

    m = re.search(r"(?i)(?:групп[аыеу]?|group|гр\.?)\s*[:\-№]?\s*([0-9a-zA-Zа-яА-Я\-]*\d+[0-9a-zA-Zа-яА-Я\-]*)", text)
    if m:
        return m.group(1).strip()

    m = re.search(r"(?i)\b([0-9a-zA-Zа-яА-Я\-]*\d+[0-9a-zA-Zа-яА-Я\-]*)\s*(?:групп[аыеу]?|group)\b", text)
    if m:
        return m.group(1).strip()

    return None


def extract_week_from_text(text: str) -> Optional[int]:
    """
    Extract schedule week number from header/text.
    Supports 'I неделя', 'II неделя', '1 неделя', '2 неделя', etc.
    Returns 1, 2, ... or None.
    """
    if not text:
        return None

    # Matches 'I неделя', 'II неделя', '1 неделя', '1-я неделя', '1 нед.'
    m = re.search(r"(?i)\b([IVX]+|\d+)\s*(?:[-–—]\s*(?:я|ая|ей|ю)\s*)?недел[яеию]?\b", text)
    if m:
        val = m.group(1).upper()
        if val in ROMAN_NUMERALS:
            return ROMAN_NUMERALS[val]
        if val.isdigit():
            return int(val)

    # Matches 'неделя 1', 'неделя: II', 'week 1', etc.
    m = re.search(r"(?i)\b(?:недел[яеию]?|week)\s*[:№\-]?\s*([IVX]+|\d+)\b", text)
    if m:
        val = m.group(1).upper()
        if val in ROMAN_NUMERALS:
            return ROMAN_NUMERALS[val]
        if val.isdigit():
            return int(val)

    return None


def parse_lesson_num(raw_value: Any, default_num: int = 1) -> int:
    """Parse lesson number from cell text or default index."""
    if raw_value is None:
        return default_num

    s = str(raw_value).strip().upper()
    if s in ROMAN_NUMERALS:
        return ROMAN_NUMERALS[s]

    digits = re.findall(r"\d+", s)
    if digits:
        return int(digits[0])

    return default_num


def parse_time_to_minutes(t_str: str) -> Optional[int]:
    """Parse time string like '9.20', '09:20', '9:20' to minutes from midnight."""
    if not t_str:
        return None
    m = re.search(r"\b(\d{1,2})[.:](\d{2})\b", t_str)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def minutes_to_time_str(total_min: int) -> str:
    """Convert minutes from midnight to 'HH:MM' string."""
    h = (total_min // 60) % 24
    m = total_min % 60
    return f"{h:02d}:{m:02d}"


def normalize_time_str(t: str) -> str:
    """Normalize a single time token into 'HH:MM'."""
    m = re.search(r"\b(\d{1,2})[.:](\d{2})\b", t)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    return t


def format_time_interval(
    t1: str,
    t2: str,
    override_time: str = "",
    subsequent_start: str = "09:20"
) -> str:
    """
    Format a clean single interval: 'HH:MM - HH:MM'.
    Completely eliminates merged time strings like '8.00 (9.20 - 10.40)'.

    When a class has a start time of 8.00 (period 0 / early class):
      Calculates end time dynamically as (subsequent_start - 10 minutes break).
      Returns '08:00 - <end_time>' (e.g. '08:00 - 09:10').
    """
    is_early_8am = False
    if override_time:
        m_ov = re.search(r"\b(\d{1,2})[.:](\d{2})\b", override_time)
        if m_ov and int(m_ov.group(1)) == 8 and int(m_ov.group(2)) == 0:
            is_early_8am = True
    elif t1:
        m_t1 = re.search(r"\b(\d{1,2})[.:](\d{2})\b", t1)
        if m_t1 and int(m_t1.group(1)) == 8 and int(m_t1.group(2)) == 0 and not t2:
            is_early_8am = True

    if is_early_8am:
        # Determine start time of subsequent regular class (e.g. 9.20)
        sub_time = subsequent_start
        if t1:
            m_sub = re.search(r"\b(\d{1,2})[.:](\d{2})\b", t1)
            if m_sub:
                h, mn = int(m_sub.group(1)), int(m_sub.group(2))
                # If t1 specifies morning period 1 time (e.g. 9.20 or 9.30)
                if 9 * 60 <= h * 60 + mn <= 10 * 60:
                    sub_time = f"{h:02d}:{mn:02d}"

        sub_min = parse_time_to_minutes(sub_time) or (9 * 60 + 20)
        end_min = sub_min - 10
        end_time_str = minutes_to_time_str(end_min)
        return f"08:00 - {end_time_str}"

    # Standard clean interval: 'HH:MM - HH:MM'
    raw = f"{t1} {t2}".strip()
    times = re.findall(r"\b\d{1,2}[.:]\d{2}\b", raw)
    if len(times) >= 2:
        return f"{normalize_time_str(times[0])} - {normalize_time_str(times[1])}"
    elif len(times) == 1:
        return normalize_time_str(times[0])
    else:
        cleaned = raw.replace("\n", " ").strip()
        cleaned = re.sub(r"\s*-\s*", " - ", cleaned)
        return cleaned


def extract_room_and_override(rm1: str, rm2: str) -> Tuple[str, str]:
    """
    Extract room name(s) and any override start time (e.g. '8.00' written in room cell).
    """
    override_time = ""
    rooms = []
    for rm in (rm1, rm2):
        if not rm:
            continue
        lines = [l.strip() for l in rm.split("\n") if l.strip()]
        for line in lines:
            if re.match(r"^\d{1,2}[.:]\d{2}$", line):
                override_time = line
            else:
                rooms.append(line)
    return ", ".join(rooms), override_time


def parse_cell_lesson(raw_text: Optional[str]) -> List[Dict[str, str]]:
    """
    Parse a schedule cell containing lesson information into subject, teacher, room.
    Handles single lessons, multiline cells, and subgroup splits (e.g. 1 п/г, 2 п/г).
    """
    if not raw_text or not raw_text.strip():
        return []

    text = raw_text.strip()
    if text.lower() in ("-", "—", "нет", "окно", "нет пар", "none", "пусто", "х", "."):
        return []

    subgroup_split = re.split(r"(?=(?:[12]\s*(?:п/?г|подгруппа|п\.г\.)))", text, flags=re.IGNORECASE)
    chunks = [c.strip() for c in subgroup_split if c.strip()]
    if not chunks:
        chunks = [text]

    results = []
    for chunk in chunks:
        working = chunk
        room = ""
        teacher = ""

        room_kw = re.search(r"(?i)\b(?:спортзал|дистант(?:но|ионно)?|онлайн|zoom|актовый\s*зал|тир)\b", working)
        if room_kw:
            room = room_kw.group(0).strip()
            working = working[:room_kw.start()] + " " + working[room_kw.end():]

        if not room:
            room_prefix = re.search(
                r"(?i)(?:каб(?:инет)?\.?|ауд(?:итория)?\.?|к\.|лаб\.)\s*([0-9a-zA-Zа-яА-Я\/\-]+)",
                working
            )
            if room_prefix:
                room = room_prefix.group(0).strip()
                working = working[:room_prefix.start()] + " " + working[room_prefix.end():]

        tm = TEACHER_RE.search(working)
        if tm:
            teacher = tm.group(0).strip()
            working = working[:tm.start()] + " " + working[tm.end():]

        if not room:
            m_trailing = re.search(r"(?:^|\s)([0-9]{2,4}[а-яА-Яa-zA-Z]?)\s*$", working)
            if m_trailing:
                room = m_trailing.group(1).strip()
                working = working[:m_trailing.start()] + " " + working[m_trailing.end():]

        subject = working.replace("\n", " ")
        subject = re.sub(r"\(\s*\)", "", subject)
        subject = re.sub(r"(?<=\s)[\/\|\,\;]+(?=\s)", " ", subject)
        subject = re.sub(r"^\s*[\/\|\,\;]+\s*", "", subject)
        subject = re.sub(r"\s*[\/\|\,\;]+\s*$", "", subject)
        subject = re.sub(r"\s{2,}", " ", subject).strip()

        if subject or teacher or room:
            results.append({
                "subject": subject,
                "teacher": teacher,
                "room": room
            })

    return results


ROOM_KEYWORDS = ("каб", "ауд", "комн", "помещ", "класс", "room", "спортзал")
TABLE_HEADER_KEYWORDS = ("предмет", "дисциплин", "преподават", "аудитор", "учител")


def is_valid_group_code(val: str) -> bool:
    """Check if cell string represents a valid group identifier."""
    if not val or len(val) > 8:
        return False
    val_lower = val.lower()
    if any(kw in val_lower for kw in ROOM_KEYWORDS):
        return False
    if not any(ch.isdigit() for ch in val):
        return False
    if re.match(r"^\d{1,2}[.:]\d{2}", val):
        return False
    if val.isdigit() and len(val) <= 2:
        return False
    return True


def find_blocks_in_table(table: List[List[Optional[str]]]) -> List[Tuple[int, Dict[int, str]]]:
    """
    Scan table rows to find group block headers.
    Returns list of (row_index, {col_index: group_name}).
    """
    blocks = []
    for r_idx, row in enumerate(table):
        # A row with pair number in col 0 is a data row, not a header
        c0 = clean_cell(row[0])
        if c0.isdigit() and len(c0) <= 2:
            continue

        row_text_lower = " ".join(clean_cell(c).lower() for c in row)
        # If this row is a standard table column header (Subject, Teacher, Room), skip
        if any(kw in row_text_lower for kw in TABLE_HEADER_KEYWORDS):
            continue

        groups_in_row = {}
        for c_idx in range(len(row)):
            val = clean_cell(row[c_idx])
            if is_valid_group_code(val):
                groups_in_row[c_idx] = val

        if groups_in_row:
            blocks.append((r_idx, groups_in_row))

    return blocks


def parse_schedule_pdf(
    pdf_path: str,
    group: Optional[str] = None,
    date: Optional[str] = None,
    week: Optional[int] = None,
    include_empty: bool = False
) -> Dict[str, Any]:
    """
    Parse a PDF schedule file and return structured data conforming to:
    {
      "group": "...",
      "date": "...",
      "week": 1,
      "parsed at": "...",
      "lessons": [
        {
          "num": 1,
          "time": "...",
          "subject": "...",
          "teacher": "...",
          "room": "..."
        }
      ]
    }
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    parsed_at = datetime.now().astimezone().isoformat()
    detected_date = date
    detected_week = week
    target_group = group
    lessons: List[Dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        if not detected_date:
            detected_date = extract_date_from_text(full_text, os.path.basename(pdf_path))
            if not detected_date:
                detected_date = datetime.now().strftime("%Y-%m-%d")

        if detected_week is None:
            detected_week = extract_week_from_text(full_text)

        all_blocks = []
        for p_idx, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            if not tables:
                tables = page.extract_tables({
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                    "snap_tolerance": 3
                })

            for t_idx, table in enumerate(tables):
                if not table or len(table) < 2:
                    continue
                blocks = find_blocks_in_table(table)
                for b_row, gmap in blocks:
                    all_blocks.append((p_idx, t_idx, table, b_row, gmap))

        discovered_groups: List[str] = []
        for _, _, _, _, gmap in all_blocks:
            for g_name in gmap.values():
                if g_name not in discovered_groups:
                    discovered_groups.append(g_name)

        if not target_group:
            if "3421" in discovered_groups:
                target_group = "3421"
            elif discovered_groups:
                target_group = discovered_groups[0]
            else:
                target_group = extract_group_from_text(full_text) or "Unknown"

        for p_idx, t_idx, table, b_row, groups_in_row in all_blocks:
            matched_cols = [c for c, name in groups_in_row.items()
                            if target_group.lower() in name.lower() or name.lower() in target_group.lower()]
            if not matched_cols:
                continue

            target_col = matched_cols[0]
            group_name = groups_in_row[target_col]

            group_cols = sorted(groups_in_row.keys())
            target_idx = group_cols.index(target_col)
            is_triplet = False
            if target_idx < len(group_cols) - 1:
                is_triplet = (group_cols[target_idx + 1] - target_col) >= 2
            elif target_idx > 0:
                is_triplet = (target_col - group_cols[target_idx - 1]) >= 2
            else:
                is_triplet = (len(table[b_row]) >= target_col + 2)

            start_r = b_row + 1
            if start_r < len(table) and clean_cell(table[start_r][target_col]) in ("А", "Б", "A", "B"):
                start_r += 1

            cur_r = start_r
            while cur_r < len(table):
                chk_groups = [clean_cell(table[cur_r][c]) for c in range(len(table[cur_r]))
                              if clean_cell(table[cur_r][c]) and any(ch.isdigit() for ch in clean_cell(table[cur_r][c]))
                              and len(clean_cell(table[cur_r][c])) in (3, 4, 5, 6, 7)]
                if len(chk_groups) >= 2 and not clean_cell(table[cur_r][0]):
                    break

                num_val = clean_cell(table[cur_r][0])
                if num_val and (num_val.isdigit() or num_val.upper() in ROMAN_NUMERALS):
                    num_int = parse_lesson_num(num_val, default_num=len(lessons) + 1)
                    r1 = table[cur_r]

                    has_r2 = (cur_r + 1 < len(table)) and not clean_cell(table[cur_r + 1][0])
                    r2 = table[cur_r + 1] if has_r2 else [None] * len(r1)

                    if is_triplet:
                        t1 = clean_cell(r1[target_col - 1]) if target_col > 0 else ""
                        t2 = clean_cell(r2[target_col - 1]) if target_col > 0 and len(r2) > target_col - 1 else ""
                        s1 = clean_cell(r1[target_col]) if len(r1) > target_col else ""
                        s2 = clean_cell(r2[target_col]) if len(r2) > target_col else ""
                        rm1 = clean_cell(r1[target_col + 1]) if len(r1) > target_col + 1 else ""
                        rm2 = clean_cell(r2[target_col + 1]) if len(r2) > target_col + 1 else ""

                        room_val, override_time = extract_room_and_override(rm1, rm2)
                        final_time = format_time_interval(t1, t2, override_time)

                        # Early class starting at 8.00 (period 0): set num to 0
                        if override_time and parse_time_to_minutes(override_time) == 8 * 60:
                            num_int = 0
                        elif t1 and parse_time_to_minutes(t1) == 8 * 60 and not t2:
                            num_int = 0

                        full_subj = (s1 + "\n" + s2).strip()
                        if full_subj or include_empty:
                            tm = TEACHER_RE.findall(full_subj)
                            teacher_val = ", ".join(t.strip() for t in tm) if tm else ""
                            subj_clean = TEACHER_RE.sub("", full_subj)
                            subj_clean = subj_clean.replace("\n", " ")
                            subj_clean = re.sub(r"\s{2,}", " ", subj_clean).strip()

                            lessons.append({
                                "num": num_int,
                                "time": final_time,
                                "subject": subj_clean,
                                "teacher": teacher_val,
                                "room": room_val
                            })
                    else:
                        time_val = clean_cell(r1[1]) if len(r1) > 1 else ""
                        cell_text = clean_cell(r1[target_col]) if len(r1) > target_col else ""

                        parsed_chunks = parse_cell_lesson(cell_text)
                        if parsed_chunks:
                            for chunk in parsed_chunks:
                                lessons.append({
                                    "num": num_int,
                                    "time": time_val,
                                    "subject": chunk["subject"],
                                    "teacher": chunk["teacher"],
                                    "room": chunk["room"]
                                })
                        elif include_empty and (num_val or time_val):
                            lessons.append({
                                "num": num_int,
                                "time": time_val,
                                "subject": "",
                                "teacher": "",
                                "room": ""
                            })

                    cur_r = cur_r + 2 if has_r2 else cur_r + 1
                else:
                    cur_r += 1

            break

        if not lessons:
            lessons = parse_row_based_fallback(pdf, target_group=target_group, include_empty=include_empty)

        if not lessons:
            lessons = parse_text_fallback(full_text, include_empty=include_empty)

        # Ensure lessons are sorted chronologically by lesson number (period 0 first)
        lessons.sort(key=lambda x: x["num"])

        # Re-number lessons sequentially starting from 1 for the group's first lesson,
        # regardless of time or row in PDF, while preserving identical numbers for subgroups in the same slot.
        if lessons:
            unique_nums = []
            for l in lessons:
                if l["num"] not in unique_nums:
                    unique_nums.append(l["num"])

            num_map = {orig: idx + 1 for idx, orig in enumerate(unique_nums)}
            for l in lessons:
                l["num"] = num_map[l["num"]]

    return {
        "group": target_group or "Unknown",
        "date": detected_date,
        "week": detected_week,
        "parsed at": parsed_at,
        "lessons": lessons
    }


def parse_row_based_fallback(pdf: pdfplumber.PDF, target_group: Optional[str] = None, include_empty: bool = False) -> List[Dict[str, Any]]:
    """
    Fallback parser for tables where each row is a lesson (with subject, teacher, room columns).
    """
    lessons = []
    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            if not table or len(table) < 2:
                continue

            header_row_idx = 0
            num_col = None
            time_col = None
            subj_col = None
            teach_col = None
            room_col = None

            for r_idx in range(min(3, len(table))):
                row = [clean_cell(c).lower() for c in table[r_idx]]
                for idx, cell in enumerate(row):
                    if num_col is None and any(w in cell for w in ["№", "пара", "номер", "урок", "num"]):
                        num_col = idx
                    elif time_col is None and any(w in cell for w in ["время", "time", "период", "звон"]):
                        time_col = idx
                    elif subj_col is None and any(w in cell for w in ["предмет", "дисциплин", "заняти", "subject"]):
                        subj_col = idx
                    elif teach_col is None and any(w in cell for w in ["преподават", "учител", "фио", "педагог", "teacher"]):
                        teach_col = idx
                    elif room_col is None and any(w in cell for w in ["каб", "ауд", "помещен", "комн", "room"]):
                        room_col = idx
                if subj_col is not None:
                    header_row_idx = r_idx
                    break

            if subj_col is not None:
                for r_idx in range(header_row_idx + 1, len(table)):
                    row = table[r_idx]
                    if not any(row):
                        continue

                    raw_num = row[num_col] if num_col is not None and num_col < len(row) else None
                    num_val = parse_lesson_num(raw_num, default_num=len(lessons) + 1)
                    time_val = clean_cell(row[time_col]) if time_col is not None and time_col < len(row) else ""
                    subj_val = clean_cell(row[subj_col]) if subj_col < len(row) else ""
                    teach_val = clean_cell(row[teach_col]) if teach_col is not None and teach_col < len(row) else ""
                    room_val = clean_cell(row[room_col]) if room_col is not None and room_col < len(row) else ""

                    if subj_val or include_empty:
                        lessons.append({
                            "num": num_val,
                            "time": time_val,
                            "subject": subj_val,
                            "teacher": teach_val,
                            "room": room_val
                        })

    return lessons


def parse_text_fallback(text: str, include_empty: bool = False) -> List[Dict[str, Any]]:
    """
    Fallback parser for text-based schedules without grid lines.
    """
    lessons = []
    lines = text.splitlines()
    time_regex = re.compile(r"(\d{1,2}[:.]\d{2}\s*[-–—]\s*\d{1,2}[:.]\d{2})")

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue

        time_match = time_regex.search(line_str)
        if time_match:
            time_val = time_match.group(1).strip()
            before_time = line_str[:time_match.start()].strip()
            after_time = line_str[time_match.end():].strip()

            num_val = parse_lesson_num(before_time, default_num=len(lessons) + 1)
            parsed_chunks = parse_cell_lesson(after_time)
            if parsed_chunks:
                for chunk in parsed_chunks:
                    lessons.append({
                        "num": num_val,
                        "time": time_val,
                        "subject": chunk["subject"],
                        "teacher": chunk["teacher"],
                        "room": chunk["room"]
                    })
            elif include_empty:
                lessons.append({
                    "num": num_val,
                    "time": time_val,
                    "subject": "",
                    "teacher": "",
                    "room": ""
                })

    return lessons


def collect_pdf_files(inputs: List[str]) -> List[str]:
    """
    Expand a list of file paths, directory paths, and glob patterns into a unique sorted list of PDF files.
    """
    found_files: Set[str] = set()
    for item in inputs:
        if os.path.isdir(item):
            # Scan directory recursively for all .pdf files
            pattern = os.path.join(item, "**", "*.pdf")
            for f in glob.glob(pattern, recursive=True):
                found_files.add(os.path.abspath(f))
        elif any(char in item for char in ("*", "?", "[")):
            for f in glob.glob(item, recursive=True):
                if f.lower().endswith(".pdf") and os.path.isfile(f):
                    found_files.add(os.path.abspath(f))
        elif os.path.isfile(item) and item.lower().endswith(".pdf"):
            found_files.add(os.path.abspath(item))

    return sorted(list(found_files))


def parse_multiple_pdfs(
    pdf_paths: List[str],
    group: Optional[str] = None,
    date: Optional[str] = None,
    week: Optional[int] = None,
    include_empty: bool = False,
    all_groups: bool = False,
    retention_days: Optional[int] = None,
    clean_files: bool = False,
    deduplicate: bool = False,
) -> List[Any]:
    """
    Parse multiple PDF schedule files and return combined results sorted by date.
    - If retention_days is set: automatically discards schedules older than retention_days.
    - If clean_files is True: automatically deletes old PDF files from disk.
    - If deduplicate is True: updated schedule replaces older one for the same date and group.
    """
    results_map: Dict[Any, Any] = {}
    results_list: List[Any] = []
    today = datetime.now().date()

    for path in pdf_paths:
        try:
            if all_groups:
                data = parse_all_groups_from_pdf(path, date=date, week=week)
                first_g = next(iter(data.values()), None) if data else None
                s_date_str = first_g.get("date") if first_g else None
            else:
                data = parse_schedule_pdf(path, group=group, date=date, week=week, include_empty=include_empty)
                s_date_str = data.get("date")

            # Check retention limit (keep last N days and future dates)
            if retention_days is not None and s_date_str:
                try:
                    s_dt = datetime.strptime(s_date_str, "%Y-%m-%d").date()
                    if (today - s_dt).days > retention_days:
                        if clean_files and os.path.exists(path):
                            try:
                                os.remove(path)
                                print(f"Deleted old schedule file (> {retention_days} days): {path}", file=sys.stderr)
                            except OSError as err:
                                print(f"Warning: Failed to delete {path}: {err}", file=sys.stderr)
                        continue  # Skip old schedule from results
                except ValueError:
                    pass

            entry = {"file": os.path.basename(path), "groups": data} if all_groups else data

            if deduplicate and not all_groups:
                key = (data.get("date"), data.get("group"))
                results_map[key] = entry
            else:
                results_list.append(entry)

        except Exception as e:
            print(f"Warning: Failed to parse {path}: {e}", file=sys.stderr)

    combined_results = list(results_map.values()) if (deduplicate and not all_groups) else results_list
    if not all_groups:
        # Sort schedules by date (earliest to latest)
        combined_results.sort(key=lambda x: x.get("date", ""))

    return combined_results


def parse_all_groups_from_pdf(
    pdf_path: str,
    date: Optional[str] = None,
    week: Optional[int] = None
) -> Dict[str, Dict[str, Any]]:
    """
    Discover all groups present in the PDF and parse schedule for each group.
    Returns a dictionary mapping group_name -> schedule_data.
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    discovered_groups: List[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue
                blocks = find_blocks_in_table(table)
                for _, gmap in blocks:
                    for g in gmap.values():
                        if g not in discovered_groups:
                            discovered_groups.append(g)

    if not discovered_groups:
        single_res = parse_schedule_pdf(pdf_path, date=date, week=week)
        return {single_res["group"]: single_res}

    results = {}
    for grp in discovered_groups:
        res = parse_schedule_pdf(pdf_path, group=grp, date=date, week=week)
        if res["lessons"]:
            results[grp] = res

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Parse one or multiple university/college PDF schedules into structured JSON."
    )
    parser.add_argument(
        "pdf_inputs",
        nargs="+",
        help="One or more PDF files, glob patterns (e.g. '*.pdf'), or directory paths"
    )
    parser.add_argument("-g", "--group", help="Target group name/number (e.g. '3421')")
    parser.add_argument("-d", "--date", help="Override schedule date (e.g. '2026-10-31')")
    parser.add_argument("-w", "--week", type=int, help="Override schedule week number (e.g. 1 or 2)")
    parser.add_argument("-o", "--output", help="Save combined output to JSON file (default: stdout)")
    parser.add_argument(
        "--out-dir",
        help="Save each parsed schedule as an individual JSON file in this directory"
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation level (default: 2)")
    parser.add_argument("--all-groups", action="store_true", help="Extract all detected groups")
    parser.add_argument("--include-empty", action="store_true", help="Include empty pairs/windows")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Keep schedules within the last N days (and future dates). Older schedules are dropped (e.g. 10)."
    )
    parser.add_argument(
        "--clean-files",
        action="store_true",
        help="Automatically delete PDF files older than retention-days from disk."
    )
    parser.add_argument(
        "--as-list",
        action="store_true",
        help="Always output a JSON array even when only one schedule is parsed."
    )
    parser.add_argument(
        "--deduplicate",
        action="store_true",
        help="Deduplicate schedules by date and group, keeping only the newest update."
    )

    args = parser.parse_args()

    try:
        # Collect all valid PDF files from inputs
        pdf_files = collect_pdf_files(args.pdf_inputs)
        if not pdf_files:
            print("Error: No PDF files found matching the given inputs.", file=sys.stderr)
            sys.exit(1)

        # Mode A: Save each file individually into an output directory
        if args.out_dir:
            os.makedirs(args.out_dir, exist_ok=True)
            saved_count = 0
            for pdf_path in pdf_files:
                base_name = os.path.splitext(os.path.basename(pdf_path))[0]
                if args.all_groups:
                    data = parse_all_groups_from_pdf(pdf_path, date=args.date, week=args.week)
                else:
                    data = parse_schedule_pdf(
                        pdf_path,
                        group=args.group,
                        date=args.date,
                        week=args.week,
                        include_empty=args.include_empty
                    )

                grp_suffix = f"_{args.group}" if args.group else ""
                out_path = os.path.join(args.out_dir, f"{base_name}{grp_suffix}.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=args.indent)
                    f.write("\n")
                saved_count += 1
                print(f"Saved: {out_path}")
            print(f"Successfully processed {saved_count} PDF file(s) into {args.out_dir}")
            return

        # Mode B: Single PDF file input without list/retention requested -> single JSON object / result
        if len(pdf_files) == 1 and not args.as_list and args.retention_days is None:
            pdf_path = pdf_files[0]
            if args.all_groups:
                data = parse_all_groups_from_pdf(pdf_path, date=args.date, week=args.week)
            else:
                data = parse_schedule_pdf(
                    pdf_path,
                    group=args.group,
                    date=args.date,
                    week=args.week,
                    include_empty=args.include_empty
                )
        # Mode C: Multiple PDF files, or retention filtering, or --as-list requested -> combined list
        else:
            dedup = args.deduplicate or (args.retention_days is not None)
            data = parse_multiple_pdfs(
                pdf_files,
                group=args.group,
                date=args.date,
                week=args.week,
                include_empty=args.include_empty,
                all_groups=args.all_groups,
                retention_days=args.retention_days,
                clean_files=args.clean_files,
                deduplicate=dedup,
            )

        json_output = json.dumps(data, ensure_ascii=False, indent=args.indent)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(json_output + "\n")
            print(f"Successfully saved parsed schedule(s) to {args.output}")
        else:
            print(json_output)

    except Exception as e:
        print(f"Error parsing PDF schedule: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
