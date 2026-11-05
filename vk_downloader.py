#!/usr/bin/env python3
"""
vk_downloader.py - Module and CLI tool to fetch schedule PDFs from a VK community.

Downloads the latest matching schedule PDF from a VK group wall,
avoids redundant downloads using a local state file, and prints the downloaded or
already up-to-date file path to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

logger = logging.getLogger("vk_downloader")


# ==============================================================================
# Exceptions
# ==============================================================================

class VKDownloaderError(Exception):
    """Base exception for VKDownloader errors."""
    pass


class VKAuthError(VKDownloaderError):
    """Raised when authentication / token parameters are invalid or missing."""
    pass


class VKAPIError(VKDownloaderError):
    """Raised when the VK API returns an error response."""
    def __init__(self, code: int, message: str, raw_response: Optional[dict] = None):
        super().__init__(f"VK API error {code}: {message}")
        self.code = code
        self.message = message
        self.raw_response = raw_response or {}


class ScheduleNotFoundError(VKDownloaderError):
    """Raised when no matching schedule PDF attachment is found."""
    pass


# ==============================================================================
# Helpers
# ==============================================================================

def normalize_keyword(keyword: str) -> str:
    """Strip and normalize whitespace in a keyword string."""
    return re.sub(r"\s+", " ", keyword.strip().lower())


def parse_keywords(val: Optional[Union[str, List[str]]]) -> List[str]:
    """
    Parse keywords from a string (comma or semicolon separated) or list of strings.
    Empty items are removed and whitespace is trimmed.
    """
    if not val:
        return []
    if isinstance(val, str):
        parts = re.split(r"[,;]+", val)
    else:
        parts = []
        for item in val:
            parts.extend(re.split(r"[,;]+", item))
    return [p.strip() for p in parts if p.strip()]


def matches_keywords(
    title: str,
    keywords: Optional[List[str]] = None,
    regex_pattern: Optional[Union[str, re.Pattern]] = None,
) -> bool:
    """
    Check if a document title matches the specified keywords and/or regex.

    - Matching is case-insensitive.
    - If regex_pattern is provided, title must match regex search.
    - If keywords are provided, ALL keywords must match the title.
      Punctuation and separators (hyphens, dots, underscores, spaces) between
      numbers and words (e.g. '3-4 курс' vs '3.4 курс') are matched flexibly.
    """
    if not title:
        return False

    title_clean = title.strip()

    # 1. Regex check if provided
    if regex_pattern:
        if isinstance(regex_pattern, str):
            compiled = re.compile(regex_pattern, re.IGNORECASE)
        else:
            compiled = regex_pattern
        if not compiled.search(title_clean):
            return False

    if not keywords:
        return True

    title_lower = title_clean.lower()

    for kw in keywords:
        kw_norm = normalize_keyword(kw)
        if not kw_norm:
            continue

        # Direct substring check
        if kw_norm in title_lower:
            continue

        # Flexible pattern check: allow separators like [-._ /] interchangeably
        # E.g. '3-4 курс' matches '3.4 курс', '3 4 курс', '3-4курс', etc.
        tokens = re.split(r"[\s\-_.,/]+", kw_norm)
        if len(tokens) > 1 and all(tokens):
            flexible_regex = r"[\s\-_.,/]*".join(re.escape(t) for t in tokens)
            if re.search(flexible_regex, title_lower):
                continue

        return False

    return True


def normalize_group_id(group_id: Optional[Union[int, str]]) -> Optional[int]:
    """
    Normalize group ID to a negative integer for VK wall.get owner_id parameter.
    Handles 'club123', 'public123', positive or negative integer strings/ints.
    Returns None if group_id is not a numeric group identifier.
    """
    if group_id is None:
        return None
    s = str(group_id).strip()
    if not s:
        return None

    # Strip prefixes like 'club', 'public', 'event'
    s = re.sub(r"^(club|public|event)", "", s, flags=re.IGNORECASE)

    try:
        num = int(s)
        # In VK API, group owner_id is negative
        return -abs(num)
    except ValueError:
        return None


def sanitize_filename(name: str, fallback_ext: str = ".pdf") -> str:
    """
    Sanitize filename for safe local disk storage, preserving Unicode characters.
    """
    if not name:
        return f"schedule_{int(datetime.now().timestamp())}{fallback_ext}"
    # Replace invalid filesystem characters and path traversal
    s = re.sub(r'[/\\?%*:|"<>]+', "_", name)
    s = s.strip(". ")
    if not s:
        return f"schedule_{int(datetime.now().timestamp())}{fallback_ext}"
    if not s.lower().endswith(".pdf"):
        s += fallback_ext
    return s


# ==============================================================================
# State File Management
# ==============================================================================

def load_state(state_file: Union[str, Path]) -> Dict[str, Any]:
    """Load local state file JSON data."""
    path = Path(state_file)
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read state file {path}: {e}")
        return {}


def save_state(state_file: Union[str, Path], state: Dict[str, Any]) -> None:
    """Atomically save state file JSON data."""
    path = Path(state_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f".tmp.{os.getpid()}")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        temp_path.replace(path)
    except OSError as e:
        logger.warning(f"Failed to save state file {path}: {e}")


def is_doc_up_to_date(
    state: Dict[str, Any],
    doc: Dict[str, Any],
    target_path: Path,
) -> bool:
    """
    Determine if target file is already downloaded and up to date.
    Checks doc ID, owner ID, file existence on disk, and size if available.
    """
    if not target_path.is_file():
        return False

    state_doc_id = state.get("doc_id")
    state_owner_id = state.get("owner_id")
    doc_id = doc.get("id")
    doc_owner_id = doc.get("owner_id")

    if state_doc_id is not None and doc_id is not None:
        if state_doc_id == doc_id and (state_owner_id is None or state_owner_id == doc_owner_id):
            doc_size = doc.get("size")
            if doc_size and target_path.stat().st_size != doc_size:
                return False
            return True

    return False


# ==============================================================================
# VKDownloader Class
# ==============================================================================

class VKDownloader:
    """
    Downloader for schedule PDFs from a VK community wall.
    """
    VK_API_BASE = "https://api.vk.com/method"

    def __init__(
        self,
        token: str,
        group_id: Optional[Union[str, int]] = None,
        domain: Optional[str] = None,
        api_version: str = "5.199",
        session: Optional[requests.Session] = None,
        timeout: int = 30,
    ):
        if not token:
            raise VKAuthError("VK service token is required")

        self.token = str(token).strip()
        self.api_version = api_version
        self.timeout = timeout
        self.session = session or requests.Session()

        self.group_id: Optional[int] = None
        self.domain: Optional[str] = None

        if group_id is not None:
            norm_id = normalize_group_id(group_id)
            if norm_id is not None:
                self.group_id = norm_id
            else:
                # If non-numeric string given as group_id, treat as domain
                if not domain:
                    self.domain = str(group_id).strip()

        if domain:
            self.domain = domain.strip().lstrip("@")

        if self.group_id is None and not self.domain:
            raise ValueError("Either group_id or domain must be provided")

    def _call_api(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Make an authenticated call to VK API and return the 'response' object."""
        url = f"{self.VK_API_BASE}/{method}"
        req_params = {
            "access_token": self.token,
            "v": self.api_version,
            **params,
        }
        try:
            resp = self.session.get(url, params=req_params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise VKAPIError(0, f"HTTP request failed: {e}") from e
        except json.JSONDecodeError as e:
            raise VKAPIError(0, f"Invalid JSON in VK response: {e}") from e

        if "error" in data:
            err = data["error"]
            code = err.get("error_code", -1)
            msg = err.get("error_msg", "Unknown error")
            raise VKAPIError(code, msg, raw_response=err)

        return data.get("response", {})

    def get_wall_posts(
        self,
        count: int = 20,
        filter_type: str = "owner",
    ) -> List[Dict[str, Any]]:
        """Fetch latest posts from the community wall."""
        params: Dict[str, Any] = {"count": count, "filter": filter_type}
        if self.group_id is not None:
            params["owner_id"] = self.group_id
        elif self.domain:
            params["domain"] = self.domain

        response_data = self._call_api("wall.get", params)
        return response_data.get("items", [])

    def find_schedule_doc(
        self,
        posts: List[Dict[str, Any]],
        keywords: Optional[List[str]] = None,
        regex_pattern: Optional[Union[str, re.Pattern]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Scan wall posts for attachments of type 'doc' with .pdf extension
        matching the keywords or regex pattern.
        Returns a dict with {'doc': doc_obj, 'post': post_obj} or None.
        """
        for post in posts:
            # Collect attachments from the post itself
            attachments = list(post.get("attachments", []))

            # Also inspect reposted attachments (copy_history)
            for repost in post.get("copy_history", []):
                attachments.extend(repost.get("attachments", []))

            for att in attachments:
                if att.get("type") != "doc":
                    continue
                doc = att.get("doc")
                if not doc or not isinstance(doc, dict):
                    continue

                ext = str(doc.get("ext", "")).lower()
                title = str(doc.get("title", ""))

                if ext != "pdf" and not title.lower().endswith(".pdf"):
                    continue

                if matches_keywords(title, keywords=keywords, regex_pattern=regex_pattern):
                    return {"doc": doc, "post": post}

        return None

    def find_all_schedule_docs(
        self,
        posts: List[Dict[str, Any]],
        keywords: Optional[List[str]] = None,
        regex_pattern: Optional[Union[str, re.Pattern]] = None,
        days: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scan wall posts for attachments of type 'doc' with .pdf extension
        matching the keywords or regex pattern.
        If days is provided, only includes posts from the last `days` days.
        Returns a list of dicts: [{'doc': doc, 'post': post}, ...]
        """
        now = datetime.now(timezone.utc)
        matches = []
        seen_doc_ids = set()

        for post in posts:
            post_time = post.get("date")
            if days is not None and post_time:
                post_dt = datetime.fromtimestamp(post_time, tz=timezone.utc)
                if (now - post_dt).days > days:
                    continue

            attachments = list(post.get("attachments", []))
            for repost in post.get("copy_history", []):
                attachments.extend(repost.get("attachments", []))

            for att in attachments:
                if att.get("type") != "doc":
                    continue
                doc = att.get("doc")
                if not doc or not isinstance(doc, dict):
                    continue

                ext = str(doc.get("ext", "")).lower()
                title = str(doc.get("title", ""))

                if ext != "pdf" and not title.lower().endswith(".pdf"):
                    continue

                doc_id = (doc.get("owner_id"), doc.get("id"))
                if doc_id in seen_doc_ids:
                    continue

                if matches_keywords(title, keywords=keywords, regex_pattern=regex_pattern):
                    seen_doc_ids.add(doc_id)
                    matches.append({"doc": doc, "post": post})

        return matches

    def get_doc_download_url(self, doc: Dict[str, Any]) -> str:
        """Get direct download URL for document, querying docs.getById if needed."""
        url = doc.get("url")
        if url:
            return url

        # Fallback to docs.getById if URL is not present in wall attachment
        owner_id = doc.get("owner_id")
        doc_id = doc.get("id")
        if owner_id is not None and doc_id is not None:
            logger.debug(f"Direct doc URL missing, calling docs.getById for {owner_id}_{doc_id}...")
            res = self._call_api("docs.getById", {"docs": f"{owner_id}_{doc_id}"})
            items = []
            if isinstance(res, list):
                items = res
            elif isinstance(res, dict) and "items" in res:
                items = res.get("items", [])
            if items and isinstance(items[0], dict) and items[0].get("url"):
                return items[0]["url"]

        raise VKDownloaderError(f"Could not determine download URL for doc id={doc_id}")

    def download_file(
        self,
        url: str,
        destination_path: Path,
        chunk_size: int = 65536,
    ) -> Tuple[Path, str]:
        """Stream download file to destination_path and calculate sha256 checksum."""
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination_path.with_suffix(f".part.{os.getpid()}")

        hasher = hashlib.sha256()
        try:
            with self.session.get(url, stream=True, timeout=self.timeout) as resp:
                resp.raise_for_status()
                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            hasher.update(chunk)
            temp_path.replace(destination_path)
        except Exception:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

        return destination_path, hasher.hexdigest()

    def fetch_schedule(
        self,
        keywords: Optional[Union[str, List[str]]] = None,
        regex_pattern: Optional[Union[str, re.Pattern]] = None,
        output_dir: Union[str, Path] = "./downloads",
        state_file: Optional[Union[str, Path]] = ".last_vk_doc.json",
        count: int = 20,
        force: bool = False,
    ) -> Tuple[Path, bool]:
        """
        High-level sync workflow:
        1. Fetch wall posts.
        2. Find matching PDF doc.
        3. Check state to avoid redundant downloads.
        4. Download if needed.
        5. Update state.

        Returns (file_path, downloaded_fresh).
        """
        kw_list = parse_keywords(keywords)
        out_dir = Path(output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        logger.debug(f"Fetching latest {count} posts from VK wall...")
        posts = self.get_wall_posts(count=count, filter_type="owner")
        if not posts:
            # If owner filter returned nothing, retry with filter="all"
            logger.debug("No posts with filter='owner', retrying with filter='all'...")
            posts = self.get_wall_posts(count=count, filter_type="all")

        match = self.find_schedule_doc(posts, keywords=kw_list, regex_pattern=regex_pattern)
        if not match:
            filter_desc = []
            if kw_list:
                filter_desc.append(f"keywords={kw_list}")
            if regex_pattern:
                filter_desc.append(f"regex='{regex_pattern}'")
            desc = " and ".join(filter_desc) if filter_desc else "no filters"
            raise ScheduleNotFoundError(
                f"No matching schedule PDF attachment found in recent posts ({desc})."
            )

        doc = match["doc"]
        post = match["post"]
        doc_title = doc.get("title", "schedule.pdf")
        filename = sanitize_filename(doc_title)
        target_path = out_dir / filename

        state: Dict[str, Any] = {}
        if state_file:
            state = load_state(state_file)

        # Redundancy check
        if not force and state_file and is_doc_up_to_date(state, doc, target_path):
            logger.info(f"Schedule is already up to date: {target_path}")
            return target_path, False

        # Download required
        logger.info(f"Downloading schedule: '{doc_title}' -> {target_path}")
        download_url = self.get_doc_download_url(doc)
        saved_path, sha256_hash = self.download_file(download_url, target_path)

        if state_file:
            new_state = {
                "post_id": post.get("id"),
                "doc_id": doc.get("id"),
                "owner_id": doc.get("owner_id"),
                "title": doc_title,
                "saved_path": str(saved_path),
                "size": saved_path.stat().st_size,
                "sha256": sha256_hash,
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(state_file, new_state)

        return saved_path, True

    def fetch_all_schedules(
        self,
        keywords: Optional[Union[str, List[str]]] = None,
        regex_pattern: Optional[Union[str, re.Pattern]] = None,
        output_dir: Union[str, Path] = "./downloads",
        days: int = 10,
        count: int = 50,
        force: bool = False,
    ) -> List[Path]:
        """
        Fetch all matching schedule PDFs from recent wall posts within the last `days` days.
        Automatically checks existing files in output_dir and downloads only not-yet-downloaded files.
        Returns a list of all matching Path objects in output_dir.
        """
        kw_list = parse_keywords(keywords)
        out_dir = Path(output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        logger.debug(f"Fetching latest {count} posts from VK wall for last {days} days...")
        posts = self.get_wall_posts(count=count, filter_type="owner")
        if not posts:
            posts = self.get_wall_posts(count=count, filter_type="all")

        matches = self.find_all_schedule_docs(posts, keywords=kw_list, regex_pattern=regex_pattern, days=days)
        if not matches:
            single = self.find_schedule_doc(posts, keywords=kw_list, regex_pattern=regex_pattern)
            if single:
                matches = [single]
            else:
                raise ScheduleNotFoundError(
                    f"No matching schedule PDF attachments found in recent posts (days={days})."
                )

        saved_paths: List[Path] = []

        for item in matches:
            doc = item["doc"]
            doc_title = doc.get("title", "schedule.pdf")
            filename = sanitize_filename(doc_title)
            target_path = out_dir / filename

            # Check if file is already downloaded and complete
            doc_size = doc.get("size")
            if not force and target_path.is_file() and (not doc_size or target_path.stat().st_size == doc_size):
                logger.info(f"Schedule already downloaded and up to date: {target_path}")
                saved_paths.append(target_path)
                continue

            # Download missing/updated schedule
            logger.info(f"Downloading new schedule: '{doc_title}' -> {target_path}")
            download_url = self.get_doc_download_url(doc)
            saved_file, _ = self.download_file(download_url, target_path)
            saved_paths.append(saved_file)

        return saved_paths


# ==============================================================================
# Public Function API
# ==============================================================================

def fetch_schedule(
    token: Optional[str] = None,
    group_id: Optional[Union[str, int]] = None,
    domain: Optional[str] = None,
    keywords: Optional[Union[str, List[str]]] = None,
    regex_pattern: Optional[Union[str, re.Pattern]] = None,
    output_dir: Union[str, Path] = "./downloads",
    state_file: Optional[Union[str, Path]] = ".last_vk_doc.json",
    count: int = 20,
    force: bool = False,
    return_status: bool = False,
) -> Union[Path, Tuple[Path, bool]]:
    """
    Convenience function to fetch schedule PDF.
    Reads missing arguments from environment variables if not provided:
      VK_SERVICE_TOKEN, VK_GROUP_ID, VK_DOMAIN, TARGET_KEYWORDS, TARGET_REGEX, OUTPUT_DIR.

    Returns:
      Path if return_status is False (default)
      (Path, is_fresh_bool) if return_status is True
    """
    token = token or os.environ.get("VK_SERVICE_TOKEN")
    if not token:
        raise VKAuthError("VK service token is required (pass token or set VK_SERVICE_TOKEN)")

    group_id = group_id if group_id is not None else os.environ.get("VK_GROUP_ID")
    domain = domain or os.environ.get("VK_DOMAIN")

    if keywords is None and regex_pattern is None:
        keywords = os.environ.get("TARGET_KEYWORDS")
        regex_pattern = os.environ.get("TARGET_REGEX")

    downloader = VKDownloader(token=token, group_id=group_id, domain=domain)
    path, is_fresh = downloader.fetch_schedule(
        keywords=keywords,
        regex_pattern=regex_pattern,
        output_dir=output_dir,
        state_file=state_file,
        count=count,
        force=force,
    )
    if return_status:
        return path, is_fresh
    return path


# ==============================================================================
# CLI Entry Point
# ==============================================================================

def main(argv: Optional[List[str]] = None) -> int:
    """
    CLI entry point. Parses args and environment variables, runs the downloader,
    prints saved file path to stdout, and exits with 0 on success or non-zero on error.
    """
    parser = argparse.ArgumentParser(
        description="Automatically fetch schedule PDFs from a VK community."
    )
    parser.add_argument(
        "-t", "--token",
        default=os.environ.get("VK_SERVICE_TOKEN"),
        help="VK service token / access token (fallback: VK_SERVICE_TOKEN env var)",
    )
    parser.add_argument(
        "-g", "--group-id",
        default=os.environ.get("VK_GROUP_ID"),
        help="VK community ID (numeric or club12345, fallback: VK_GROUP_ID env var)",
    )
    parser.add_argument(
        "-d", "--domain",
        default=os.environ.get("VK_DOMAIN"),
        help="VK community domain / screen name (fallback: VK_DOMAIN env var)",
    )
    parser.add_argument(
        "-k", "--keywords",
        nargs="*",
        default=None,
        help="Target keywords to filter doc filename (fallback: TARGET_KEYWORDS env var)",
    )
    parser.add_argument(
        "-r", "--regex",
        default=os.environ.get("TARGET_REGEX"),
        help="Regex pattern to filter doc filename (fallback: TARGET_REGEX env var)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=os.environ.get("OUTPUT_DIR", "./downloads"),
        help="Directory to save downloaded PDF (default: ./downloads, fallback: OUTPUT_DIR env var)",
    )
    parser.add_argument(
        "-s", "--state-file",
        default=os.environ.get("STATE_FILE", ".last_vk_doc.json"),
        help="Path to state file tracking latest download (default: .last_vk_doc.json)",
    )
    parser.add_argument(
        "-c", "--count",
        type=int,
        default=int(os.environ.get("VK_COUNT", "20")),
        help="Number of wall posts to scan (default: 20, fallback: VK_COUNT env var)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Download all matching schedules posted within the last N days (default: fetch latest single schedule)",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force re-download even if state indicates file is already up to date",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose debug logging to stderr",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Quiet mode: suppress progress messages, output only file path on stdout",
    )

    args = parser.parse_args(argv)

    # Configure logging strictly to stderr so stdout remains clean
    log_level = logging.ERROR if args.quiet else (logging.DEBUG if args.verbose else logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s" if args.verbose else "%(message)s",
        stream=sys.stderr,
        force=True,
    )

    # 1. Validate token
    if not args.token:
        sys.stderr.write(
            "Error: VK service token is required. Specify via VK_SERVICE_TOKEN environment "
            "variable or --token flag.\n"
        )
        return 1

    # 2. Validate group identifier
    group_id = args.group_id
    domain = args.domain
    if not group_id and not domain:
        sys.stderr.write(
            "Error: VK community identifier is required. Specify via VK_GROUP_ID / VK_DOMAIN "
            "environment variables or --group-id / --domain flags.\n"
        )
        return 1

    # 3. Resolve keywords & regex
    if args.keywords is not None and len(args.keywords) > 0:
        keywords = parse_keywords(args.keywords)
    else:
        keywords = parse_keywords(os.environ.get("TARGET_KEYWORDS"))

    regex_pattern = args.regex or os.environ.get("TARGET_REGEX")

    if not keywords and not regex_pattern:
        sys.stderr.write(
            "Error: Filter keywords or regex pattern required. Specify via TARGET_KEYWORDS "
            "environment variable, --keywords, or --regex.\n"
        )
        return 1

    # 4. Execute fetch
    try:
        downloader = VKDownloader(
            token=args.token,
            group_id=group_id,
            domain=domain,
        )
        if args.days is not None:
            saved_paths = downloader.fetch_all_schedules(
                keywords=keywords,
                regex_pattern=regex_pattern,
                output_dir=args.output_dir,
                days=args.days,
                count=max(args.count, 50),
                force=args.force,
            )
            for p in saved_paths:
                print(str(p), flush=True)
            return 0
        else:
            saved_path, _ = downloader.fetch_schedule(
                keywords=keywords,
                regex_pattern=regex_pattern,
                output_dir=args.output_dir,
                state_file=args.state_file,
                count=args.count,
                force=args.force,
            )
            # Print saved file path to stdout upon success
            print(str(saved_path), flush=True)
            return 0

    except ScheduleNotFoundError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 3
    except VKAuthError as e:
        sys.stderr.write(f"Authentication error: {e}\n")
        return 1
    except VKAPIError as e:
        sys.stderr.write(f"VK API error: {e}\n")
        return 2
    except VKDownloaderError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 2
    except Exception as e:
        sys.stderr.write(f"Unexpected error: {e}\n")
        return 4


if __name__ == "__main__":
    sys.exit(main())
