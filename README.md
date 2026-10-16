# Revenue Cloud quoting agent

A chat agent for Salesforce Revenue Cloud. You talk to it in plain English: create a quote for an account, add a product, apply a discount, generate the document, amend an asset. Then you ask it the hard question: why did this product land at this price? It explains the price waterfall step by step, quoting the real numbers from Revenue Cloud's pricing engine.

Built with Claude, LangGraph, and an MCP server of its own that talks to Salesforce through the Salesforce CLI.

> Work in progress. Read-only tools and price explanations work; quote creation, discounts, amendments and documents come next.

## Design principles

- **The pricing engine calculates, the AI explains.** Claude never works out a price. It reads the waterfall that Revenue Cloud produced and explains it, so every number in an explanation comes from Salesforce.
- **Nothing changes without a yes.** Read tools run freely. Any tool that creates or changes data shows what it will do and waits for approval.
- **Tools live in an MCP server**, so the same tools work from this chat agent, Claude Desktop, or any MCP client.
- **No credentials in the code.** Every Salesforce call goes through the Salesforce CLI login.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate           # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # Mac/Linux: cp; then add your Anthropic API key to .env
sf org login web --alias rcadev  # the Revenue Cloud org
python sf_client.py              # should print the org name
python discover.py               # read-only: how the org is set up
```

## Run it

```bash
python chat.py
```

Try:
- "Show me the recent quotes"
- "What's on the Cellular Tablet Purchase quote?"
- "Explain the price of the Standard Cellular Tablet line. Why is the net price what it is?"

Each tool call is shown as it happens, and every turn is logged to `logs/` with token counts.

## Tools (MCP server)

| Tool | What it does |
|---|---|
| `find_accounts` | Account name to Id |
| `search_products` | Products with list price and selling model from the standard price book |
| `find_quotes` | Recent quotes, filtered by name or account |
| `get_quote` | A quote and all its lines with prices, discounts and terms |
| `get_price_waterfall` | Every pricing procedure step for a line, in order, with price in, price out and adjustments, plus the adjustments stored on the line |

The raw waterfall API response is large and mostly plumbing, so the tool returns a compact version: each step's name, type, adjustments and price values only. That keeps the model's context small and focused (cheaper, and fewer chances to misread).

## Use the same tools from Claude Desktop

Add this to Claude Desktop's config file (Settings > Developer > Edit Config), with your own paths:

```json
{
  "mcpServers": {
    "revenue-cloud": {
      "command": "C:\\Users\\you\\Documents\\rca-quote-agent\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\you\\Documents\\rca-quote-agent\\mcp_server.py"]
    }
  }
}
```

## Files

| File | What it does |
|---|---|
| `chat.py` | The chat agent: Claude, tools loaded over MCP, memory, approvals for write tools |
| `mcp_server.py` | The MCP server and its Revenue Cloud tools |
| `sf_client.py` | SOQL, REST and describe calls through the Salesforce CLI |
| `discover.py` | Read-only check of how the org is set up |
| `probe_waterfall.py` | Read-only check of the waterfall API for one quote line |
