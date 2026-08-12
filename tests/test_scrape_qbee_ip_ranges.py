import base64
import contextlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import scrape_DamnYou as mod


def _varint(value):
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(output)


def _f_varint(field, value):
    return _varint(field << 3) + _varint(value)


def _f_bytes(field, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _mutation(begin, end, kind_field, kind_body=b""):
    return (
        _f_bytes(2, _f_varint(1, begin))
        + _f_bytes(3, _f_varint(1, end))
        + _f_varint(8, 1)
        + _f_bytes(7, _f_bytes(kind_field, kind_body))
    )


def _style_mutation(styles):
    entries = b""
    for style_id, name in styles:
        entries += _f_bytes(5, _f_bytes(1, style_id) + _f_bytes(2, name))
    return _f_bytes(7, _f_bytes(104, entries))


def _drawing_body(block_id):
    node = _f_bytes(1, block_id)
    for field in (7, 13, 5, 1, 9, 3):
        node = _f_bytes(field, node)
    return node


def _textbox_body(block_id, is_begin):
    body = _f_bytes(1, block_id) + _f_varint(7, 1)
    if is_begin:
        body += _f_varint(3, 1)
    return body


def _wire(text, mutations, styles):
    root = _f_bytes(2, _f_bytes(6, _f_bytes(1, text)))
    root += b"".join(_f_bytes(2, mutation) for mutation in mutations)
    root += _f_bytes(2, _style_mutation(styles))
    return _f_bytes(1, root)


def _payload(text, mutations, styles=None, title="测试文档"):
    if styles is None:
        styles = CODE_STYLES
    encoded = base64.b64encode(_wire(text, mutations, styles)).decode("ascii")
    return mod.DocumentPayload("https://example.test/doc/x", title, encoded)


def _run_style(begin, end):
    return _mutation(begin, end, 201, _f_bytes(1, "code-char"))


def _paragraph_style(end):
    return _mutation(end, end + 1, 202, _f_bytes(1, "code-para"))


def _drawing(pos, block_id):
    return _mutation(pos, pos + 1, 215, _drawing_body(block_id))


def _textbox(pos, block_id, is_begin):
    return _mutation(pos, pos + 1, 209, _textbox_body(block_id, is_begin))


CODE_STYLES = [
    ("code-char", "melo-codeblock-Base-theme-char"),
    ("code-para", "melo-codeblock-Base-theme-para"),
]
EXPECTED = [
    "59.47.237.128-59.47.237.255",
    "::ffff:59.47.237.128-::ffff:59.47.237.255",
    "240e:978:302:100::-240e:978:302:1ff:ffff:ffff:ffff:ffff",
    "2002::-2002:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
]
UPDATE_LOG_IP = "59.47.225.128-59.47.225.255"


class FixtureBuilder:
    def __init__(self):
        self.parts = []
        self.mutations = []

    @property
    def pos(self):
        return sum(map(len, self.parts))

    def add(self, text):
        begin = self.pos
        self.parts.append(text)
        return begin, self.pos

    def drawing(self, block_id):
        begin, _ = self.add("\x08")
        self.mutations.append(_drawing(begin, block_id))

    def block(self, block_id, lines, paragraph=False):
        begin_marker, _ = self.add("\x1c")
        self.mutations.append(_textbox(begin_marker, block_id, True))
        for line in lines:
            begin, end = self.add(line)
            self.mutations.append(_paragraph_style(end) if paragraph else _run_style(begin, end))
            self.add("\r")
        end_marker, _ = self.add("\x1d")
        self.mutations.append(_textbox(end_marker, block_id, False))

    def empty_block(self, block_id):
        begin, _ = self.add("\x1c")
        end, _ = self.add("\x1d")
        self.mutations.extend((_textbox(begin, block_id, True), _textbox(end, block_id, False)))

    def payload(self, styles=None):
        return _payload("".join(self.parts), self.mutations, CODE_STYLES if styles is None else styles)


def _section_fixture(valid=True, styles=None):
    fixture = FixtureBuilder()
    fixture.add("对策A：其他\r")
    fixture.drawing("before")
    fixture.add("\r对策B：屏蔽涉及的IP段\r更新时间 涉及IP段\r")
    fixture.add(UPDATE_LOG_IP + "\r")
    fixture.drawing("main")
    fixture.add("\r普通段落中的 8.8.8.8 不应提取\r")
    fixture.drawing("empty")
    fixture.drawing("last")
    fixture.add("\r对策C：对上传限速\r")
    fixture.drawing("after")
    fixture.add("\r尾部正文\r")
    fixture.block("before", ["9.9.9.9"])
    lines = [EXPECTED[0], "# comment", EXPECTED[1], EXPECTED[2], EXPECTED[0]]
    fixture.block("main", lines if valid else ["# comment", "not-an-ip"])
    fixture.empty_block("empty")
    fixture.block("last", [EXPECTED[3]] if valid else ["still-not-an-ip"], paragraph=True)
    fixture.block("after", ["8.8.4.4"])
    return fixture.payload(styles=styles)


class TestWireAndValidation(unittest.TestCase):
    def test_wire_reader_and_nested_paths(self):
        nodes = mod.decode_wire(_f_varint(1, 300) + _f_bytes(2, _f_varint(1, 7)))
        self.assertEqual(nodes[0].varint, 300)
        self.assertEqual(nodes[1].children[0].path, (2, 1))
        self.assertEqual(nodes[1].children[0].varint, 7)

    def test_bad_wire(self):
        for value in (b"", b"\x00", _varint((1 << 3) | 3), b"\x0a\x05x"):
            with self.subTest(value=value), self.assertRaises(mod.ParseError):
                mod.decode_wire(value)

    def test_valid_ip_rules(self):
        values = ["1.2.3.4", "2001:db8::1", "10.0.0.0/8", "2001:db8::/32",
                  "::ffff:1.2.3.0/120", "1.2.3.4-1.2.3.10",
                  "::ffff:1.2.3.4-::ffff:1.2.3.10"]
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(mod._is_valid_ip_line(value))

    def test_invalid_ip_rules(self):
        values = ["", "# 1.2.3.4", "999.1.1.1", "1.2.3.4/33",
                  "1.2.3.10-1.2.3.4", "1.2.3.4-2001:db8::1",
                  "1.2.3.4 - 1.2.3.5", "1.2.3.4 # note", "更新时间"]
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(mod._is_valid_ip_line(value))


class TestExtraction(unittest.TestCase):
    def test_drawing_references_define_section_code_blocks(self):
        ranges = mod.extract_code_ip_ranges(_section_fixture(), mod.START_HEADING, mod.END_HEADING)
        self.assertEqual(ranges, EXPECTED)
        self.assertNotIn(UPDATE_LOG_IP, ranges)
        self.assertNotIn("9.9.9.9", ranges)
        self.assertNotIn("8.8.4.4", ranges)

    def test_update_table_is_not_a_fallback(self):
        fixture = FixtureBuilder()
        fixture.add("对策B：屏蔽涉及的IP段\r更新时间\r" + UPDATE_LOG_IP +
                    "\r对策C：对上传限速\r")
        fixture.mutations.append(_mutation(0, 1, 201))
        with self.assertRaises(mod.CodeBlockError):
            mod.extract_code_ip_ranges(fixture.payload(), mod.START_HEADING, mod.END_HEADING)

    def test_direct_styled_line_in_section(self):
        fixture = FixtureBuilder()
        fixture.add("对策B：屏蔽涉及的IP段\r")
        begin, end = fixture.add("1.1.1.1")
        fixture.mutations.append(_run_style(begin, end))
        fixture.add("\r对策C：对上传限速\r")
        self.assertEqual(
            mod.extract_code_ip_ranges(fixture.payload(), mod.START_HEADING, mod.END_HEADING),
            ["1.1.1.1"],
        )

    def test_code_without_valid_ip(self):
        with self.assertRaises(mod.NoValidRangesError):
            mod.extract_code_ip_ranges(_section_fixture(valid=False), mod.START_HEADING, mod.END_HEADING)

    def test_missing_code_styles(self):
        with self.assertRaises(mod.CodeBlockError):
            mod.extract_code_ip_ranges(_section_fixture(styles=[]), mod.START_HEADING, mod.END_HEADING)

    def test_incomplete_block_markers(self):
        fixture = FixtureBuilder()
        fixture.add("对策B：屏蔽涉及的IP段\r")
        fixture.drawing("broken")
        fixture.add("\r对策C：对上传限速\r")
        marker, _ = fixture.add("\x1c")
        fixture.mutations.append(_textbox(marker, "broken", True))
        with self.assertRaises(mod.CodeBlockError):
            mod.extract_code_ip_ranges(fixture.payload(), mod.START_HEADING, mod.END_HEADING)

    def test_heading_errors(self):
        texts = [
            "对策C：对上传限速\r对策B：屏蔽涉及的IP段\r",
            "对策B：屏蔽涉及的IP段\r",
            "对策C：对上传限速\r",
            "对策B：屏蔽涉及的IP段\r对策B：屏蔽涉及的IP段\r对策C：对上传限速\r",
        ]
        for text in texts:
            with self.subTest(text=text), self.assertRaises(mod.SectionError):
                mod.extract_code_ip_ranges(
                    _payload(text, [_mutation(0, 1, 201)], CODE_STYLES),
                    mod.START_HEADING,
                    mod.END_HEADING,
                )

    def test_bad_base64_and_missing_text_node(self):
        with self.assertRaises(mod.ParseError):
            mod.extract_code_ip_ranges(mod.DocumentPayload("x", "t", "%%%"), mod.START_HEADING, mod.END_HEADING)
        encoded = base64.b64encode(_f_bytes(1, _f_bytes(2, b"x"))).decode("ascii")
        with self.assertRaises(mod.ParseError):
            mod.extract_code_ip_ranges(mod.DocumentPayload("x", "t", encoded), mod.START_HEADING, mod.END_HEADING)


class TestJSONPAndURL(unittest.TestCase):
    def test_jsonp(self):
        self.assertEqual(mod._extract_jsonp(' clientVarsCallback({"a":1}); '), {"a": 1})

    def test_bad_jsonp(self):
        for value in ("{}", "otherCallback({});", "clientVarsCallback([1]);"):
            with self.subTest(value=value), self.assertRaises(mod.ParseError):
                mod._extract_jsonp(value)

    def test_opendoc_link_and_no_guess_fallback(self):
        page = '<link href="/dop-api/opendoc?id=x&amp;callback=clientVarsCallback">'
        self.assertEqual(mod._find_opendoc_url(page, "https://docs.qq.com/doc/x"),
                         "https://docs.qq.com/dop-api/opendoc?id=x&callback=clientVarsCallback")
        with self.assertRaises(mod.ParseError):
            mod._find_opendoc_url("<html>none</html>", "https://docs.qq.com/doc/x")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = self.server.routes.get(urllib.parse.urlparse(self.path).path)
        if route is None:
            self.send_error(404)
            return
        status, body, headers, delay, cookie = route
        if cookie and cookie not in (self.headers.get("Cookie") or ""):
            status, body = 403, b"missing cookie"
        if delay:
            time.sleep(delay)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *_args):
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True


