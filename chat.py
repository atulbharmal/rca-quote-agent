"""Chat with the Revenue Cloud agent in the terminal.

    python chat.py

Claude is given the tools from mcp_server.py and decides which to call, in
what order, from what you type. Every tool call is shown as it happens.
Tools that change data (added later) pause and ask you to approve first.

Type 'exit' to quit, 'new' to start a fresh conversation.
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

load_dotenv()

MODEL = os.getenv("LLM_MODEL", "anthropic:claude-sonnet-5-5")
HERE = Path(__file__).parent
LOG_DIR = HERE / "logs"

# Tools that change Salesforce data must be approved by a person first.
# (Names are listed here ahead of time; the tools themselves come next.)
WRITE_TOOLS = {"create_quote", "add_quote_line", "update_quote_line", "apply_discount",
               "create_amendment", "generate_document"}

SYSTEM_PROMPT = """You are a Revenue Cloud quoting assistant for sales reps and deal desk.

How you work
- Only offer actions you have a tool for. You can look things up, explain prices,
  create quotes, add products, change line quantities and apply discounts. You can also generate a
  customer-facing quote document. You cannot amend assets yet.
- Before any change, look up the real Ids first (find_accounts, search_products,
  get_quote). Say in one line what you are about to do; the user approves each change.
- After a change, report what the tool returned from Salesforce, not what you asked
  for. If the tool lists defaults_used (start date, billing frequency, term), tell
  the user which defaults were applied. If the user asked for a discount, offer to explain the new price.
- For generate_quote_document, write the cover_note yourself: 2 to 5 friendly,
  plain sentences for the customer, with no prices or numbers (the document's
  table has those). Then give the user the document's link as a markdown link,
  e.g. [Open the quote document](file:///...). In the browser app it is also shown inline.
- If a tool refuses (e.g. a discount above the limit), say so plainly. Never try to
  get around a limit by splitting it into smaller changes.
- Use the tools for every fact. Never invent Ids, product names, prices or numbers.
- If a name matches several accounts, products or quotes, list them briefly and ask which one.
- Keep answers short and plain. Use a small table when showing several lines or steps.

Explaining a price (the most important thing you do)
- Always call get_price_waterfall for the line first.
- Walk through the steps in sequence: where the price starts (list price), each step
  that changed it and by how much, and where it ends (net unit price, line total).
- Skip steps that did not change the price, but say they were checked if relevant.
- Only use numbers that appear in the tool output. You may state the difference between
  two numbers shown (e.g. 119 to 95.20 is -23.80), labelled as such.
- Never predict what a price would be after a change.
- Check the bundle structure (bundle_structure in get_quote, bundle in
  get_price_waterfall). A component line whose relationship says its price is
  included in the bundle price is priced at 0 because the bundle line's price covers it.
  Say which bundle line it belongs to.
- When a step names a discount schedule, call get_adjustment_schedule (with the
  line's product Id) to read its tiers, then explain why it did or did not apply,
  e.g. "the first volume tier starts at 10 units; this line has 1".
- If the data does not explain something, say exactly what is missing. Do not guess.
- Name the Revenue Cloud element for each step (e.g. "Price Book Entries", a volume
  discount schedule, a manual discount) so the user can find it in Setup."""


def text_of(message) -> str:
    t = getattr(message, "text", None)
    if isinstance(t, str):
        return str(t)
    return str(message.content)


def short(value, n=160) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= n else s[:n] + "..."


def ask_approval(interrupt_value) -> dict:
    """Show each pending write action and collect approve / reject decisions."""
    decisions = []
    for req in interrupt_value.get("action_requests", []):
        print(f"\n  ⚠ The agent wants to run: {req.get('name')}")
        print(f"    with: {json.dumps(req.get('args', {}), indent=2, default=str)}")
        answer = input("    Approve? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            decisions.append({"type": "approve"})
        else:
            reason = input("    Reason (optional): ").strip() or "The user declined."
            decisions.append({"type": "reject", "message": reason})
    return {"decisions": decisions}


async def run_turn(agent, payload, config, log) -> None:
    shown: set[str] = set()  # tool calls already printed (they can reappear after an approval)
    while True:
        interrupted = None
        async for chunk in agent.astream(payload, config, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "__interrupt__":
                    interrupted = update[0].value
                    continue
                for m in (update or {}).get("messages", []) if isinstance(update, dict) else []:
                    if isinstance(m, AIMessage):
                        usage = m.usage_metadata or {}
                        log.append({"type": "model", "tool_calls": m.tool_calls,
                                    "input_tokens": usage.get("input_tokens", 0),
                                    "output_tokens": usage.get("output_tokens", 0)})
                        for call in m.tool_calls:
                            if call.get("id") not in shown:
                                shown.add(call.get("id"))
                                print(f"  → {call['name']}({short(call['args'], 120)})")
                        if not m.tool_calls and text_of(m).strip():
                            print(f"\nAgent: {text_of(m).strip()}\n")
                    elif isinstance(m, ToolMessage):
                        status = "error" if m.status == "error" else "ok"
                        log.append({"type": "tool", "name": m.name, "status": status,
                                    "chars": len(str(m.content))})
                        print(f"  ← {m.name}: {status} ({len(str(m.content))} chars)")
        if not interrupted:
            return
        payload = Command(resume=ask_approval(interrupted))


async def main() -> None:
    client = MultiServerMCPClient({
        "salesforce": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(HERE / "mcp_server.py")],
            "cwd": str(HERE),
            # Pass the full environment: by default MCP gives the server only a few
            # variables, and the Salesforce CLI needs more to find its login.
            "env": dict(os.environ),
        }
    })
    # One MCP server process for the whole chat (not one per tool call):
    # faster, and real errors come back instead of being swallowed.
    async with client.session("salesforce") as session:
        tools = await load_mcp_tools(session)
        print(f"Connected to MCP server: {len(tools)} tools ({', '.join(t.name for t in tools)})")
        await chat_loop(tools)


async def chat_loop(tools) -> None:
    agent = create_agent(
        model=init_chat_model(MODEL),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=[HumanInTheLoopMiddleware(interrupt_on={
            name: {"allowed_decisions": ["approve", "reject"]}
            for name in WRITE_TOOLS if name in {t.name for t in tools}
        })],
        checkpointer=InMemorySaver(),  # remembers the conversation within this session
    )

    thread = str(uuid.uuid4())
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"chat-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    print(f"Model: {MODEL}. Type 'exit' to quit, 'new' for a fresh conversation.\n")

    while True:
        try:
            user = (await asyncio.to_thread(input, "You: ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            continue
        if user.lower() in ("exit", "quit"):
            break
        if user.lower() == "new":
            thread = str(uuid.uuid4())
            print("(started a new conversation)\n")
            continue
        log: list[dict] = [{"type": "user", "text": user}]
        try:
            await run_turn(agent, {"messages": [("user", user)]}, {"configurable": {"thread_id": thread}}, log)
        except Exception as e:
            print(f"\n  (error: {type(e).__name__}: {e})")
            print("  (details of tool errors are in logs/mcp_server.log)\n")
            log.append({"type": "error", "error": str(e)})
        tokens = sum(e.get("input_tokens", 0) + e.get("output_tokens", 0) for e in log)
        print(f"  [{tokens} tokens this turn]\n")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "turn": log}, default=str) + "\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye.")
