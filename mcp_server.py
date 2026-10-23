"""MCP server: Revenue Cloud tools for any MCP client (this project's chat agent, Claude Desktop, ...).

    python mcp_server.py        # normally started by chat.py, not by hand

Read tools: find_accounts, search_products, find_quotes, get_quote,
    get_price_waterfall, get_adjustment_schedule
Write tools (approved by a person in the chat): create_quote, add_quote_line,
    update_quote_line, apply_discount
Document tool (reads only): generate_quote_document

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
import os
import re
import time
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
        "SELECT Id, Name, Status, Account.Name, Opportunity.Name, Pricebook2.Name, StartDate, Discount, "
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


# ---------- Write tools (the chat agent asks a person to approve each call) ----------
#
# All three go through Revenue Cloud's Place Sales Transaction API, so Revenue Cloud
# validates and prices every change itself. After each change the tool reads the
# records back and returns what Salesforce actually holds, not what was requested.

PLACE = "/connect/rev/sales-transaction/actions/place"
MAX_DISCOUNT = float(os.getenv("MAX_DISCOUNT_PERCENT", "30"))  # above this: deal desk, not the agent


def _place(graph_id: str, records: list[dict]) -> str:
    body = {"pricingPref": "System", "graph": {"graphId": graph_id, "records": records}}
    try:
        res = sf.rest("POST", PLACE, body) or {}
    except SalesforceError as e:
        raise ValueError(f"Revenue Cloud rejected the change: {str(e).splitlines()[0][:400]}")
    if not res.get("isSuccess", False):
        errors = res.get("errorResponse") or res
        raise ValueError(f"Revenue Cloud rejected the change: {str(errors)[:400]}")
    return res.get("salesTransactionId") or ""


def _quote_header(quote_id: str) -> dict:
    """Place Sales Transaction needs the quote in the record list, even when only
    its lines change: send it as a PATCH of the existing quote with no field changes."""
    return {"referenceId": "refQuote", "record": {"attributes": {"type": "Quote", "method": "PATCH", "id": quote_id}}}


@lru_cache(maxsize=1)
def _quote_createable() -> frozenset:
    try:
        return frozenset(f["name"] for f in sf.describe("Quote")["fields"] if f.get("createable"))
    except SalesforceError:
        return frozenset()


def _line_prices(line_id: str) -> dict:
    rows = _query(
        "SELECT Id, Product2.Name, Quantity, ListPrice, UnitPrice, Discount, NetUnitPrice, TotalPrice "
        f"FROM QuoteLineItem WHERE Id = {soql_string(line_id)}"
    )
    if not rows:
        raise ValueError(f"Quote line {line_id} not found.")
    return _flatten(rows[0])


def _require_draft(quote_id: str) -> dict:
    rows = _query(f"SELECT Id, Name, Status FROM Quote WHERE Id = {soql_string(quote_id)}")
    if not rows:
        raise ValueError(f"Quote {quote_id} not found.")
    if rows[0].get("Status") not in (None, "Draft"):
        raise ValueError(f"Quote '{rows[0]['Name']}' is {rows[0]['Status']}; only Draft quotes can be changed.")
    return rows[0]


@tool
def create_quote(account_id: str, quote_name: str, opportunity_id: str = "") -> dict:
    """Create a new, empty Draft quote for an account on the standard price book.
    Requires approval. Use find_accounts first to get the account Id. If
    opportunity_id is not given, the account's most recent open opportunity is used
    when there is one. Add products afterwards with add_quote_line."""
    aid = _id(account_id, "001", "Account")
    name = (quote_name or "").strip()
    if not name:
        raise ValueError("A quote name is required.")
    pricebook = _query("SELECT Id FROM Pricebook2 WHERE IsStandard = true AND IsActive = true LIMIT 1")
    if not pricebook:
        raise ValueError("No active standard price book found.")
    record = {"attributes": {"type": "Quote", "method": "POST"}, "Name": name, "Pricebook2Id": pricebook[0]["Id"]}
    createable = _quote_createable()
    if opportunity_id:
        record["OpportunityId"] = _id(opportunity_id, "006", "Opportunity")
    else:
        opp = _query(f"SELECT Id FROM Opportunity WHERE AccountId = {soql_string(aid)} AND IsClosed = false "
                     "ORDER BY CreatedDate DESC LIMIT 1")
        if opp:
            record["OpportunityId"] = opp[0]["Id"]
    if "AccountId" in createable:
        record["AccountId"] = aid
    quote_id = _place("createQuote", [{"referenceId": "refQuote", "record": record}])
    if not quote_id:
        raise ValueError("Revenue Cloud reported success but returned no quote Id.")
    return {"created_quote": get_quote(quote_id)["quote"]}


@lru_cache(maxsize=1)
def _billing_frequencies() -> tuple[str, ...]:
    """Billing Frequency picklist values allowed on quote lines in this org."""
    try:
        for f in sf.describe("QuoteLineItem")["fields"]:
            if f["name"] == "BillingFrequency":
                return tuple(v["value"] for v in f.get("picklistValues", []) if v.get("active", True))
    except SalesforceError:
        pass
    return ()


def _pick_frequency(wanted: str) -> str:
    """Match a frequency like 'Monthly' or 'Yearly' to this org's picklist value."""
    allowed = _billing_frequencies()
    key = (wanted or "").lower().replace("-", "").replace(" ", "")
    aliases = {"yearly": "annual", "annually": "annual", "semiannually": "semiannual"}
    key = aliases.get(key, key)
    for v in allowed:
        if v.lower().replace("-", "").replace(" ", "") .startswith(key[:5]):
            return v
    if allowed:
        raise ValueError(f"Billing frequency '{wanted}' is not allowed here. Allowed: {', '.join(allowed)}.")
    return wanted


