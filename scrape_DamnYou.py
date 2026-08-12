"""Extract IP rules from the code blocks in a Tencent Docs section.

The document stores its visible text and formatting operations in a Base64-
encoded protobuf-like wire payload.  Code block bodies are stored at the end
of that payload, while drawing placeholders in the main story identify where
those blocks are displayed.  This module deliberately follows that structure;
it never scans the whole document for IP-looking text.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import html as html_module
import ipaddress
import json
import os
import re
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_URL = "https://docs.qq.com/doc/DQnJBTGJjSFZBR2JW"
DEFAULT_OUTPUT = "DamnYou.txt"
DEFAULT_TIMEOUT = 30.0

START_HEADING = "对策B：屏蔽涉及的IP段"
END_HEADING = "对策C：对上传限速"
JSONP_CALLBACK = "clientVarsCallback"
CODE_STYLE_PREFIX = "melo-codeblock-Base-theme-"
CODE_STYLE_NAMES = frozenset({"HTML Code"})

WIRE_KIND_BY_FIELD = {
    201: "run",
    202: "paragraph",
    203: "section",
    208: "commentContent",
    209: "textboxContent",
    210: "story",
    211: "field",
    212: "commentRef",
    215: "drawing",
    216: "table",
    217: "tableRow",
    218: "tableCell",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ScrapeError(Exception):
    exit_code = 1


class FetchError(ScrapeError):
    exit_code = 2


class ParseError(ScrapeError):
    exit_code = 3


class SectionError(ScrapeError):
    exit_code = 4


class CodeBlockError(ScrapeError):
    exit_code = 5


class NoValidRangesError(ScrapeError):
    exit_code = 6


@dataclass(slots=True)
class WireNode:
    path: tuple[int, ...]
    field: int
    wire_type: int
    start: int
    end: int
    varint: Optional[int] = None
    fixed: Optional[int] = None
    data: Optional[bytes] = None
    text: Optional[str] = None
    children: list["WireNode"] = field(default_factory=list)


@dataclass(slots=True)
class TextRun:
    begin: int
    end: int
    kind: str
    props: dict = field(default_factory=dict)


@dataclass(slots=True)
class DocumentContent:
    title: str
    text: str
    runs: list[TextRun]
    style_names: dict[str, str]


@dataclass(slots=True)
class DocumentPayload:
    url: str
    title: str
    attributed_text_b64: str


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ParseError("protobuf 数据在 varint 处截断")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ParseError("protobuf varint 长度超过 64 位")


def _parse_wire_message(data: bytes, base: int, path: tuple[int, ...]) -> list[WireNode]:
    nodes: list[WireNode] = []
    pos = 0
    while pos < len(data):
        key_start = pos
        key, pos = _read_varint(data, pos)
        field_no = key >> 3
        wire_type = key & 7
        if field_no == 0:
            raise ParseError("protobuf 字段号不能为 0")
        node = WireNode(
            path=path + (field_no,),
            field=field_no,
            wire_type=wire_type,
            start=base + key_start,
            end=base + pos,
        )
        if wire_type == 0:
            node.varint, pos = _read_varint(data, pos)
        elif wire_type == 1:
            if pos + 8 > len(data):
                raise ParseError("protobuf fixed64 字段越界")
            node.fixed = struct.unpack("<Q", data[pos : pos + 8])[0]
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise ParseError("protobuf length-delimited 字段越界")
            raw = data[pos : pos + length]
            pos += length
            node.data = raw
            try:
                node.text = raw.decode("utf-8")
            except UnicodeDecodeError:
                node.text = None
            try:
                node.children = _parse_wire_message(raw, node.start, node.path)
            except ParseError:
                node.children = []
        elif wire_type == 5:
            if pos + 4 > len(data):
                raise ParseError("protobuf fixed32 字段越界")
            node.fixed = struct.unpack("<I", data[pos : pos + 4])[0]
            pos += 4
        else:
            raise ParseError(f"不支持的 protobuf wire type {wire_type}")
        node.end = base + pos
        nodes.append(node)
    return nodes


def decode_wire(data: bytes) -> list[WireNode]:
    if not data:
        raise ParseError("protobuf 载荷为空")
    return _parse_wire_message(data, 0, ())


def _decode_base64(value: str, label: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ParseError(f"{label} 不是非空字符串")
    cleaned = "".join(value.split())
    try:
        return base64.b64decode(cleaned, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ParseError(f"{label} Base64 载荷损坏: {error}") from error


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _http_get(
    opener: urllib.request.OpenerDirector, url: str, timeout: float
) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Referer": "https://docs.qq.com/",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.getcode() or 200
            if status != 200:
                raise FetchError(f"HTTP {status}: {url}")
            content_type = response.headers.get("Content-Type") or ""
            charset_match = re.search(r"charset=([\w-]+)", content_type, re.I)
            charset = charset_match.group(1) if charset_match else "utf-8"
            return response.read(), charset
    except FetchError:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        raise FetchError(f"HTTP {error.code}: {url}") from error
    except urllib.error.URLError as error:
        raise FetchError(f"网络请求失败: {error.reason}") from error
    except (OSError, ValueError, TimeoutError) as error:
        raise FetchError(f"网络请求失败: {error}") from error


def _decode_text(data: bytes, charset: str) -> str:
    encodings = []
    for encoding in (charset, "utf-8", "gbk"):
        normalized = (encoding or "").lower()
        if normalized and normalized not in encodings:
            encodings.append(normalized)
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    raise ParseError("HTTP 响应无法按声明字符集、UTF-8 或 GBK 解码")


def _find_opendoc_url(page_html: str, page_url: str) -> str:
    match = re.search(
        r"<(?:link|script)\b[^>]*\b(?:href|src)\s*=\s*([\"'])([^\"']*dop-api/opendoc[^\"']*)\1",
        page_html,
        re.I,
    )
    if not match:
        raise ParseError("页面 HTML 中未找到 dop-api/opendoc 地址")
    href = html_module.unescape(match.group(2))
    api_url = urllib.parse.urljoin(page_url, href)
    parsed = urllib.parse.urlparse(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ParseError("dop-api/opendoc 地址不是有效的 HTTP(S) URL")
    return api_url


def _extract_jsonp(text: str) -> dict:
    pattern = r"\s*" + re.escape(JSONP_CALLBACK) + r"\s*\((.*)\)\s*;?\s*"
    match = re.fullmatch(pattern, text, re.S)
    if not match:
        raise ParseError(f"接口响应不是 {JSONP_CALLBACK}(...) JSONP 格式")
    try:
        value = json.loads(match.group(1))
    except (TypeError, ValueError) as error:
        raise ParseError(f"JSONP 中的 JSON 解析失败: {error}") from error
    if not isinstance(value, dict):
        raise ParseError("JSONP 顶层不是 JSON 对象")
    return value


def _attributed_text_b64(client_vars: dict) -> str:
    collab = client_vars.get("collab_client_vars")
    attributed = collab.get("initialAttributedText") if isinstance(collab, dict) else None
    entries = attributed.get("text") if isinstance(attributed, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ParseError("clientVars.collab_client_vars.initialAttributedText.text[0] 缺失")
    value = entries[0]
    if not isinstance(value, str):
        raise ParseError("initialAttributedText.text[0] 不是 Base64 字符串")
    _decode_base64(value, "initialAttributedText.text[0]")
    return value


def fetch_document_payload(url: str, timeout: float = DEFAULT_TIMEOUT) -> DocumentPayload:
    if timeout <= 0:
        raise FetchError("请求超时必须大于 0 秒")
    opener = _build_opener()
    page_bytes, page_charset = _http_get(opener, url, timeout)
    page_html = _decode_text(page_bytes, page_charset)
    api_url = _find_opendoc_url(page_html, url)
    api_bytes, api_charset = _http_get(opener, api_url, timeout)
    payload = _extract_jsonp(_decode_text(api_bytes, api_charset))
    client_vars = payload.get("clientVars")
    if not isinstance(client_vars, dict):
        raise ParseError("JSONP 中缺少 clientVars 对象")
    title = client_vars.get("title") or client_vars.get("padTitle") or ""
    return DocumentPayload(
        url=url,
        title=title if isinstance(title, str) else "",
        attributed_text_b64=_attributed_text_b64(client_vars),
    )


def _first_child(node: Optional[WireNode], field_no: int) -> Optional[WireNode]:
    if node is None:
        return None
    return next((child for child in node.children if child.field == field_no), None)


def _child_path(node: Optional[WireNode], fields: tuple[int, ...]) -> Optional[WireNode]:
    current = node
    for field_no in fields:
        current = _first_child(current, field_no)
        if current is None:
            return None
    return current


def _deepest_field1_text(node: Optional[WireNode]) -> Optional[str]:
    if node is None:
        return None
    child = _first_child(node, 1)
    if child is None:
        return node.text
    return _deepest_field1_text(child) if child.children else child.text


def _field1_varint(node: Optional[WireNode]) -> Optional[int]:
    if node is None:
        return None
    child = _first_child(node, 1)
    if child is None:
        return None
    if child.varint is not None:
        return child.varint
    return _field1_varint(child) if child.children else None


def _live_style_names(styles_root: WireNode) -> dict[str, str]:
    names: dict[str, str] = {}
    for entry in styles_root.children:
        if entry.field != 5:
            continue
        style_id = _deepest_field1_text(_first_child(entry, 1))
        name = _deepest_field1_text(_first_child(entry, 2))
        if style_id and name:
            names[style_id] = name
    return names


def _live_run_props(kind_node: WireNode) -> dict:
    kind = WIRE_KIND_BY_FIELD.get(kind_node.field)
    if kind in {"paragraph", "run"}:
        style_id = _deepest_field1_text(_first_child(kind_node, 1))
        key = "pStyle" if kind == "paragraph" else "rStyle"
        return {kind: {key: {"val": style_id}}} if style_id else {kind: {}}
    if kind == "textboxContent":
        props: dict[str, object] = {}
        block_id = _deepest_field1_text(_first_child(kind_node, 1))
        if block_id:
            props["id"] = block_id
        if _first_child(kind_node, 3) is not None:
            props["isBegin"] = True
        if _first_child(kind_node, 7) is not None:
            props["isCodeBlock"] = True
        return {"textboxContent": props}
    if kind == "drawing":
        # Tencent Docs stores the referenced textbox ID at this fixed path.
        block_id_node = _child_path(kind_node, (3, 9, 1, 5, 13, 7, 1))
        block_id = block_id_node.text if block_id_node is not None else None
        return {"drawing": {"codeBlockId": block_id}} if block_id else {"drawing": {}}
    return {kind: {}} if kind else {}


def _document_text(nodes: list[WireNode]) -> str:
    candidates: list[str] = []
    for root in nodes:
        if root.field != 1:
            continue
        for entry in root.children:
            if entry.field != 2:
                continue
            text_node = _child_path(entry, (6, 1))
            if text_node is not None and text_node.text is not None:
                candidates.append(text_node.text)
    if len(candidates) != 1 or not candidates[0]:
        raise ParseError("wire 载荷中未找到唯一的正文节点 1/2/6/1")
    return candidates[0]


def _content_from_live_wire(nodes: list[WireNode], title: str) -> DocumentContent:
    text = _document_text(nodes)
    runs: list[TextRun] = []
    style_names: dict[str, str] = {}
    seen_mutations: set[int] = set()

    def walk(node: WireNode, parent: Optional[WireNode]) -> None:
        if node.field == 7 and parent is not None and id(parent) not in seen_mutations:
            kind_node = next(
                (child for child in node.children if child.field in WIRE_KIND_BY_FIELD), None
            )
            if kind_node is not None:
                begin = _field1_varint(_first_child(parent, 2))
                end = _field1_varint(_first_child(parent, 3))
                marker = _first_child(parent, 8)
                if begin is not None and end is not None and marker is not None:
                    seen_mutations.add(id(parent))
                    runs.append(
                        TextRun(
                            begin=begin,
                            end=end,
                            kind=WIRE_KIND_BY_FIELD[kind_node.field],
                            props=_live_run_props(kind_node),
                        )
                    )
            else:
                styles_root = _first_child(node, 104)
                if styles_root is not None:
                    style_names.update(_live_style_names(styles_root))
        for child in node.children:
            walk(child, node)

    for root in nodes:
        walk(root, None)
    if not runs:
        raise ParseError("wire 正文中未找到格式标记")
    for run in runs:
        if run.begin < 0 or run.end < run.begin or run.end > len(text):
            raise ParseError(
                f"格式标记越界: {run.kind} {run.begin}..{run.end}，正文长度 {len(text)}"
            )
    runs.sort(key=lambda item: (item.begin, item.end, item.kind))
    return DocumentContent(title=title, text=text, runs=runs, style_names=style_names)


def _normalize_document(payload: DocumentPayload) -> DocumentContent:
    if not isinstance(payload, DocumentPayload):
        raise ParseError("payload 不是 DocumentPayload")
    raw = _decode_base64(payload.attributed_text_b64, "initialAttributedText.text[0]")
    return _content_from_live_wire(decode_wire(raw), payload.title)


def _is_code_style_name(name: str) -> bool:
    return name in CODE_STYLE_NAMES or name.startswith(CODE_STYLE_PREFIX)


def _code_style_ids(content: DocumentContent) -> set[str]:
    return {
        style_id
        for style_id, name in content.style_names.items()
        if _is_code_style_name(name)
    }


def _run_style_id(run: TextRun) -> Optional[str]:
    if run.kind == "paragraph":
        return ((run.props.get("paragraph") or {}).get("pStyle") or {}).get("val")
    if run.kind == "run":
        return ((run.props.get("run") or {}).get("rStyle") or {}).get("val")
    return None


def _span_has_code_style(
    content: DocumentContent, begin: int, end: int, code_ids: set[str]
) -> bool:
    for run in content.runs:
        if _run_style_id(run) not in code_ids:
            continue
        if run.kind == "paragraph" and (
            run.begin <= end < run.end or begin <= run.begin < end
        ):
            return True
        if run.kind == "run" and run.begin < end and run.end > begin:
            return True
    return False


def _split_lines(text: str) -> list[tuple[int, int]]:
    lines: list[tuple[int, int]] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\r":
            lines.append((start, index))
            index += 2 if index + 1 < len(text) and text[index + 1] == "\n" else 1
            start = index
        elif char in {"\n", "\x05"}:
            lines.append((start, index))
            index += 1
            start = index
        else:
            index += 1
    if start < len(text):
        lines.append((start, len(text)))
    return lines


def _clean_line(raw: str) -> str:
    return "".join(char for char in raw if char > "\x1f" and char != "\x7f").strip()


def _is_valid_ip_line(line: str) -> bool:
    token = line.strip()
    if not token or token.startswith("#") or any(char.isspace() for char in token):
        return False
    if token.count("-") == 1:
        low_text, high_text = token.split("-", 1)
        try:
            low = ipaddress.ip_address(low_text)
            high = ipaddress.ip_address(high_text)
            return low.version == high.version and int(low) <= int(high)
        except ValueError:
            return False
    if "-" in token:
        return False
    try:
        if "/" in token:
            ipaddress.ip_network(token, strict=False)
        else:
            ipaddress.ip_address(token)
    except ValueError:
        return False
    return True


def _heading_bounds(
    content: DocumentContent, lines: list[tuple[int, int]], start_heading: str, end_heading: str
) -> tuple[int, int]:
    starts = [begin for begin, end in lines if _clean_line(content.text[begin:end]) == start_heading]
    ends = [begin for begin, end in lines if _clean_line(content.text[begin:end]) == end_heading]
    if not starts:
        raise SectionError(f"未找到起始标题“{start_heading}”")
    if not ends:
        raise SectionError(f"未找到结束标题“{end_heading}”")
    if len(starts) != 1 or len(ends) != 1:
        raise SectionError("起止标题不唯一，无法可靠确定章节范围")
    if starts[0] >= ends[0]:
        raise SectionError("标题顺序异常：起始标题位于结束标题之后")
    return starts[0], ends[0]


def _referenced_block_ids(
    content: DocumentContent, section_start: int, section_end: int
) -> list[tuple[int, str]]:
    references: list[tuple[int, str]] = []
    seen: set[str] = set()
    for run in content.runs:
        if run.kind != "drawing" or not (section_start <= run.begin < section_end):
            continue
        block_id = (run.props.get("drawing") or {}).get("codeBlockId")
        if isinstance(block_id, str) and block_id and block_id not in seen:
            seen.add(block_id)
            references.append((run.begin, block_id))
    return references


def _block_regions(content: DocumentContent, block_ids: Iterable[str]) -> dict[str, tuple[int, int]]:
    by_id: dict[str, list[TextRun]] = {}
    for run in content.runs:
        if run.kind != "textboxContent":
            continue
        props = run.props.get("textboxContent") or {}
        block_id = props.get("id")
        if isinstance(block_id, str) and block_id:
            by_id.setdefault(block_id, []).append(run)
    regions: dict[str, tuple[int, int]] = {}
    for block_id in block_ids:
        markers = by_id.get(block_id, [])
        begins = [run for run in markers if (run.props.get("textboxContent") or {}).get("isBegin")]
        ends = [run for run in markers if not (run.props.get("textboxContent") or {}).get("isBegin")]
        if len(begins) != 1 or len(ends) != 1:
            raise CodeBlockError(
                f"代码块 {block_id!r} 的开始/结束标记不完整或不唯一"
            )
        begin_marker, end_marker = begins[0], ends[0]
        begin_props = begin_marker.props.get("textboxContent") or {}
        end_props = end_marker.props.get("textboxContent") or {}
        if not begin_props.get("isCodeBlock") or not end_props.get("isCodeBlock"):
            raise CodeBlockError(f"代码块 {block_id!r} 缺少代码块属性")
        start, stop = begin_marker.end, end_marker.begin
        if start > stop:
            raise CodeBlockError(f"代码块 {block_id!r} 的正文边界倒置")
        regions[block_id] = (start, stop)
    return regions


def _styled_lines_in_span(
    content: DocumentContent,
    lines: list[tuple[int, int]],
    start: int,
    stop: int,
    code_ids: set[str],
) -> list[str]:
    values: list[str] = []
    for line_start, line_end in lines:
        if line_end <= start or line_start >= stop:
            continue
        begin = max(line_start, start)
        end = min(line_end, stop)
        if begin > end or not _span_has_code_style(content, begin, end, code_ids):
            continue
        value = _clean_line(content.text[begin:end])
        if _is_valid_ip_line(value):
            values.append(value)
    return values


def extract_code_ip_ranges(
    payload: DocumentPayload, start_heading: str, end_heading: str
) -> list[str]:
    content = _normalize_document(payload)
    lines = _split_lines(content.text)
    section_start, section_end = _heading_bounds(content, lines, start_heading, end_heading)
    code_ids = _code_style_ids(content)
    if not code_ids:
        raise CodeBlockError("未解析到代码块字符或段落样式")

    references = _referenced_block_ids(content, section_start, section_end)
    regions = _block_regions(content, (block_id for _, block_id in references))

    # Events preserve the order in which content appears in the visible section.
    events: list[tuple[int, int, tuple[int, int]]] = []
    for line_start, line_end in lines:
        if line_start < section_start or line_start >= section_end:
            continue
        if _span_has_code_style(content, line_start, line_end, code_ids):
            events.append((line_start, 0, (line_start, line_end)))
    for drawing_pos, block_id in references:
        events.append((drawing_pos, 1, regions[block_id]))
    events.sort(key=lambda item: (item[0], item[1]))
    if not events:
        raise CodeBlockError("目标章节中未发现带代码样式的正文或代码块引用")

    values: list[str] = []
    for _, _, (start, stop) in events:
        if start == stop:
            continue
        if not _span_has_code_style(content, start, stop, code_ids):
            raise CodeBlockError("目标章节引用的代码块缺少代码样式标记")
        values.extend(_styled_lines_in_span(content, lines, start, stop, code_ids))

    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    if not unique:
        raise NoValidRangesError("目标章节的代码块中没有有效 IP 行")
    return unique


def write_ranges(path: Path, ranges: Iterable[str]) -> None:
    target = Path(path)
    unique: list[str] = []
    seen: set[str] = set()
    for item in ranges:
        value = item.strip()
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    content = "\n".join(unique) + ("\n" if unique else "")
    temp_name: Optional[str] = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temp_name, target)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="从腾讯文档的“对策B”代码块提取 IP 规则"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"腾讯文档地址（默认 {DEFAULT_URL}）")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"输出文件（默认 {DEFAULT_OUTPUT}）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="请求超时秒数")
    parser.add_argument("--verbose", action="store_true", help="显示处理进度")
    args = parser.parse_args(argv)
    try:
        if args.verbose:
            print("正在读取腾讯文档页面和正文载荷...", file=sys.stderr)
        payload = fetch_document_payload(args.url, args.timeout)
        if args.verbose:
            print(f"已获取文档：{payload.title or '(无标题)'}", file=sys.stderr)
            print("正在定位“对策B”及其代码块...", file=sys.stderr)
        ranges = extract_code_ip_ranges(payload, START_HEADING, END_HEADING)
        write_ranges(Path(args.output), ranges)
        if args.verbose:
            print(f"已原子写入 {args.output}，共 {len(ranges)} 行", file=sys.stderr)
        return 0
    except ScrapeError as error:
        print(f"错误: {error}", file=sys.stderr)
        return error.exit_code
    except OSError as error:
        print(f"错误: 写入输出文件失败: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
