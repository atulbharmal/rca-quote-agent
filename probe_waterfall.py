"""Work out how to read the price waterfall for a quote line. Read-only.

    python probe_waterfall.py                    # latest quote line
    python probe_waterfall.py 0QLKY0000023bGr4AI # a specific quote line

Tries the waterfall API with the line's PriceWaterfallIdentifier split in the
likely ways, reads the line's price adjustments, and looks at the pricing
execution records. Prints a summary; saves everything to probe.json.
"""

import json
import sys

from sf_client import SalesforceError, connect, soql_string

sf = connect()
out: dict = {}


def try_call(label: str, fn):
    try:
        res = fn()
        out[label] = res
        print(f"  OK   {label}")
        return res
    except SalesforceError as e:
        out[label] = {"error": str(e)[:800]}
        print(f"  FAIL {label}: {str(e).splitlines()[0][:150]}")
        return None


line_id = sys.argv[1] if len(sys.argv) > 1 else None
where = f"WHERE Id = {soql_string(line_id)}" if line_id else "ORDER BY CreatedDate DESC"
line = sf.query(
    "SELECT Id, QuoteId, Product2.Name, Quantity, ListPrice, UnitPrice, NetUnitPrice, Discount, "
    f"DiscountAmount, TotalAdjustmentAmount, PriceWaterfallIdentifier FROM QuoteLineItem {where} LIMIT 1"
)[0]
out["line"] = line
print(f"Quote line {line['Id']}: {line['Product2']['Name']}, qty {line['Quantity']}, "
      f"list {line['ListPrice']}, unit {line['UnitPrice']}, net {line['NetUnitPrice']}")
ident = line.get("PriceWaterfallIdentifier") or ""
print(f"PriceWaterfallIdentifier: {ident or '(empty)'}")

print("\n1) Waterfall API, trying the identifier split different ways")
found = None
if ":" in ident:
    ref, exec_id = ident.split(":", 1)
    for label, a, b in (
        ("ref + exec", ref, exec_id),
        ("line Id + exec", line["Id"], exec_id),
        ("exec + ref", exec_id, ref),
    ):
        res = try_call(f"waterfall [{label}]", lambda a=a, b=b: sf.rest("GET", f"/connect/core-pricing/waterfall/{a}/{b}"))
        if res and not found:
            found = (label, res)
else:
    print("  no identifier on this line; reprice the quote in Salesforce and run again")

print("\n2) Price adjustments stored on the line (QuoteLinePriceAdjustment)")
adj_fields = try_call("QuoteLinePriceAdjustment describe", lambda: [f["name"] for f in sf.describe("QuoteLinePriceAdjustment")["fields"]])
if adj_fields:
    wanted = [f for f in adj_fields if f not in ("IsDeleted", "SystemModstamp", "CreatedById", "LastModifiedById")][:30]
    rows = try_call("QuoteLinePriceAdjustment rows", lambda: sf.query(
        f"SELECT {', '.join(wanted)} FROM QuoteLinePriceAdjustment WHERE QuoteLineItemId = {soql_string(line['Id'])}"))
    for r in rows or []:
        print("   - " + ", ".join(f"{k}={v}" for k, v in r.items() if v not in (None, "", False) and k != "Id"))
    if rows == []:
        print("   (no adjustments on this line)")

print("\n3) Pricing execution records (latest 3 of each)")
for obj in ("PricingProcessExecution", "PricingApiExecution"):
    fields = try_call(f"{obj} describe", lambda o=obj: [f["name"] for f in sf.describe(o)["fields"]])
    if fields:
        cols = [f for f in fields if f not in ("IsDeleted", "SystemModstamp", "CreatedById", "LastModifiedById")][:25]
        rows = try_call(f"{obj} rows", lambda o=obj, c=cols: sf.query(f"SELECT {', '.join(c)} FROM {o} ORDER BY CreatedDate DESC LIMIT 3"))
        for r in rows or []:
            print("   - " + ", ".join(f"{k}={str(v)[:60]}" for k, v in r.items() if v not in (None, "", False)))

if found:
    label, res = found
    print(f"\n=== Waterfall found using [{label}]. First part of the response:")
    print(json.dumps(res, indent=2)[:3000])

with open("probe.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, default=str)
print("\nSaved everything to probe.json")
