"""Merge the IP ranges in DamnYou.txt and BTN.txt into Ban.dat.

Accepted input forms (IPv4 and IPv6 are both supported):

* a single address: ``192.0.2.1``
* a CIDR network: ``192.0.2.0/24``
* an inclusive range: ``192.0.2.10-192.0.2.99``

The output is the exact union of both inputs. Overlapping, contained, duplicate,
and directly adjacent ranges are combined without adding any addresses that
were absent from the inputs.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FIRST_INPUT = SCRIPT_DIR / "DamnYou.txt"
DEFAULT_SECOND_INPUT = SCRIPT_DIR / "BTN.txt"
DEFAULT_OUTPUT = SCRIPT_DIR / "Ban.dat"


@dataclass(frozen=True, order=True)
class IPRange:
    version: int
    first: int
    last: int

    @property
    def size(self) -> int:
        return self.last - self.first + 1


class InputError(ValueError):
    """An input line is not a supported IP range."""


def _address(value: int, version: int) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if version == 4:
        return ipaddress.IPv4Address(value)
    return ipaddress.IPv6Address(value)


def _format_address(value: int, version: int) -> str:
    address = _address(value, version)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return f"::ffff:{address.ipv4_mapped}"
    return str(address)


def parse_range(text: str) -> IPRange:
    token = text.strip().lstrip("\ufeff")
    if not token:
        raise InputError("empty line")

    if "-" in token:
        parts = token.split("-")
        if len(parts) != 2:
            raise InputError("a range must contain exactly one '-' separator")
        try:
            first = ipaddress.ip_address(parts[0].strip())
            last = ipaddress.ip_address(parts[1].strip())
        except ValueError as exc:
            raise InputError(str(exc)) from exc
        if first.version != last.version:
            raise InputError("range endpoints use different IP versions")
        if int(first) > int(last):
            raise InputError("range start is greater than range end")
        return IPRange(first.version, int(first), int(last))

    try:
        if "/" in token:
            network = ipaddress.ip_network(token, strict=False)
            return IPRange(network.version, int(network.network_address), int(network.broadcast_address))
        single = ipaddress.ip_address(token)
        return IPRange(single.version, int(single), int(single))
    except ValueError as exc:
        raise InputError(str(exc)) from exc


def read_ranges(path: Path) -> tuple[list[IPRange], int]:
    ranges: list[IPRange] = []
    nonempty_lines = 0
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        nonempty_lines += 1
        try:
            ranges.append(parse_range(line))
        except InputError as exc:
            raise InputError(f"{path}, line {line_number}: {line!r}: {exc}") from exc
    return ranges, nonempty_lines


def merge_ranges(ranges: Iterable[IPRange]) -> list[IPRange]:
    ordered = sorted(ranges)
    if not ordered:
        return []

    merged: list[IPRange] = []
    current = ordered[0]
    for item in ordered[1:]:
        if item.version == current.version and item.first <= current.last + 1:
            current = IPRange(current.version, current.first, max(current.last, item.last))
        else:
            merged.append(current)
            current = item
    merged.append(current)
    return merged


def intersect_ranges(left: Sequence[IPRange], right: Sequence[IPRange]) -> list[IPRange]:
    intersection: list[IPRange] = []
    left_index = 0
    right_index = 0

    while left_index < len(left) and right_index < len(right):
        left_item = left[left_index]
        right_item = right[right_index]
        if left_item.version < right_item.version:
            left_index += 1
            continue
        if left_item.version > right_item.version:
            right_index += 1
            continue

        first = max(left_item.first, right_item.first)
        last = min(left_item.last, right_item.last)
        if first <= last:
            intersection.append(IPRange(left_item.version, first, last))

        if left_item.last < right_item.last:
            left_index += 1
        else:
            right_index += 1

    return intersection


def address_count(ranges: Iterable[IPRange]) -> int:
    return sum(item.size for item in ranges)


def format_range(item: IPRange) -> str:
    first = _format_address(item.first, item.version)
    if item.first == item.last:
        return first
    return f"{first}-{_format_address(item.last, item.version)}"


def write_ranges(path: Path, ranges: Sequence[IPRange]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{format_range(item)}\n" for item in ranges)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    except OSError:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _counts_by_version(ranges: Iterable[IPRange]) -> tuple[int, int]:
    ipv4 = 0
    ipv6 = 0
    for item in ranges:
        if item.version == 4:
            ipv4 += 1
        else:
            ipv6 += 1
    return ipv4, ipv6


def _print_summary(
    first_path: Path,
    first_lines: int,
    first_ranges: Sequence[IPRange],
    second_path: Path,
    second_lines: int,
    second_ranges: Sequence[IPRange],
    intersection: Sequence[IPRange],
    combined: Sequence[IPRange],
    output_path: Path,
) -> None:
    first_count = address_count(first_ranges)
    second_count = address_count(second_ranges)
    common_count = address_count(intersection)
    ipv4_ranges, ipv6_ranges = _counts_by_version(combined)

    print("Comparison complete:")
    print(
        f"  {first_path.name}: {first_lines:,} input lines, "
        f"{len(first_ranges):,} merged ranges, {first_count:,} unique addresses"
    )
    print(
        f"  {second_path.name}: {second_lines:,} input lines, "
        f"{len(second_ranges):,} merged ranges, {second_count:,} unique addresses"
    )
    print(f"  Present in both: {len(intersection):,} ranges, {common_count:,} addresses")
    print(f"  Only in {first_path.name}: {first_count - common_count:,} addresses")
    print(f"  Only in {second_path.name}: {second_count - common_count:,} addresses")
    print(
        f"Output: {output_path} ({len(combined):,} ranges: "
        f"{ipv4_ranges:,} IPv4 and {ipv6_ranges:,} IPv6)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge two changing IP range lists into one exact, deduplicated Ban.dat."
    )
    parser.add_argument(
        "first", nargs="?", type=Path, default=DEFAULT_FIRST_INPUT, help="first input file"
    )
    parser.add_argument(
        "second", nargs="?", type=Path, default=DEFAULT_SECOND_INPUT, help="second input file"
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="output file"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        first_raw, first_lines = read_ranges(args.first)
        second_raw, second_lines = read_ranges(args.second)
        first = merge_ranges(first_raw)
        second = merge_ranges(second_raw)
        intersection = intersect_ranges(first, second)
        combined = merge_ranges([*first, *second])
        write_ranges(args.output, combined)
    except (InputError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    _print_summary(
        args.first,
        first_lines,
        first,
        args.second,
        second_lines,
        second,
        intersection,
        combined,
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
