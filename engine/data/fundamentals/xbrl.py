"""Parsers for exchange XBRL filings (SEBI Integrated Filing – Financials, Shareholding Pattern).

Listed companies file quarterly results and shareholding patterns with NSE and BSE in XBRL; NSE
publishes each instance file at ``nsearchives.nseindia.com/corporate/xbrl/...``. These parsers
read the instance document directly (no taxonomy download): facts are matched by local element
name and by context period/dimensions.

Results schemas (SEBI Integrated Filing taxonomies, 2025+): ``IndAs`` (companies), ``Banking``,
``NBFC``, ``LI`` (life insurance), ``GI`` (general insurance). The schema is read from the
utility comment or entity namespace in the file, falling back to the URL and then to tags.

Monetary values in these files are in rupees (the ``decimals`` attribute only states rounding;
``LevelOfRounding`` is the presentation unit of the PDF). Shareholding percentages are fractions
(0.5044 = 50.44%); they are stored as percent.

Every parsed row carries ``known_at`` (the exchange dissemination time from NSE's filing list),
never the period end: results for the quarter to 30 Sep are not known until they are filed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"


class XbrlError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Context:
    id: str
    start: date | None
    end: date | None
    instant: date | None
    members: tuple[tuple[str, str], ...]  # (axis, member) local names, explicit members
    typed: bool  # has typed dimension members (per-shareholder rows and the like)

    @property
    def plain(self) -> bool:
        return not self.members and not self.typed

    @property
    def days(self) -> int | None:
        if self.start is None or self.end is None:
            return None
        return (self.end - self.start).days


@dataclass(frozen=True, slots=True)
class Fact:
    name: str
    context: str
    value: str
    unit: str | None


@dataclass
class XbrlDoc:
    contexts: dict[str, Context]
    facts: list[Fact]
    header: str  # first bytes of the file (utility comment, namespaces) for schema detection

    def text(self, name: str) -> str | None:
        """First value of a non-dimensional fact (header items such as ``Symbol``)."""
        for f in self.facts:
            if f.name == name and self.contexts[f.context].plain:
                return f.value.strip()
        return None

    def number(self, name: str, context: str) -> float | None:
        for f in self.facts:
            if f.name == name and f.context == context:
                return _to_float(f.value)
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _to_float(raw: str) -> float | None:
    s = raw.strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(raw: str | None) -> date | None:
    if not raw:
        return None
    s = raw.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_xbrl(data: bytes) -> XbrlDoc:
    head = data[:4096].decode("utf-8", errors="replace")
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        raise XbrlError("refusing XBRL with a DTD/entity declaration")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise XbrlError(f"not XML: {exc}") from exc
    if _local(root.tag) != "xbrl":
        raise XbrlError(f"not an XBRL instance (root <{_local(root.tag)}>)")
    contexts: dict[str, Context] = {}
    for c in root.iter(XBRLI + "context"):
        cid = c.get("id") or ""
        period = c.find(XBRLI + "period")
        start = end = instant = None
        if period is not None:
            start = _date(period.findtext(XBRLI + "startDate"))
            end = _date(period.findtext(XBRLI + "endDate"))
            instant = _date(period.findtext(XBRLI + "instant"))
        members = tuple(
            (_local(m.get("dimension") or ""), _local((m.text or "").strip()))
            for m in c.iter(XBRLDI + "explicitMember")
        )
        typed = c.find(f".//{XBRLDI}typedMember") is not None
        contexts[cid] = Context(cid, start, end, instant, members, typed)
    facts: list[Fact] = []
    for el in root:
        ref = el.get("contextRef")
        if ref is None or ref not in contexts:
            continue
        facts.append(Fact(_local(el.tag), ref, el.text or "", el.get("unitRef")))
    return XbrlDoc(contexts, facts, head)


# --------------------------------------------------------------------------- results


class ResultsSchema(StrEnum):
    INDAS = "IndAs"
    BANKING = "Banking"
    NBFC = "NBFC"
    LIFE_INSURANCE = "LI"
    GENERAL_INSURANCE = "GI"
    UNKNOWN = "unknown"


_SCHEMA_TOKENS = {
    "INDAS": ResultsSchema.INDAS,
    "BANKING": ResultsSchema.BANKING,
    "NBFC": ResultsSchema.NBFC,
    "LI": ResultsSchema.LIFE_INSURANCE,
    "GI": ResultsSchema.GENERAL_INSURANCE,
}
_COMMENT = re.compile(r"<!--\s*IF([A-Za-z]+)\s+V")
_NAMESPACE = re.compile(r"IntegratedFinance_([A-Za-z]+)/")
_URL = re.compile(r"INTEGRATED_FILING_([A-Z]+)")

# Tag preferences per item, first present wins. Items a schema does not report stay None.
_REVENUE: dict[ResultsSchema, tuple[str, ...]] = {
    ResultsSchema.BANKING: ("InterestEarned",),
    ResultsSchema.LIFE_INSURANCE: ("NetPremiumIncome", "GrossPremiumIncome"),
    ResultsSchema.GENERAL_INSURANCE: ("PremiumEarned", "NetPremiumWritten"),
}
_DEFAULT_REVENUE = ("RevenueFromOperations",)
_PBT = (
    "ProfitBeforeTax",
    "ProfitLossFromOrdinaryActivitiesBeforeTax",
    "ProfitLossBeforeTax",
    "ProfitOrLossBeforeTax",
)
_PAT = (
    "ProfitLossForPeriod",
    "ProfitLossForThePeriod",
    "ProfitLossAfterTaxAndExtraordinaryItems",
    "ProfitLossAfterTaxBeforeExtraordinaryItems",
    "ProfitLossAfterTax",
)
_PAT_OWNERS = (
    "ProfitOrLossAttributableToOwnersOfParent",
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
)
_EPS_BASIC = (
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "BasicEarningsPerShareAfterExtraordinaryItems",
    "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized",
    "BasicEarningsLossPerShareFromContinuingOperations",
)
_EPS_DILUTED = (
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "DilutedEarningsPerShareAfterExtraordinaryItems",
    "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized",
    "DilutedEarningsLossPerShareFromContinuingOperations",
)


@dataclass(frozen=True, slots=True)
class QuarterlyResult:
    """One filed result (one period, standalone or consolidated). Rupees unless stated.

    ``ebitda_inr`` is derived for non-financial companies only:
    profit before exceptional items and tax + finance costs + depreciation − other income.
    For banks ``operating_profit_inr`` is operating profit before provisions; for NBFCs and
    insurers EBITDA is not meaningful (interest is their cost of goods) and is left empty.
    """

    symbol: str
    isin: str
    company: str
    schema: str
    period_start: date
    period_end: date
    period_months: int
    reporting_type: str  # Quarterly | Half yearly | Annual (as filed)
    consolidated: bool
    audited: bool | None
    revenue_inr: float | None
    total_income_inr: float | None
    ebitda_inr: float | None
    operating_profit_inr: float | None
    pbt_inr: float | None
    pat_inr: float | None
    pat_owners_inr: float | None
    eps_basic: float | None
    eps_diluted: float | None
    board_meeting_date: date | None
    known_at: datetime
    source_url: str
    filing_id: str
    revision: str  # Original | Revised (as listed by NSE)


def detect_results_schema(doc: XbrlDoc, source_url: str = "") -> ResultsSchema:
    for rx, text in ((_COMMENT, doc.header), (_NAMESPACE, doc.header), (_URL, source_url)):
        m = rx.search(text)
        if m and m.group(1).upper() in _SCHEMA_TOKENS:
            return _SCHEMA_TOKENS[m.group(1).upper()]
    names = {f.name for f in doc.facts}
    if "OperatingProfitBeforeProvisionAndContingencies" in names:
        return ResultsSchema.BANKING
    if "GrossPremiumIncome" in names:
        return ResultsSchema.LIFE_INSURANCE
    if "RevenueFromOperations" in names:
        return ResultsSchema.INDAS
    return ResultsSchema.UNKNOWN


def _period_context(doc: XbrlDoc) -> Context:
    """The non-dimensional duration context of the reporting period: the shortest one ending at
    the latest period end (a Q2 filing also carries the April–September half year)."""
    durations = [c for c in doc.contexts.values() if c.plain and c.end and c.start]
    if not durations:
        raise XbrlError("no duration context")
    end = max(c.end for c in durations if c.end is not None)
    at_end = [c for c in durations if c.end == end]
    return min(at_end, key=lambda c: c.days or 0)


def _first(doc: XbrlDoc, names: tuple[str, ...], ctx: str) -> float | None:
    for n in names:
        v = doc.number(n, ctx)
        if v is not None:
            return v
    return None


def parse_results_xbrl(
    data: bytes,
    *,
    known_at: datetime,
    source_url: str = "",
    filing_id: str = "",
    revision: str = "Original",
) -> QuarterlyResult:
    if known_at.tzinfo is None:
        raise ValueError("known_at must be timezone-aware")
    doc = parse_xbrl(data)
    schema = detect_results_schema(doc, source_url)
    ctx = _period_context(doc)
    if ctx.start is None or ctx.end is None:  # pragma: no cover - filtered above
        raise XbrlError("reporting period has no dates")
    c = ctx.id
    symbol = doc.text("Symbol") or ""
    if not symbol:
        raise XbrlError("no Symbol in filing")
    nature = (doc.text("NatureOfReportStandaloneConsolidated") or "").lower()
    audited_raw = (doc.text("WhetherResultsAreAuditedOrUnaudited") or "").lower()
    audited = None if not audited_raw else audited_raw.startswith("audited")
    revenue = _first(doc, _REVENUE.get(schema, _DEFAULT_REVENUE), c)
    ebitda = operating = None
    if schema is ResultsSchema.INDAS:
        pbe = doc.number("ProfitBeforeExceptionalItemsAndTax", c)
        fin = doc.number("FinanceCosts", c)
        dep = doc.number("DepreciationDepletionAndAmortisationExpense", c)
        oth = doc.number("OtherIncome", c)
        if pbe is not None and fin is not None and dep is not None:
            ebitda = pbe + fin + dep - (oth or 0.0)
        operating = ebitda
    elif schema is ResultsSchema.BANKING:
        operating = doc.number("OperatingProfitBeforeProvisionAndContingencies", c)
    pat = _first(doc, _PAT, c)
    return QuarterlyResult(
        symbol=symbol,
        isin=doc.text("ISIN") or "",
        company=doc.text("NameOfTheCompany") or doc.text("NameOfBank") or "",
        schema=schema.value,
        period_start=ctx.start,
        period_end=ctx.end,
        period_months=round((ctx.days or 0) / 30.4),
        reporting_type=doc.text("TypeOfReportingPeriod") or "",
        consolidated=nature.startswith("consolidated"),
        audited=audited,
        revenue_inr=revenue,
        total_income_inr=doc.number("Income", c),
        ebitda_inr=ebitda,
        operating_profit_inr=operating,
        pbt_inr=_first(doc, _PBT, c),
        pat_inr=pat,
        pat_owners_inr=_first(doc, _PAT_OWNERS, c),
        eps_basic=_first(doc, _EPS_BASIC, c),
        eps_diluted=_first(doc, _EPS_DILUTED, c),
        board_meeting_date=_date(doc.text("DateOfBoardMeetingWhenFinancialResultsWereApproved")),
        known_at=known_at,
        source_url=source_url,
        filing_id=filing_id,
        revision=revision,
    )


# --------------------------------------------------------------------------- shareholding

SHP_AXIS = "CategoryOfShareholdersAxis"
# Output column -> taxonomy member (SEBI shareholding pattern categories).
SHP_CATEGORIES: dict[str, str] = {
    "promoter_pct": "ShareholdingOfPromoterAndPromoterGroupMember",
    "public_pct": "PublicShareholdingMember",
    "fii_pct": "InstitutionsForeignMember",  # FPIs (cat I + II) + FDI + other foreign
    "fpi_cat1_pct": "InstitutionsForeignPortfolioInvestorCategoryOneMember",
    "dii_pct": "InstitutionsDomesticMember",
    "mf_pct": "MutualFundsOrUTIMember",
    "insurance_pct": "InsuranceCompaniesMember",
    "govt_pct": "GovernmentsMember",
    "retail_pct": "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember",
    "non_institutions_pct": "NonInstitutionsMember",
}


@dataclass(frozen=True, slots=True)
class ShareholdingPattern:
    """Category holdings in percent of total shares, as of ``as_on`` (quarter end)."""

    symbol: str
    isin: str
    company: str
    as_on: date
    promoter_pct: float | None
    public_pct: float | None
    fii_pct: float | None
    fpi_cat1_pct: float | None
    dii_pct: float | None
    mf_pct: float | None
    insurance_pct: float | None
    govt_pct: float | None
    retail_pct: float | None
    non_institutions_pct: float | None
    total_shares: float | None
    shareholders: float | None
    known_at: datetime
    source_url: str
    filing_id: str
    revision: str


def parse_shareholding_xbrl(
    data: bytes,
    *,
    known_at: datetime,
    source_url: str = "",
    filing_id: str = "",
    revision: str = "Original",
) -> ShareholdingPattern:
    if known_at.tzinfo is None:
        raise ValueError("known_at must be timezone-aware")
    doc = parse_xbrl(data)
    symbol = doc.text("Symbol") or ""
    if not symbol:
        raise XbrlError("no Symbol in filing")
    as_on = _date(doc.text("DateOfReport"))
    by_member: dict[str, str] = {}
    for c in doc.contexts.values():
        if c.typed or len(c.members) != 1 or c.instant is None:
            continue
        axis, member = c.members[0]
        if axis == SHP_AXIS and (as_on is None or c.instant == as_on):
            by_member[member] = c.id
            as_on = as_on or c.instant
    if as_on is None or not by_member:
        raise XbrlError("no shareholding category contexts")

    def pct(member: str) -> float | None:
        cid = by_member.get(member)
        if cid is None:
            return None
        v = doc.number("ShareholdingAsAPercentageOfTotalNumberOfShares", cid)
        return None if v is None else round(v * 100.0, 4)

    total_ctx = by_member.get("ShareholdingPatternMember", "")
    vals = {col: pct(member) for col, member in SHP_CATEGORIES.items()}
    return ShareholdingPattern(
        symbol=symbol,
        isin=doc.text("ISIN") or "",
        company=doc.text("NameOfTheCompany") or "",
        as_on=as_on,
        promoter_pct=vals["promoter_pct"],
        public_pct=vals["public_pct"],
        fii_pct=vals["fii_pct"],
        fpi_cat1_pct=vals["fpi_cat1_pct"],
        dii_pct=vals["dii_pct"],
        mf_pct=vals["mf_pct"],
        insurance_pct=vals["insurance_pct"],
        govt_pct=vals["govt_pct"],
        retail_pct=vals["retail_pct"],
        non_institutions_pct=vals["non_institutions_pct"],
        total_shares=doc.number("NumberOfShares", total_ctx) if total_ctx else None,
        shareholders=doc.number("NumberOfShareholders", total_ctx) if total_ctx else None,
        known_at=known_at,
        source_url=source_url,
        filing_id=filing_id,
        revision=revision,
    )
