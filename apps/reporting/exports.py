"""XLSX OOXML sans dépendance : texte inline explicite, aucune formule/hyperlien."""

import hashlib
import io
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from django.core import signing
from django.utils.translation import gettext

from apps.core import idempotency
from apps.core.commands import Command, Effect, register_command
from apps.core.payloads import invalid_input, payload_hash

from .filters import PARIS, limit, refused
from .selectors import SCHEMAS, dataset
from .snapshots import authorize

BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


class BoundedBuffer(io.BytesIO):
    """Refuser le ZIP dès qu'il dépasse la limite, avant sa mise à disposition."""

    def write(self, data):
        if self.tell() + len(data) > min(limit("MAX_BYTES", 20000000), 20000000):
            raise refused(gettext("The file is too large. Reduce the filters."))
        return super().write(data)


def text(value):
    value = BAD_XML.sub("", str(value))
    if value.lstrip("\ufeff\u200b\t\r\n ").startswith(("=", "+", "-", "@")):
        value = "'" + value
    if len(value) > 32767:
        raise refused(gettext("A cell exceeds Excel's text limit. Reduce the filters."))
    return escape(value)


def cell(value, *, header=False):
    if header:
        return '<c s="3" t="inlineStr"><is><t xml:space="preserve">' + text(value) + "</t></is></c>"
    if value is None:
        return "<c/>"
    if isinstance(value, datetime):
        # Date Excel sans timezone, explicitement convertie en Europe/Paris.
        value = value.astimezone(PARIS).replace(tzinfo=None)
        serial = (value - datetime(1899, 12, 30)).total_seconds() / 86400
        return f'<c s="2"><v>{serial}</v></c>'
    if isinstance(value, date):
        return f'<c s="1"><v>{(value - date(1899, 12, 30)).days}</v></c>'
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return f"<c><v>{value}</v></c>"
    return '<c t="inlineStr"><is><t xml:space="preserve">' + text(value) + "</t></is></c>"


