"""CSV and PDF rendering of a route result — the literal submission
requirement ("a registration number in, a timestamped, location-wise route
out"), built once and shared by both branches of `GET /api/v1/routes/
{plate}/export`.

Table-only PDF, no charting library: per docs/DAY3-DESIGN.md §4.4, "the
requirement here is defensibility, not design."
"""

from __future__ import annotations

import csv
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

__all__ = ["route_to_csv", "route_to_pdf"]

_CSV_HEADERS = [
    "plate",
    "camera_id",
    "location",
    "wall_clock_s",
    "pts_ms",
    "link_kind",
    "confidence",
    "evidence_ref",
]


def _csv_cell(value: object) -> object:
    """Formula-injection guard: a cell opening with =, +, - or @ executes in
    Excel/Sheets when the file is opened. Prefixing with a single quote makes
    the cell a literal — the standard mitigation for CSVs that carry
    untrusted text (plate strings, evidence refs)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def route_to_csv(route: dict) -> bytes:
    """One row per hop — plate repeated on every row so the file is
    self-describing even split from its response headers."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_HEADERS)
    plate = route.get("plate", "")
    for hop in route.get("hops", []):
        writer.writerow(
            [
                _csv_cell(plate),
                _csv_cell(hop.get("camera_id", "")),
                _csv_cell(hop.get("location", "")),
                _csv_cell(hop.get("wall_clock_s", "")),
                _csv_cell(hop.get("pts_ms", "")),
                _csv_cell(hop.get("link_kind", "") or ""),
                _csv_cell(hop.get("confidence", "")),
                _csv_cell(hop.get("evidence_ref", "") or ""),
            ]
        )
    return buf.getvalue().encode("utf-8")


def route_to_pdf(route: dict) -> bytes:
    """Hops table plus a provenance block per hop (camera, timestamp,
    evidence reference) — the same rows as the CSV, laid out for a reader who
    did not request the raw file."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    styles = getSampleStyleSheet()
    plate = route.get("plate", "")
    hops = route.get("hops", [])

    # Paragraph parses a mini-HTML dialect — an unescaped plate containing
    # '<' or '&' would be read as markup (or crash the paragraph parser).
    elements = [
        Paragraph(f"Route reconstruction: {escape(str(plate))}", styles["Title"]),
        Spacer(1, 12),
    ]

    header_style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ]
    )

    hop_rows = [["Camera", "Location", "Wall clock (s)", "PTS (ms)", "Link", "Confidence"]]
    for hop in hops:
        confidence = hop.get("confidence")
        hop_rows.append(
            [
                str(hop.get("camera_id", "")),
                str(hop.get("location", "") or ""),
                str(hop.get("wall_clock_s", "")),
                str(hop.get("pts_ms", "")),
                str(hop.get("link_kind", "") or ""),
                f"{confidence:.2f}" if isinstance(confidence, int | float) else "",
            ]
        )
    hop_table = Table(hop_rows, repeatRows=1)
    hop_table.setStyle(header_style)
    elements.append(hop_table)

    if hops:
        elements.append(Spacer(1, 16))
        elements.append(Paragraph("Provenance", styles["Heading2"]))
        prov_rows = [["Camera", "Timestamp (wall clock s)", "Evidence ref"]]
        for hop in hops:
            prov_rows.append(
                [
                    str(hop.get("camera_id", "")),
                    str(hop.get("wall_clock_s", "")),
                    str(hop.get("evidence_ref", "") or "-"),
                ]
            )
        prov_table = Table(prov_rows, repeatRows=1)
        prov_table.setStyle(header_style)
        elements.append(prov_table)
    else:
        elements.append(Paragraph("No hops recorded for this plate.", styles["Normal"]))

    doc.build(elements)
    return buf.getvalue()
