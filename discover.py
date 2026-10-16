"""Find out how the Revenue Cloud org is set up, so the agent's tools can be built for it.

    python discover.py

Read-only: it changes nothing in Salesforce. Prints a summary and saves the
details to discovery.json (kept out of git). Each check runs on its own, so
one failure doesn't stop the rest.
"""

import json
import re

from sf_client import SalesforceError, connect

sf = connect()
report: dict = {}


def check(name: str, fn):
    try:
        result = fn()
        report[name] = result
        return result
    except SalesforceError as e:
        report[name] = {"error": str(e)[:500]}
        print(f"  ! {name}: {str(e).splitlines()[0][:160]}")
        return None


def show(title: str, rows, fields: list[str]):
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        return
    for r in rows:
        print("  - " + " | ".join(str(r.get(f, "")) for f in fields))


print("=== Org")
org = check("org", lambda: sf.query("SELECT Name, OrganizationType, IsSandbox FROM Organization")[0])
if org:
    print(f"  {org['Name']} ({org['OrganizationType']}, sandbox: {org['IsSandbox']})")

print("\n=== REST access through the CLI")
lim = check("rest_test", lambda: sf.rest("GET", "/limits"))
print("  REST calls work" if isinstance(lim, dict) and "DailyApiRequests" in lim else "  REST calls did NOT work (see error above)")

print("\n=== Revenue Cloud objects in this org")
names = check("sobjects", sf.list_sobjects) or []
interesting = [n for n in names if re.search(
    r"Pricing|Waterfall|PriceAdjust|SellingModel|ProductRelat|AssetState|AssetAction|SalesTransaction|DocumentTemplate|Amend|Execution",
    n)]
report["interesting_objects"] = interesting
for n in interesting[:60]:
    print(f"  {n}")
if len(interesting) > 60:
    print(f"  ... and {len(interesting) - 60} more (see discovery.json)")

show("=== Products (active, first 15)",
     check("products", lambda: sf.query("SELECT Id, Name, ProductCode FROM Product2 WHERE IsActive = true ORDER BY Name LIMIT 15")),
     ["Name", "ProductCode", "Id"])
show("=== Product selling models",
     check("selling_models", lambda: sf.query("SELECT Id, Name, SellingModelType, Status FROM ProductSellingModel LIMIT 20")),
     ["Name", "SellingModelType", "Status"])
show("=== Price books",
     check("pricebooks", lambda: sf.query("SELECT Id, Name, IsStandard, IsActive FROM Pricebook2")),
     ["Name", "IsStandard", "IsActive", "Id"])
show("=== Accounts (recent 5)",
     check("accounts", lambda: sf.query("SELECT Id, Name FROM Account ORDER BY LastModifiedDate DESC LIMIT 5")),
     ["Name", "Id"])
show("=== Assets (first 5)",
     check("assets", lambda: sf.query("SELECT Id, Name, AccountId, Quantity FROM Asset ORDER BY CreatedDate DESC LIMIT 5")),
     ["Name", "Quantity", "AccountId", "Id"])
show("=== Quotes (recent 3)",
     check("quotes", lambda: sf.query("SELECT Id, Name, Status, TotalPrice, AccountId FROM Quote ORDER BY CreatedDate DESC LIMIT 3")),
     ["Name", "Status", "TotalPrice", "Id"])

print("\n=== Pricing procedures")
procs = None
for soql in (
    "SELECT Id, Name, ApiName, UsageType FROM ExpressionSetDefinition LIMIT 20",
    "SELECT Id, DeveloperName, MasterLabel FROM ExpressionSetDefinition LIMIT 20",
    "SELECT Id, Name, ApiName, UsageType FROM ExpressionSet LIMIT 20",
):
    try:
        procs = sf.query(soql)
        report["pricing_procedures"] = {"soql": soql, "rows": procs}
        for p in procs:
            print("  - " + " | ".join(str(v) for k, v in p.items() if k != "Id"))
        break
    except SalesforceError:
        continue
if procs is None:
    print("  could not query pricing procedures directly (fine; we'll see them in the waterfall)")


def pricing_fields(obj: str) -> list[dict]:
    d = sf.describe(obj)
    keep = re.compile(r"Pricing|Execution|Waterfall|Discount|Adjust|NetUnit|ListPrice|UnitPrice|SellingModel|Subscription|Term|StartDate|EndDate|Asset|Original", re.I)
    return [{"name": f["name"], "type": f["type"], "label": f["label"]} for f in d["fields"] if keep.search(f["name"])]


for obj in ("Quote", "QuoteLineItem"):
    fields = check(f"{obj}_fields", lambda o=obj: pricing_fields(o)) or []
    print(f"\n=== {obj}: pricing-related fields")
    for f in fields:
        print(f"  {f['name']}  ({f['type']})  {f['label']}")

print("\n=== Latest quote line: values of pricing-related fields")
qli_fields = [f["name"] for f in (report.get("QuoteLineItem_fields") or []) if isinstance(report.get("QuoteLineItem_fields"), list)]
line = None
if qli_fields:
    cols = ", ".join(["Id", "QuoteId", "Product2.Name", "Quantity"] + [f for f in qli_fields if f not in ("Quantity",)][:40])
    rows = check("latest_quote_line", lambda: sf.query(f"SELECT {cols} FROM QuoteLineItem ORDER BY CreatedDate DESC LIMIT 1"))
    if rows:
        line = rows[0]
        for k, v in line.items():
            if v not in (None, ""):
                print(f"  {k}: {v}")
    else:
        print("  no quote lines yet: create one quote by hand, then run this again")

print("\n=== Price waterfall test")
if line:
    exec_ids = {k: v for k, v in line.items() if "xecution" in k and isinstance(v, str) and v}
    if not exec_ids:
        print("  no execution id field on the quote line; we'll find where it's stored together")
    for field, exec_id in exec_ids.items():
        wf = check(f"waterfall_via_{field}", lambda e=exec_id: sf.rest("GET", f"/connect/core-pricing/waterfall/{line['Id']}/{e}"))
        if wf and not (isinstance(wf, dict) and "error" in wf):
            print(f"  waterfall returned using {field}; saved in discovery.json")
else:
    print("  skipped (no quote line)")

with open("discovery.json", "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, default=str)
print("\nSaved details to discovery.json")
