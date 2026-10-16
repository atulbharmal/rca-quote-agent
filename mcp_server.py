"""MCP server: Revenue Cloud tools for any MCP client (this project's chat agent, Claude Desktop, ...).

    python mcp_server.py        # normally started by chat.py, not by hand

Read-only tools for now:
    find_accounts, search_products, find_quotes, get_quote, get_price_waterfall,
    get_adjustment_schedule

Design rules
- Every number a tool returns comes from Salesforce. The model explains; Revenue
  Cloud's pricing engine calculates.
- Tools return compact, structured data, not raw API dumps: a raw waterfall is
  tens of thousands of characters, most of it plumbing the model doesn't need.
- Errors are raised as plain messages, so the model can tell the user what went wrong.
- Never print to stdout here: stdout is the MCP channel to the client.
"""

import functools
import logging
import re
from functools import lru_cache
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sf_client import SalesforceError, connect, soql_string

LOG_FILE = Path(__file__).parent / "logs" / "mcp_server.log"
LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rca-mcp")

mcp = FastMCP("revenue-cloud", log_level="WARNING")
sf = connect()


def tool(fn):
    """Register an MCP tool and log every call, with the full error if it fails."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        log.info("call %s %s", fn.__name__, kwargs)
        try:
            result = fn(*args, **kwargs)
            log.info("ok   %s", fn.__name__)
            return result
        except Exception:
            log.exception("FAIL %s", fn.__name__)
            raise
    return mcp.tool()(wrapper)

ID = re.compile(r"^[a-zA-Z0-9]{15,18}$")
PRICE_KEY = re.compile(r"price|subtotal|discount|adjust|amount|quantity|tier|uplift|total|term", re.I)


def _id(value: str, prefix: str, what: str) -> str:
    value = (value or "").strip()
    if not ID.match(value) or not value.startswith(prefix):
        raise ValueError(f"'{value}' is not a valid {what} Id (it should start with {prefix}).")
    return value


def _like(text: str) -> str:
    text = (text or "").strip().replace("%", "").replace("_", "\\_")
    if not text:
        raise ValueError("Search text is empty.")
    return soql_string(f"%{text}%")


def _query(soql: str) -> list[dict]:
    try:
        return sf.query(soql)
    except SalesforceError as e:
        raise ValueError(f"Salesforce query failed: {str(e).splitlines()[0]}")


def _flatten(r: dict) -> dict:
    """Turn {'Product2': {'Name': 'X'}} into {'Product2.Name': 'X'} and drop empty values."""
    out = {}
    for k, v in r.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if k2 != "attributes" and v2 not in (None, ""):
                    out[f"{k}.{k2}"] = v2
        elif v not in (None, ""):
            out[k] = v
    return out


SKIP_FIELDS = {"IsDeleted", "SystemModstamp", "CreatedById", "LastModifiedById", "CreatedDate",
               "LastModifiedDate", "LastViewedDate", "LastReferencedDate", "OwnerId"}


@lru_cache(maxsize=4)
def _fields(sobject: str) -> tuple[str, ...]:
    try:
        d = sf.describe(sobject)
    except SalesforceError:
        return ()
    return tuple(f["name"] for f in d["fields"] if f["name"] not in SKIP_FIELDS
                 and f.get("type") not in ("address", "location"))[:40]


# ---------- Accounts, products, quotes ----------

@tool
def find_accounts(name: str, limit: int = 5) -> list[dict]:
    """Find accounts whose name contains the given text. Use this to turn a
    customer name into an Account Id before creating or looking up quotes."""
    rows = _query(
        f"SELECT Id, Name, Type, Industry, BillingCountry FROM Account "
        f"WHERE Name LIKE {_like(name)} ORDER BY Name LIMIT {min(int(limit), 20)}"
    )
    return [_flatten(r) for r in rows]


@tool
def search_products(text: str, limit: int = 10) -> list[dict]:
    """Search active products by name or product code, with their list price
    and selling model in the standard price book."""
    like = _like(text)
    base = (
        "SELECT Id, Product2Id, Product2.Name, Product2.ProductCode, UnitPrice, {extra} "
        "FROM PricebookEntry WHERE IsActive = true AND Pricebook2.IsStandard = true "
        f"AND (Product2.Name LIKE {like} OR Product2.ProductCode LIKE {like}) "
        f"ORDER BY Product2.Name LIMIT {min(int(limit), 25)}"
    )
    try:  # Revenue Cloud price book entries carry a selling model
        rows = sf.query(base.format(extra="ProductSellingModelId, ProductSellingModel.Name, ProductSellingModel.SellingModelType"))
    except SalesforceError:
        rows = _query(base.format(extra="Product2.Family"))
    return [
        {**_flatten(r), "PricebookEntryId": r["Id"]}
        for r in rows
    ]


@tool
def find_quotes(text: str = "", account_name: str = "", limit: int = 10) -> list[dict]:
    """List recent quotes. Optionally filter by quote name text and/or account name."""
    where = []
    if text:
        where.append(f"Name LIKE {_like(text)}")
    if account_name:
        where.append(f"Account.Name LIKE {_like(account_name)}")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = _query(
        "SELECT Id, Name, Status, Account.Name, TotalPrice, GrandTotal, CreatedDate FROM Quote "
        f"{clause} ORDER BY CreatedDate DESC LIMIT {min(int(limit), 25)}"
    )
    return [_flatten(r) for r in rows]


@tool
def get_quote(quote_id: str) -> dict:
    """Get a quote with all its lines: product, quantity, list price, unit price,
    net unit price, discount, adjustments and term. Use the line Ids it returns
    with get_price_waterfall."""
    qid = _id(quote_id, "0Q0", "Quote")
    header = _query(
        "SELECT Id, Name, Status, Account.Name, Pricebook2.Name, StartDate, Discount, "
        f"Subtotal, TotalPrice, GrandTotal FROM Quote WHERE Id = {soql_string(qid)}"
    )
    if not header:
        raise ValueError(f"Quote {qid} not found.")
    lines = _query(
        "SELECT Id, Product2Id, Product2.Name, Product2.ProductCode, Quantity, ListPrice, UnitPrice, NetUnitPrice, "
        "Discount, DiscountAmount, TotalAdjustmentAmount, TotalPrice, SellingModelType, "
        "StartDate, EndDate, SubscriptionTerm, SubscriptionTermUnit "
        f"FROM QuoteLineItem WHERE QuoteId = {soql_string(qid)} ORDER BY SortOrder, CreatedDate"
    )
    links = _bundle_links([l["Id"] for l in lines])
    names = {l["Id"]: (l.get("Product2") or {}).get("Name") for l in lines}
    for link in links:
        for key in ("MainQuoteLineId", "AssociatedQuoteLineId"):
            if link.get(key) in names:
                link[key.replace("Id", "Product")] = names[link[key]]
    return {
        "quote": _flatten(header[0]),
        "lines": [_flatten(l) for l in lines],
        **({"bundle_structure": links} if links else {}),
    }


def _bundle_links(line_ids: list[str]) -> list[dict]:
    """Bundle structure from QuoteLineRelationship: which line is a component of
    which bundle line, and whether its price is included in the bundle price."""
    fields = _fields("QuoteLineRelationship")
    if not fields or not line_ids:
        return []
    ids = ", ".join(soql_string(i) for i in line_ids)
    main = "MainQuoteLineId" if "MainQuoteLineId" in fields else None
    assoc = "AssociatedQuoteLineId" if "AssociatedQuoteLineId" in fields else None
    if not (main and assoc):
        return []
    rows = _query(
        f"SELECT {', '.join(fields)} FROM QuoteLineRelationship "
        f"WHERE {main} IN ({ids}) OR {assoc} IN ({ids})"
    )
    return [{k: v for k, v in _flatten(r).items() if k not in ("Id", "Name")} for r in rows]


# ---------- Price waterfall ----------

@lru_cache(maxsize=1)
def _adjustment_fields() -> tuple[str, ...]:
    skip = {"IsDeleted", "SystemModstamp", "CreatedById", "LastModifiedById", "CreatedDate", "LastModifiedDate"}
    try:
        fields = [f["name"] for f in sf.describe("QuoteLinePriceAdjustment")["fields"]]
    except SalesforceError:
        return ()
    return tuple(f for f in fields if f not in skip)[:30]


def _stored_adjustments(line_id: str) -> list[dict]:
    fields = _adjustment_fields()
    if not fields:
        return []
    rows = _query(
        f"SELECT {', '.join(fields)} FROM QuoteLinePriceAdjustment "
        f"WHERE QuoteLineItemId = {soql_string(line_id)}"
    )
    return [{k: v for k, v in _flatten(r).items() if k not in ("Id", "QuoteLineItemId")} for r in rows]


def _price_values(params: dict) -> dict:
    """Keep only the price-related values of a step, dropping plumbing and nulls."""
    return {
        k: v for k, v in (params or {}).items()
        if v is not None and PRICE_KEY.search(k) and not k.startswith("section-") and not isinstance(v, (dict, list))
    }


def compact_waterfall(raw: dict) -> dict:
    steps = []
    for s in sorted(raw.get("waterfall") or [], key=lambda s: s.get("sequence", 0)):
        element = s.get("pricingElement") or {}
        adjustments = [
            {k: v for k, v in a.items() if v is not None}
            for a in element.get("adjustments") or []
            if any(v is not None for v in a.values())
        ]
        # Discount schedule Ids (prefix 84X) used by this step, so the agent can
        # look up the schedule's rules with get_adjustment_schedule.
        values = list((s.get("inputParameters") or {}).values()) + list((s.get("outputParameters") or {}).values()) \
            + list(element.values())
        schedule_ids = sorted({v for v in values if isinstance(v, str) and v.startswith("84X")})
        steps.append({
            "sequence": s.get("sequence"),
            "step": element.get("name"),
            "type": element.get("elementType"),
            **({"adjustments": adjustments} if adjustments else {}),
            **({"schedule_ids": schedule_ids} if schedule_ids else {}),
            "in": _price_values(s.get("inputParameters")),
            "out": _price_values(s.get("outputParameters")),
        })
    return {
        "executed_at": raw.get("executionEndTimestamp"),
        "currency": raw.get("currencyCode"),
        "final": _price_values(raw.get("output")),
        "steps": steps,
    }


@tool
def get_price_waterfall(quote_line_id: str) -> dict:
    """Explain how a quote line's price was calculated. Returns the line's prices,
    every step of the Revenue Cloud pricing procedure in order (with each step's
    price in, price out and adjustments), and the price adjustments stored on the
    line (such as manual discounts). Base every pricing explanation on this data."""
    lid = _id(quote_line_id, "0QL", "Quote Line Item")
    rows = _query(
        "SELECT Id, QuoteId, Product2Id, Product2.Name, Quantity, ListPrice, UnitPrice, NetUnitPrice, Discount, "
        "DiscountAmount, TotalAdjustmentAmount, TotalPrice, SellingModelType, PriceWaterfallIdentifier "
        f"FROM QuoteLineItem WHERE Id = {soql_string(lid)}"
    )
    if not rows:
        raise ValueError(f"Quote line {lid} not found.")
    line = rows[0]
    ident = line.pop("PriceWaterfallIdentifier", None) or ""
    if ":" not in ident:
        raise ValueError("No price waterfall is saved for this line. Reprice the quote in Salesforce, then try again.")
    line_ref, execution_id = ident.split(":", 1)
    try:
        raw = sf.rest("GET", f"/connect/core-pricing/waterfall/{line_ref}/{execution_id}")
    except SalesforceError as e:
        raise ValueError(f"The waterfall API failed: {str(e).splitlines()[0][:300]}")
    return {
        "line": _flatten(line),
        "waterfall": compact_waterfall(raw or {}),
        "stored_adjustments": _stored_adjustments(lid),
        "bundle": _bundle_links([lid]),
    }


# ---------- Discount schedules (the rules behind waterfall steps) ----------

@tool
def get_adjustment_schedule(schedule_id: str, product_id: str = "") -> dict:
    """Get the rules of a price adjustment schedule (volume, tier, attribute or
    bundle discounts): the schedule itself and its tiers, such as quantity ranges
    and the discount each range gives. Use it when a waterfall step names a
    schedule, to explain why a discount did or did not apply. Pass product_id
    (starts with 01t) to show only the tiers for that product."""
    sid = _id(schedule_id, "", "Price Adjustment Schedule")
    sched_fields = _fields("PriceAdjustmentSchedule")
    tier_fields = _fields("PriceAdjustmentTier")
    if not sched_fields:
        raise ValueError("Price adjustment schedules are not readable in this org.")
    sched = _query(f"SELECT {', '.join(sched_fields)} FROM PriceAdjustmentSchedule WHERE Id = {soql_string(sid)}")
    if not sched:
        raise ValueError(f"Schedule {sid} not found.")
    tiers = []
    if tier_fields:
        where = f"PriceAdjustmentScheduleId = {soql_string(sid)}"
        if product_id and "Product2Id" in tier_fields:
            pid = _id(product_id, "01t", "Product")
            where += f" AND (Product2Id = {soql_string(pid)} OR Product2Id = null)"
        order = "LowerBound" if "LowerBound" in tier_fields else "Id"
        tiers = _query(f"SELECT {', '.join(tier_fields)} FROM PriceAdjustmentTier WHERE {where} ORDER BY {order} LIMIT 50")
    return {
        "schedule": _flatten(sched[0]),
        "tiers": [{k: v for k, v in _flatten(t).items() if k != "PriceAdjustmentScheduleId"} for t in tiers],
    }


if __name__ == "__main__":
    mcp.run()  # stdio transport