@tool
def add_quote_line(quote_id: str, pricebook_entry_id: str, quantity: float = 1,
                   start_date: str = "", subscription_term_months: int = 0,
                   billing_frequency: str = "") -> dict:
    """Add a product to a Draft quote. Requires approval. Use search_products first:
    it returns the PricebookEntryId. For subscription products (Evergreen or
    Term-Defined selling models; the selling model comes from the price book entry)
    the tool sets a billing frequency (from the selling model, e.g. 'Evergreen - Monthly' -> Monthly, unless
    billing_frequency is given), uses today as the start date unless start_date
    (YYYY-MM-DD) is given, and uses a 12-month term for Term-Defined products unless
    subscription_term_months is given. The result lists the defaults it used and
    the quote's lines as Salesforce now holds them."""
    qid = _id(quote_id, "0Q0", "Quote")
    pbe_id = _id(pricebook_entry_id, "01u", "Price Book Entry")
    if not (0 < float(quantity) <= 100000):
        raise ValueError("Quantity must be between 0 and 100,000.")
    _require_draft(qid)
    try:
        pbe = sf.query(
            "SELECT Id, Product2Id, Product2.Name, UnitPrice, IsActive, ProductSellingModelId, "
            "ProductSellingModel.Name, ProductSellingModel.SellingModelType "
            f"FROM PricebookEntry WHERE Id = {soql_string(pbe_id)}")
    except SalesforceError:
        pbe = _query(f"SELECT Id, Product2Id, Product2.Name, UnitPrice, IsActive FROM PricebookEntry WHERE Id = {soql_string(pbe_id)}")
    if not pbe or not pbe[0].get("IsActive"):
        raise ValueError("That price book entry does not exist or is not active.")
    entry = pbe[0]
    model = entry.get("ProductSellingModel") or {}
    model_type = model.get("SellingModelType") or "OneTime"

    record = {
        "attributes": {"type": "QuoteLineItem", "method": "POST"},
        "QuoteId": "@{refQuote.id}",
        "Product2Id": entry["Product2Id"],
        "PricebookEntryId": pbe_id,
        "UnitPrice": entry["UnitPrice"],
        "Quantity": f"{float(quantity):g}",
    }
    defaults = []
    if start_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date):
        raise ValueError("start_date must be YYYY-MM-DD.")
    # The selling model is not sent: Revenue Cloud takes it from the price book entry,
    # and the field is often not editable by the user (field-level security).
    if model_type in ("Evergreen", "TermDefined"):
        if not start_date:
            start_date = time.strftime("%Y-%m-%d")
            defaults.append(f"start date {start_date} (today)")
        if not billing_frequency:
            name = model.get("Name") or ""
            billing_frequency = next((w for w in ("Semi-Annual", "Quarterly", "Monthly", "Yearly", "Annual")
                                      if w.lower() in name.lower()), "Monthly")
            defaults.append(f"billing frequency from the selling model '{name}'")
        record["BillingFrequency"] = _pick_frequency(billing_frequency)
        if model_type == "TermDefined":
            if not subscription_term_months:
                subscription_term_months = 12
                defaults.append("12-month term")
            record["SubscriptionTerm"] = int(subscription_term_months)
            record["SubscriptionTermUnit"] = "Months"
    if start_date:
        record["StartDate"] = start_date

    _place("addQuoteLine", [_quote_header(qid), {"referenceId": "refLine", "record": record}])
    return {
        "added_product": entry["Product2"]["Name"],
        "selling_model": model.get("Name") or model_type,
        **({"defaults_used": defaults} if defaults else {}),
        "quote_now": get_quote(qid),
    }


