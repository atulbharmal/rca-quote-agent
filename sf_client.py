"""Talk to the Revenue Cloud org through the Salesforce CLI.

Log in once:
    sf org login web --alias rcadev

Python never handles a password or access token: every call goes through the
CLI, which keeps its own login. Three operations:

    query(soql)               -> list of records
    rest(method, path, body)  -> parsed JSON from any REST / Connect API endpoint
    describe(sobject)         -> field list of an object

    python sf_client.py       # quick connection test
"""

import json
import os
import shutil
import subprocess
import tempfile

from dotenv import load_dotenv

load_dotenv()


class SalesforceError(RuntimeError):
    pass


class SalesforceCLI:
    def __init__(self, alias: str, api_version: str):
        self.alias = alias
        self.api_version = api_version
        self.sf = shutil.which("sf")
        if not self.sf:
            raise SalesforceError("Salesforce CLI ('sf') not found. Install it and open a new terminal.")

    def _exec(self, args: list[str]) -> subprocess.CompletedProcess:
        # stdin=DEVNULL matters: inside the MCP server, stdin is the channel to the
        # chat agent. Without this, sf inherits it and both sides wait forever.
        cmd = [self.sf, *args, "--target-org", self.alias]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        try:
            out, err = proc.communicate(timeout=float(os.getenv("SF_TIMEOUT", "60")))
        except subprocess.TimeoutExpired:
            # On Windows, sf is a script that starts node.exe; killing only the script
            # leaves node holding the pipes open. Kill the whole process tree.
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdin=subprocess.DEVNULL, capture_output=True)
            else:
                proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise SalesforceError(f"sf {' '.join(args[:2])} took too long and was stopped. "
                                  f"Check the login with: sf org display --target-org {self.alias}")
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def _json(self, *args: str) -> dict:
        out = self._exec([*args, "--json"])
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            raise SalesforceError(f"Unexpected output from sf:\n{out.stdout}\n{out.stderr}")
        if data.get("status") != 0:
            raise SalesforceError(
                f"{data.get('message')}\nIf the login expired, run: sf org login web --alias {self.alias}"
            )
        return data["result"]

    def query(self, soql: str) -> list[dict]:
        records = self._json("data", "query", "--query", soql)["records"]
        for r in records:
            r.pop("attributes", None)
        return records

    def describe(self, sobject: str) -> dict:
        return self._json("sobject", "describe", "--sobject", sobject)

    def list_sobjects(self) -> list[str]:
        return self._json("sobject", "list", "--sobject", "all")

    def rest(self, method: str, path: str, body: dict | None = None):
        """Call a REST or Connect API path, e.g. ('GET', '/connect/core-pricing/...').

        Paths without /services/ are treated as relative to /services/data/vXX.0.
        """
        if not path.startswith("/services/"):
            path = f"/services/data/v{self.api_version}{path if path.startswith('/') else '/' + path}"
        args = ["api", "request", "rest", path, "--method", method.upper()]
        tmp = None
        if body is not None:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
                json.dump(body, f)
                tmp = f.name
            args += ["--body", tmp]
        try:
            out = self._exec(args)
        finally:
            if tmp:
                os.remove(tmp)
        text = (out.stdout or "").strip()
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            raise SalesforceError(f"{method} {path} failed:\n{text}\n{out.stderr.strip()}")
        # Salesforce returns errors as a list of {errorCode, message}
        if out.returncode != 0 or (isinstance(data, list) and data and isinstance(data[0], dict) and "errorCode" in data[0]):
            raise SalesforceError(f"{method} {path} failed: {json.dumps(data)[:1500]} {out.stderr.strip()}")
        return data


def connect() -> SalesforceCLI:
    return SalesforceCLI(os.getenv("SF_ORG_ALIAS", "rcadev"), os.getenv("SF_API_VERSION", "64.0"))


def soql_string(value: str) -> str:
    """Quote a value for use inside a SOQL string literal."""
    return "'" + (value or "").replace("\\", "\\\\").replace("'", "\\'") + "'"


if __name__ == "__main__":
    org = connect().query("SELECT Name FROM Organization")[0]["Name"]
    print(f"Connected to {org}")