@contextlib.contextmanager
def _serve(api_body, page_status=200, api_status=200, delay=0, with_link=True):
    server = _Server(("127.0.0.1", 0), _Handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    link = f"{base}/dop-api/opendoc?id=x&amp;callback=clientVarsCallback"
    page = f'<link href="{link}">' if with_link else "<html>no link</html>"
    server.routes = {
        "/doc/x": (page_status, page.encode(), {"Set-Cookie": "session=yes; Path=/"}, 0, None),
        "/dop-api/opendoc": (api_status, api_body, {}, delay, "session=yes"),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"{base}/doc/x"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _jsonp_for(payload):
    value = {"clientVars": {"title": payload.title, "collab_client_vars": {
        "initialAttributedText": {"text": [payload.attributed_text_b64]}}}}
    return ("clientVarsCallback(" + json.dumps(value, ensure_ascii=False) + " );").encode("utf-8")


class TestFetchWriteAndMain(unittest.TestCase):
    def test_fetch_reuses_page_cookie(self):
        fixture = _section_fixture()
        with _serve(_jsonp_for(fixture)) as url:
            payload = mod.fetch_document_payload(url, 3)
        self.assertEqual(payload.title, fixture.title)
        self.assertEqual(payload.attributed_text_b64, fixture.attributed_text_b64)

    def test_fetch_errors(self):
        fixture = _section_fixture()
        cases = [
            (b"not jsonp", 200, 200, True, mod.ParseError),
            (_jsonp_for(fixture), 404, 200, True, mod.FetchError),
            (_jsonp_for(fixture), 200, 403, True, mod.FetchError),
            (_jsonp_for(fixture), 200, 200, False, mod.ParseError),
        ]
        for body, page_status, api_status, with_link, error in cases:
            with self.subTest(error=error.__name__), _serve(
                body, page_status, api_status, with_link=with_link
            ) as url, self.assertRaises(error):
                mod.fetch_document_payload(url, 2)

    def test_fetch_requires_strict_base64_path(self):
        values = [
            {"clientVars": {}},
            {"clientVars": {"collab_client_vars": {"initialAttributedText": {"text": []}}}},
            {"clientVars": {"collab_client_vars": {"initialAttributedText": {"text": ["%%%"]}}}},
        ]
        for value in values:
            body = ("clientVarsCallback(" + json.dumps(value) + " );").encode()
            with self.subTest(value=value), _serve(body) as url, self.assertRaises(mod.ParseError):
                mod.fetch_document_payload(url, 2)

    def test_timeout(self):
        with _serve(_jsonp_for(_section_fixture()), delay=0.3) as url, self.assertRaises(mod.FetchError):
            mod.fetch_document_payload(url, 0.03)

    def test_atomic_utf8_lf_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "DamnYou.txt"
            mod.write_ranges(target, ["1.1.1.1", "1.1.1.1", " 2001:db8::1 "])
            self.assertEqual(target.read_bytes(), b"1.1.1.1\n2001:db8::1\n")
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_main_success_and_failure_preserves_output(self):
        fixture = _section_fixture()
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "DamnYou.txt"
            with _serve(_jsonp_for(fixture)) as url:
                self.assertEqual(mod.main(["--url", url, "--output", str(target), "--timeout", "3"]), 0)
            self.assertEqual(target.read_text(encoding="utf-8").splitlines(), EXPECTED)
            target.write_text("old content\n", encoding="utf-8")
            with _serve(b"bad response") as url:
                self.assertNotEqual(mod.main(["--url", url, "--output", str(target), "--timeout", "3"]), 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "old content\n")

    def test_default_output_current_directory(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir, _serve(_jsonp_for(_section_fixture())) as url:
            try:
                os.chdir(temp_dir)
                self.assertEqual(mod.main(["--url", url, "--timeout", "3"]), 0)
                self.assertEqual(Path("DamnYou.txt").read_text(encoding="utf-8").splitlines(), EXPECTED)
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
