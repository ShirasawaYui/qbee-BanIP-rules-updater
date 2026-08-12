"""Download BTN-Collected-Rules and write a comment-free rule list.

The source file is published through GitHub's blob URL.  The downloader
normalizes that URL to the raw endpoint, removes comment and blank lines,
then atomically replaces the output file.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

DEFAULT_URL = (
    "https://github.com/PBH-BTN/BTN-Collected-Rules/blob/"
    "main/combine/all.txt"
)
DEFAULT_OUTPUT = "BTN.txt"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = "BTN-rules-downloader/1.0 (+https://github.com/PBH-BTN/BTN-Collected-Rules)"


class DownloadError(RuntimeError):
    """Raised when the source cannot be downloaded or decoded."""


def raw_url(url: str) -> str:
    """Convert a GitHub ``blob`` URL to its raw-content URL."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"无效的 HTTP(S) URL: {url}")
    parts = parsed.path.strip("/").split("/")
    if parsed.netloc.lower() == "github.com" and len(parts) >= 5 and parts[2] == "blob":
        owner, repo, _, ref = parts[:4]
        path = "/".join(parts[4:])
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    return url


def fetch_text(url: str = DEFAULT_URL, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Fetch and decode the source file as UTF-8 text."""
    if timeout <= 0:
        raise ValueError("请求超时必须大于 0 秒")
    request = urllib.request.Request(
        raw_url(url),
        headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode() or 200
            if status != 200:
                raise DownloadError(f"HTTP {status}")
            data = response.read()
    except DownloadError:
        raise
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"HTTP {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise DownloadError(f"网络请求失败: {exc}") from exc
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DownloadError("响应不是有效的 UTF-8 文本") from exc


def remove_comments(lines: Iterable[str]) -> list[str]:
    """Return non-empty lines whose first non-space character is not ``#``."""
    cleaned: list[str] = []
    for line in lines:
        value = line.strip()
        if value and not value.startswith("#"):
            cleaned.append(value)
    return cleaned


def write_rules(path: Path | str, rules: Iterable[str]) -> None:
    """Atomically write rules as UTF-8 with LF line endings."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(remove_comments(rules))
    if content:
        content += "\n"
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取 BTN 规则并移除注释后保存")
    parser.add_argument("--url", default=DEFAULT_URL, help="GitHub 文件地址")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="输出文件（默认 BTN.txt）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="请求超时秒数")
    parser.add_argument("--verbose", action="store_true", help="显示处理进度")
    args = parser.parse_args(argv)
    try:
        if args.verbose:
            print(f"正在下载 {raw_url(args.url)}...")
        text = fetch_text(args.url, args.timeout)
        rules = remove_comments(text.splitlines())
        write_rules(args.output, rules)
        if args.verbose:
            print(f"已写入 {args.output}，共 {len(rules)} 行")
        return 0
    except (DownloadError, ValueError, OSError) as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