def workbook(name, rows, filters, metadata):
    title, columns = dataset(name)
    records = [
        [title],
        [gettext("Extracted at"), metadata["extracted"]],
        [gettext("Time zone"), "Europe/Paris"],
        [
            gettext("Period"),
            gettext("Start inclusive: %(start)s; end exclusive: %(end)s")
            % {"start": filters.start, "end": filters.end},
        ],
        [gettext("Filters"), str(metadata.get("filter_labels", filters.public()))],
        [
            gettext("Reading guide"),
            gettext(
                "Stock is a current snapshot. Other data uses its business date; corrected quantities use the original event date."
            ),
        ],
        columns,
    ]
    records.extend(row["cells"] for row in rows)
    from . import statistics

    sheets = [(gettext("Data"), records, len(columns))]
    aggregates = []
    aggregate_columns = []
    summary = statistics.summarize(name, rows)
    if name in {"deliveries", "receipts"}:
        aggregate_columns = [
            gettext("Product"),
            gettext("Unit"),
            gettext("Effective quantity"),
            gettext("Initial declaration"),
        ]
        aggregates = [[r["product"], r["unit"], r["quantity"], r["initial"]] for r in summary]
    elif name == "purchases":
        aggregate_columns = [
            gettext("Currency"),
            gettext("Known amount"),
            gettext("Purchases with amount"),
            gettext("Purchases without amount"),
        ]
        aggregates = [
            [r["currency"], r["amount"] if r["known"] else None, r["known"], r["missing"]]
            for r in summary
        ]
    elif name == "requests":
        aggregate_columns = [
            gettext("Product"),
            gettext("Unit"),
            gettext("Original department"),
            gettext("Department reference"),
            gettext("Role at creation"),
            gettext("Requested"),
            gettext("Effectively handed over"),
            gettext("Closed"),
            gettext("Still reserved"),
        ]
        aggregates = [
            [
                r[k]
                for k in [
                    "product",
                    "unit",
                    "floor",
                    "floor_code",
                    "role",
                    "requested",
                    "delivered",
                    "closed",
                    "reserved",
                ]
            ]
            for r in statistics.request_totals(rows)
        ]
    elif name == "stocks":
        aggregate_columns = [gettext("Indicator — distinct references"), gettext("Reference count")]
        aggregates = [list(r) for r in statistics.indicators(name, rows)]
    if aggregate_columns:
        sheets.append(
            (
                gettext("Summary"),
                records[:6] + [aggregate_columns] + aggregates,
                len(aggregate_columns),
            )
        )
    # A14 compte toutes les lignes de données, y compris la synthèse.
    if len(rows) + len(aggregates) > min(limit("EXPORT_MAX_ROWS", 20000), 20000):
        raise refused(gettext("The workbook exceeds 20,000 data rows. Reduce the filters."))
    out = BoundedBuffer()
    with ZipFile(out, "w", ZIP_DEFLATED) as z:
        content = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        content += (
            "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(sheets) + 1)
            )
            + "</Types>"
        )
        z.writestr("[Content_Types].xml", content)
        z.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(
                f'<sheet name="{title}" sheetId="{i}" r:id="rId{i}"/>'
                for i, (title, _, _) in enumerate(sheets, 1)
            )
            + "</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(sheets) + 1)
            )
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        )
        z.writestr(
            "xl/styles.xml",
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="2"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/><numFmt numFmtId="165" formatCode="dd/mm/yyyy hh:mm"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF145B78"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
        )
        for i, (_, sheet_records, width) in enumerate(sheets, 1):
            sheet = (
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="7" topLeftCell="A8" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>'
                + "".join(
                    f'<col min="{n}" max="{n}" width="25" customWidth="1"/>'
                    for n in range(1, width + 1)
                )
                + "</cols><sheetData>"
            )
            sheet += (
                "".join(
                    ('<row ht="42" customHeight="1">' if row_index == 7 else "<row>")
                    + "".join(cell(v, header=row_index == 7) for v in record)
                    + "</row>"
                    for row_index, record in enumerate(sheet_records, 1)
                )
                + "</sheetData></worksheet>"
            )
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet)
    result = out.getvalue()
    if len(result) > min(limit("MAX_BYTES", 20000000), 20000000):
        raise refused(gettext("The file is too large. Reduce the filters."))
    return result


def apply_export(execution, resources, payload):
    try:
        verified = signing.loads(payload.get("proof", ""), salt="reporting-generated", max_age=60)
    except signing.BadSignature as exc:
        raise invalid_input() from exc
    clean = {k: v for k, v in payload.items() if k != "proof"}
    if verified != clean:
        raise invalid_input()
    if (
        set(clean) != {"dataset", "rows", "sha256", "filters", "extracted_at"}
        or payload["dataset"] not in SCHEMAS
        or type(payload["rows"]) is not int
        or payload["rows"] < 0
        or not re.fullmatch("[a-f0-9]{64}", payload["sha256"])
    ):
        raise invalid_input()
    return Effect(resource={"generated": True}, after={"event": "EXPORT_GENERATED", **clean})


def ensure_registered():
    from apps.core.commands import _registry

    if "reporting.export" not in _registry:
        register_command(
            Command(
                "reporting.export",
                frozenset({"ADMIN"}),
                lambda actor, id: None,
                lambda actor, id: None,
                apply_export,
            )
        )


def audit_generated(context, name, data, f, meta, count):
    ensure_registered()
    authorize(context)
    payload = {
        "dataset": name,
        "rows": count,
        "sha256": hashlib.sha256(data).hexdigest(),
        "filters": f.public(),
        "extracted_at": meta["extracted"].isoformat(),
    }
    payload["proof"] = signing.dumps(payload, salt="reporting-generated")
    identifier = uuid.uuid4()
    key = uuid.uuid4().hex
    idempotency.register_intent(
        context,
        intent_id=identifier,
        key=key,
        fingerprint=payload_hash(payload),
        command_type="reporting.export",
    )
    result = idempotency.execute_intent(context, intent_id=identifier, key=key, payload=payload)
    if result.status >= 400:
        raise refused(gettext("The file could not be confirmed. Reload and try again."))
