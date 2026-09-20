"""CSV/PDF rendering of a route result. The CSV is checked row-for-row since
it is meant to be read directly; the PDF is checked only for being
well-formed non-empty bytes, since asserting on rendered layout is not a
useful test."""

from __future__ import annotations

import csv
import io

from prahari_bff.export import route_to_csv, route_to_pdf

ROUTE = {
    "plate": "GJ01AB1234",
    "hops": [
        {
            "camera_id": "cam-1",
            "location": "Ashram Road Junction",
            "wall_clock_s": 1000.0,
            "pts_ms": 12345,
            "link_kind": "seen",
            "confidence": 0.92,
            "evidence_ref": "evidence/cam-1/1000",
        },
        {
            "camera_id": "cam-2",
            "location": "Paldi Circle",
            "wall_clock_s": 1080.5,
            "pts_ms": 67890,
            "link_kind": "inferred",
            "confidence": 0.61,
            "evidence_ref": None,
        },
    ],
    "rejected": [],
    "dark_zones": [],
}


def test_csv_has_one_row_per_hop_plus_header():
    rows = list(csv.reader(io.StringIO(route_to_csv(ROUTE).decode("utf-8"))))
    assert rows[0] == [
        "plate",
        "camera_id",
        "location",
        "wall_clock_s",
        "pts_ms",
        "link_kind",
        "confidence",
        "evidence_ref",
    ]
    assert len(rows) == 3  # header + 2 hops
    assert rows[1][0] == "GJ01AB1234"
    assert rows[1][1] == "cam-1"
    assert rows[2][1] == "cam-2"


def test_csv_plate_repeated_on_every_row():
    rows = list(csv.reader(io.StringIO(route_to_csv(ROUTE).decode("utf-8"))))
    plates = {row[0] for row in rows[1:]}
    assert plates == {"GJ01AB1234"}


def test_csv_missing_evidence_ref_becomes_empty_string_not_none():
    rows = list(csv.reader(io.StringIO(route_to_csv(ROUTE).decode("utf-8"))))
    assert rows[2][-1] == ""


def test_csv_of_a_route_with_no_hops_is_header_only():
    empty = {"plate": "GJ01ZZ0000", "hops": []}
    rows = list(csv.reader(io.StringIO(route_to_csv(empty).decode("utf-8"))))
    assert len(rows) == 1


def test_pdf_renders_nonempty_bytes_starting_with_the_pdf_magic_number():
    body = route_to_pdf(ROUTE)
    assert len(body) > 0
    assert body.startswith(b"%PDF-")


def test_pdf_of_a_route_with_no_hops_still_renders():
    empty = {"plate": "GJ01ZZ0000", "hops": []}
    body = route_to_pdf(empty)
    assert body.startswith(b"%PDF-")


def test_csv_cells_opening_with_formula_chars_are_escaped():
    """A cell starting with =, +, - or @ is a formula in Excel/Sheets —
    an untrusted plate or evidence ref must not execute on open."""
    hostile = {
        "plate": '=HYPERLINK("http://evil")',
        "hops": [
            {
                "camera_id": "@cmd",
                "location": "+1+1",
                "wall_clock_s": 1.0,
                "pts_ms": 1,
                "link_kind": "seen",
                "confidence": 0.5,
                "evidence_ref": "-2+3",
            }
        ],
    }
    rows = list(csv.reader(io.StringIO(route_to_csv(hostile).decode("utf-8"))))
    data_row = rows[1]
    for cell in data_row:
        assert cell.startswith("'") or cell[:1] not in ("=", "+", "-", "@")
    assert data_row[0] == '\'=HYPERLINK("http://evil")'
    assert data_row[1] == "'@cmd"
    assert data_row[7] == "'-2+3"


def test_pdf_escapes_markup_characters_in_the_plate():
    """ReportLab Paragraph parses mini-HTML — a plate containing '<' or '&'
    must be escaped, not parsed as markup (which would also crash the
    paragraph parser on an unclosed tag)."""
    hostile = {"plate": "GJ01<b>&amp;", "hops": []}
    body = route_to_pdf(hostile)
    assert body.startswith(b"%PDF-")