@tool
def update_quote_line(quote_line_id: str, quantity: float) -> dict:
    """Change the quantity of a line on a Draft quote and let Revenue Cloud reprice
    it (volume or tier discounts may change as a result). Requires approval.
    Returns the line's prices before and after; use get_price_waterfall afterwards
    to explain the new price."""
    lid = _id(quote_line_id, "0QL", "Quote Line Item")
    qty = float(quantity)
    if not (0 < qty <= 100000):
        raise ValueError("Quantity must be between 0 and 100,000.")
    before = _line_prices(lid)
    quote_id = _query(f"SELECT QuoteId FROM QuoteLineItem WHERE Id = {soql_string(lid)}")[0]["QuoteId"]
    _require_draft(quote_id)
    _place("updateQuantity", [_quote_header(quote_id), {
        "referenceId": "refLine",
        "record": {"attributes": {"type": "QuoteLineItem", "method": "PATCH", "id": lid}, "Quantity": f"{qty:g}"},
    }])
    after = before
    for _ in range(6):  # repricing can finish a moment after the call returns
        after = _line_prices(lid)
        if float(after.get("Quantity") or 0) == qty and after.get("TotalPrice") != before.get("TotalPrice"):
            break
        time.sleep(1.5)
    note = "" if float(after.get("Quantity") or 0) == qty else "Salesforce has not shown the new quantity yet; check again shortly."
    return {"before": before, "after": after, **({"note": note} if note else {})}


@tool
def apply_discount(quote_line_id: str, discount_percent: float) -> dict:
    """Set a manual percentage discount on a quote line and let Revenue Cloud
    reprice it. Requires approval. Discounts above the deal desk limit are refused
    by this tool, whatever the request. Returns the line's prices before and after;
    use get_price_waterfall afterwards to explain the new price."""
    lid = _id(quote_line_id, "0QL", "Quote Line Item")
    pct = float(discount_percent)
    if pct < 0:
        raise ValueError("A discount cannot be negative.")
    if pct > MAX_DISCOUNT:
        raise ValueError(f"{pct:g}% is above the {MAX_DISCOUNT:g}% limit the agent may apply. "
                         "Discounts above that need deal desk approval in Salesforce.")
    before = _line_prices(lid)
    quote_id = _query(f"SELECT QuoteId FROM QuoteLineItem WHERE Id = {soql_string(lid)}")[0]["QuoteId"]
    _require_draft(quote_id)
    _place("applyDiscount", [_quote_header(quote_id), {
        "referenceId": "refLine",
        "record": {"attributes": {"type": "QuoteLineItem", "method": "PATCH", "id": lid}, "Discount": pct},
    }])
    after = before
    for _ in range(6):  # repricing can finish a moment after the call returns
        after = _line_prices(lid)
        if after.get("Discount") == pct and after.get("NetUnitPrice") != before.get("NetUnitPrice"):
            break
        time.sleep(1.5)
    note = "" if after.get("Discount") == pct else "Salesforce has not shown the new discount yet; check again shortly."
    return {"before": before, "after": after, **({"note": note} if note else {})}


# ---------- Quote document (built outside Salesforce) ----------
#
# Code renders every number from Salesforce; the model only supplies the cover
# note text. The document is an HTML file: open it in a browser, print to PDF.

import html as _html
from datetime import date

DOC_DIR = Path(__file__).parent / "documents"


CURRENCY = os.getenv("DOC_CURRENCY_SYMBOL", "$")


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _money(v) -> str:
    try:
        return f"{CURRENCY}{float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


