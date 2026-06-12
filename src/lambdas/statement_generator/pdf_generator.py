"""PDF rendering for bank statements (Requirement 9.5 / 9.6).

Builds the *extracto bancario* PDF with :mod:`fpdf` (fpdf2). The document is the
referential statement the Strands_Agent asks for and that the Message_Processor
later delivers to the client over WhatsApp (Twilio Media).

The generated PDF contains exactly the Requirement 9.5 fields:
    * BTG ConnectAI branded header.
    * The client's name.
    * The **masked** account number (never the full number — Requirement 14.4).
    * The statement period (up to the cut-off date).
    * A table of movements: date, description and amount (formatted as COP).
    * The final (closing) balance, formatted as COP.

When the account has no movements in the period (Requirement 9.6) the movements
table is replaced by a single explanatory line.
"""

from __future__ import annotations

from fpdf import FPDF

from shared.formatting import format_cop
from shared.masking import mask_account
from shared.types import MockProduct, MockTransaction

# BTG brand palette (RGB). Kept local to the renderer.
_BTG_NAVY = (0, 32, 91)
_BTG_GOLD = (197, 159, 92)
_ROW_ALT = (242, 244, 248)
_WHITE = (255, 255, 255)
_TEXT_DARK = (33, 37, 41)

# Message rendered when there are no movements for the requested period (Req 9.6).
NO_MOVEMENTS_MESSAGE = "No se encontraron movimientos para el período."


def _format_movement_date(iso_date: str) -> str:
    """Render a movement's ISO datetime as a short ``YYYY-MM-DD`` date.

    Falls back to the raw string when it cannot be parsed so the statement still
    renders rather than failing.
    """
    if not iso_date:
        return ""
    # Movement dates are ISO 8601 (often with a time component); keep the date.
    return str(iso_date)[:10]


class _StatementPDF(FPDF):
    """FPDF subclass that paints the BTG ConnectAI branded header on each page."""

    def header(self) -> None:  # noqa: D401 - fpdf hook
        self.set_fill_color(*_BTG_NAVY)
        self.rect(0, 0, self.w, 22, style="F")
        self.set_xy(10, 6)
        self.set_text_color(*_WHITE)
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 10, "BTG ConnectAI", new_x="LMARGIN", new_y="NEXT")
        self.set_x(10)
        self.set_text_color(*_BTG_GOLD)
        self.set_font("Helvetica", "", 10)
        self.cell(0, 5, "Extracto Bancario", new_x="LMARGIN", new_y="NEXT")
        self.ln(8)
        self.set_text_color(*_TEXT_DARK)


def generate_statement_pdf(
    client: dict,
    account: MockProduct,
    transactions: list[MockTransaction],
    cutoff_date: str,
    period_start: str | None = None,
) -> bytes:
    """Render a bank statement PDF and return it as ``bytes``.

    Args:
        client: The Mock_Core client (provides ``name``).
        account: The product the statement is for (provides the account number,
            product name and closing balance).
        transactions: The movements to render (already filtered to the account
            and period). May be empty (Requirement 9.6).
        cutoff_date: The statement cut-off date (``YYYY-MM-DD``); the statement
            period ends here.
        period_start: Optional period start (``YYYY-MM-DD``). When omitted the
            period is shown as "hasta <cutoff_date>".

    Returns:
        The rendered PDF document as ``bytes``.
    """
    pdf = _StatementPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(10, 10, 10)
    pdf.add_page()

    masked_account = mask_account(account["account_number"])
    if period_start:
        period_text = f"Del {period_start} al {cutoff_date}"
    else:
        period_text = f"Hasta {cutoff_date}"

    # --- Client / account summary block -----------------------------------
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*_BTG_NAVY)
    pdf.cell(0, 7, "Información del titular", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_TEXT_DARK)
    pdf.set_font("Helvetica", "", 10)

    def _summary_row(label: str, value: str) -> None:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(45, 6, label)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, value, new_x="LMARGIN", new_y="NEXT")

    _summary_row("Cliente:", str(client.get("name", "")))
    _summary_row("Producto:", str(account.get("product_name", "")))
    _summary_row("Cuenta:", masked_account)
    _summary_row("Período:", period_text)
    pdf.ln(4)

    # --- Movements table ---------------------------------------------------
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*_BTG_NAVY)
    pdf.cell(0, 7, "Movimientos", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_TEXT_DARK)

    col_date = 30
    col_amount = 40
    col_desc = pdf.w - pdf.l_margin - pdf.r_margin - col_date - col_amount

    if not transactions:
        # Requirement 9.6: empty statement — explain there are no movements.
        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 8, NO_MOVEMENTS_MESSAGE, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*_TEXT_DARK)
    else:
        # Header row.
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(*_BTG_NAVY)
        pdf.set_text_color(*_WHITE)
        pdf.cell(col_date, 7, "Fecha", border=0, fill=True)
        pdf.cell(col_desc, 7, "Descripción", border=0, fill=True)
        pdf.cell(col_amount, 7, "Monto", border=0, fill=True, align="R",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*_TEXT_DARK)

        pdf.set_font("Helvetica", "", 9)
        for index, tx in enumerate(transactions):
            fill = index % 2 == 1
            if fill:
                pdf.set_fill_color(*_ROW_ALT)
            amount = tx.get("amount", 0)
            # Debits reduce the balance; show them as negative for clarity.
            signed_amount = -amount if tx.get("type") == "debit" else amount
            pdf.cell(col_date, 6, _format_movement_date(tx.get("date", "")),
                     border=0, fill=fill)
            pdf.cell(col_desc, 6, str(tx.get("description", "")),
                     border=0, fill=fill)
            pdf.cell(col_amount, 6, format_cop(signed_amount), border=0,
                     fill=fill, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)

    # --- Final balance -----------------------------------------------------
    closing_balance = account.get("available_balance",
                                  account.get("total_balance", 0))
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(*_BTG_GOLD)
    pdf.set_text_color(*_BTG_NAVY)
    pdf.cell(col_date + col_desc, 9, "Saldo final", border=0, fill=True)
    pdf.cell(col_amount, 9, format_cop(closing_balance), border=0, fill=True,
             align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_TEXT_DARK)
    pdf.ln(6)

    # --- Disclaimer --------------------------------------------------------
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(
        0,
        4,
        "Información referencial generada por BTG ConnectAI. Los registros "
        "oficiales están disponibles en los portales del banco.",
    )

    # fpdf2 returns a bytearray from output(); normalize to immutable bytes.
    return bytes(pdf.output())


__all__ = ["generate_statement_pdf", "NO_MOVEMENTS_MESSAGE"]