DOC_CSS = """
 *{box-sizing:border-box}
 body{font-family:'Segoe UI',Inter,Arial,sans-serif;color:#1f2937;background:#eef2f7;margin:0;font-size:16px;line-height:1.5}
 .page{max-width:1040px;margin:32px auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 10px 30px rgba(15,23,42,.12)}
 .band{background:linear-gradient(120deg,#0b3d91,#0b5cab 60%,#1a8fd8);color:#fff;padding:40px 48px;display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap}
 .band .kicker{letter-spacing:.18em;font-size:13px;opacity:.85;font-weight:600}
 .band h1{font-size:36px;margin:6px 0 0;line-height:1.15}
 .band .meta{text-align:right;font-size:14px;opacity:.95}
 .band .meta b{display:block;font-size:20px}
 .body{padding:36px 48px 44px}
 .parties{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:28px}
 .label{text-transform:uppercase;letter-spacing:.12em;font-size:12px;color:#6b7280;font-weight:600;margin-bottom:6px}
 .party{font-size:20px;font-weight:600}
 .sub{color:#6b7280;font-size:14px}
 .note{background:#f3f7fd;border-left:5px solid #0b5cab;border-radius:8px;padding:18px 22px;margin:0 0 28px;font-size:17px;white-space:pre-wrap}
 .cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:32px}
 .card{border:1px solid #e5e7eb;border-radius:12px;padding:18px 20px}
 .card .v{font-size:28px;font-weight:700;margin-top:4px}
 .card.save .v{color:#047857}
 .card.total{background:#0b3d91;color:#fff;border-color:#0b3d91}
 .card.total .label{color:#c7d7f5}
 table{width:100%;border-collapse:collapse;font-size:15px}
 thead th{text-align:left;font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#6b7280;padding:12px 10px;border-bottom:2px solid #1f2937}
 tbody td{padding:16px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top}
 tbody tr:nth-child(even) td{background:#fafbfd}
 .n{text-align:right;white-space:nowrap}
 .prod{font-weight:600;font-size:16px}
 .code{color:#6b7280;font-size:13px}
 .chip{display:inline-block;font-size:12px;padding:2px 10px;border-radius:999px;background:#e8f0fe;color:#0b3d91;font-weight:600}
 .disc{color:#047857;font-weight:600}
 .strike{color:#9ca3af;text-decoration:line-through;font-size:13px;display:block}
 tfoot td{padding:14px 10px;font-size:16px}
 tfoot .grand td{font-size:20px;font-weight:700;border-top:2px solid #1f2937}
 .terms{margin-top:36px;display:grid;grid-template-columns:1.2fr 1fr;gap:32px}
 .terms ul{margin:6px 0 0;padding-left:18px;color:#4b5563;font-size:14px}
 .sign .line{border-bottom:1px solid #9ca3af;height:34px;margin-bottom:4px}
 .sign .cap{font-size:12px;color:#6b7280;margin-bottom:14px}
 .foot{padding:16px 48px;background:#f8fafc;color:#6b7280;font-size:12px;border-top:1px solid #e5e7eb}
 @media print{body{background:#fff}.page{box-shadow:none;margin:0;border-radius:0}.band{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .card.total{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


@tool
def generate_quote_document(quote_id: str, cover_note: str = "") -> dict:
    """Create a customer-facing quote document (HTML, printable to PDF) from the
    quote as Salesforce holds it now. Every product, price, discount and total is
    read from Salesforce and calculated by code. You supply only cover_note: 2 to 5
    plain sentences for the customer, with no prices or numbers in it. Read-only in
    Salesforce, so it needs no approval. Returns the file path and a clickable link."""
    data = get_quote(quote_id)
    q, lines = data["quote"], data["lines"]
    esc = _html.escape
    note = (cover_note or "").strip()[:1500]

    # A bundle component priced at 0 is included in the bundle line's price, so it
    # is not counted in the list value (otherwise it would show as fake savings).
    def included(l):
        return _num(l.get("TotalPrice")) == 0 and _num(l.get("ListPrice")) > 0
    list_value = sum(_num(l.get("ListPrice")) * _num(l.get("Quantity")) for l in lines if not included(l))
    total = _num(q.get("GrandTotal", q.get("TotalPrice")))
    savings = max(list_value - total, 0)

    rows = []
    for l in lines:
        disc = _num(l.get("Discount"))
        list_p, net = _num(l.get("ListPrice")), _num(l.get("NetUnitPrice"))
        model = l.get("SellingModelType") or ""
        model_label = {"OneTime": "One-time", "Evergreen": "Subscription", "TermDefined": "Term subscription"}.get(model, model)
        term = ""
        if l.get("SubscriptionTerm"):
            term = f"<div class=code>{esc(str(l['SubscriptionTerm']))} {esc(str(l.get('SubscriptionTermUnit', '')))}</div>"
        net_cell = (f"<span class=strike>{_money(list_p)}</span>{_money(net)}" if net < list_p else _money(net))
        line_total = f"<b>{_money(l.get('TotalPrice'))}</b>"
        if included(l):
            net_cell, line_total, disc = "&ndash;", "<span class=chip>Included</span>", 0
        rows.append(
            "<tr>"
            f"<td><div class=prod>{esc(str(l.get('Product2.Name', '')))}</div>"
            f"<div class=code>{esc(str(l.get('Product2.ProductCode', '')))}</div></td>"
            f"<td>{f'<span class=chip>{esc(model_label)}</span>' if model_label else ''}{term}</td>"
            f"<td class=n>{_num(l.get('Quantity')):g}</td>"
            f"<td class=n>{_money(list_p)}</td>"
            f"<td class=n>{f'<span class=disc>{disc:g}%</span>' if disc else '&ndash;'}</td>"
            f"<td class=n>{net_cell}</td>"
            f"<td class=n>{line_total}</td>"
            "</tr>"
        )

    title = esc(str(q.get("Name", "Quote")))
    account = esc(str(q.get("Account.Name", "") or "Customer"))
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{DOC_CSS}</style></head>
<body><div class="page">
 <div class="band">
  <div><div class="kicker">QUOTE</div><h1>{title}</h1></div>
  <div class="meta"><b>{date.today():%B %d, %Y}</b>Quote {esc(str(q.get('Id', '')))}<br>Status: {esc(str(q.get('Status', '')))}</div>
 </div>
 <div class="body">
  <div class="parties">
   <div><div class="label">Prepared for</div><div class="party">{account}</div>
    {f'<div class="sub">{esc(str(q["Opportunity.Name"]))}</div>' if q.get("Opportunity.Name") else ''}</div>
   <div><div class="label">Price book</div><div class="party">{esc(str(q.get('Pricebook2.Name', '')))}</div>
    <div class="sub">{len(lines)} line{'s' if len(lines) != 1 else ''}</div></div>
  </div>
  {f'<div class="note">{esc(note)}</div>' if note else ''}
  <div class="cards">
   <div class="card"><div class="label">List value</div><div class="v">{_money(list_value)}</div></div>
   <div class="card save"><div class="label">Your savings</div><div class="v">{_money(savings)}</div></div>
   <div class="card total"><div class="label">Total</div><div class="v">{_money(total)}</div></div>
  </div>
  <table>
   <thead><tr><th>Product</th><th>Type</th><th class=n>Qty</th><th class=n>List price</th><th class=n>Discount</th><th class=n>Net unit</th><th class=n>Line total</th></tr></thead>
   <tbody>{''.join(rows)}</tbody>
   <tfoot>
    <tr><td colspan=6 class=n>List value</td><td class=n>{_money(list_value)}</td></tr>
    <tr><td colspan=6 class=n>Discounts</td><td class=n>&minus;{_money(savings)}</td></tr>
    <tr class="grand"><td colspan=6 class=n>Total</td><td class=n>{_money(total)}</td></tr>
   </tfoot>
  </table>
  <div class="terms">
   <div><div class="label">Notes</div><ul>
    <li>Prices, discounts and totals are taken directly from Salesforce Revenue Cloud.</li>
    <li>Subscription lines are billed per their billing frequency from the start date.</li>
    <li>Taxes, if applicable, are not included.</li></ul></div>
   <div class="sign"><div class="label">Acceptance</div>
    <div class="line"></div><div class="cap">Signature</div>
    <div class="line"></div><div class="cap">Name and title</div>
    <div class="line"></div><div class="cap">Date</div></div>
  </div>
 </div>
 <div class="foot">Generated from Salesforce Revenue Cloud on {date.today():%Y-%m-%d}. Quote {esc(str(q.get('Id', '')))}.</div>
</div></body></html>"""
    DOC_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(q.get("Name", "quote")))[:60]
    path = DOC_DIR / f"{safe}_{date.today():%Y%m%d}.html"
    path.write_text(page, encoding="utf-8")
    return {"file": str(path), "link": path.resolve().as_uri(), "lines": len(lines), "total": total,
            "how_to_open": "Show the user the link as a markdown link; it opens the document in a browser."}


if __name__ == "__main__":
    mcp.run()  # stdio transport
