"""Browser chat page for the Revenue Cloud agent.

    streamlit run app.py

Same agent, tools and rules as chat.py. Tool calls are shown as they happen,
and any change to Salesforce waits for an Approve or Reject click.

How it works: Streamlit reruns this script on every click, but the MCP server
and agent must stay alive between clicks. So they live in one background thread
with its own event loop (AgentRuntime), created once and shared across reruns.
"""

import asyncio
import concurrent.futures
import json
import os
import sys
import threading
import uuid
import webbrowser
from pathlib import Path

import streamlit as st
import streamlit.components.v1  # noqa: F401  (inline document preview)
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import chat  # reuse the system prompt, model name and write-tool list

TOOL_LABELS = {
    "find_accounts": "Looking up accounts", "search_products": "Searching products",
    "find_quotes": "Finding quotes", "get_quote": "Reading the quote",
    "get_price_waterfall": "Reading the price waterfall", "get_adjustment_schedule": "Reading discount rules",
    "create_quote": "Creating a quote", "add_quote_line": "Adding a product",
    "update_quote_line": "Changing a quantity", "apply_discount": "Applying a discount",
    "generate_quote_document": "Creating the quote document",
}


def tool_json(content):
    """MCP tool results arrive as a JSON string or a list of text blocks."""
    if isinstance(content, list):
        content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return None


class AgentRuntime:
    """Owns the MCP session and the agent in a background event loop."""

    def __init__(self, model=None):
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue | None = None
        self.ready = concurrent.futures.Future()
        self.model = model
        threading.Thread(target=self._run, daemon=True).start()
        self.tool_names = self.ready.result(timeout=120)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._worker())

    async def _worker(self):
        self.queue = asyncio.Queue()
        try:
            client = MultiServerMCPClient({"salesforce": {
                "transport": "stdio", "command": sys.executable,
                "args": [str(chat.HERE / "mcp_server.py")], "cwd": str(chat.HERE),
                "env": dict(os.environ),
            }})
            async with client.session("salesforce") as session:
                tools = await load_mcp_tools(session)
                names = {t.name for t in tools}
                self.agent = create_agent(
                    model=self.model or init_chat_model(chat.MODEL),
                    tools=tools,
                    system_prompt=chat.SYSTEM_PROMPT,
                    middleware=[HumanInTheLoopMiddleware(interrupt_on={
                        n: {"allowed_decisions": ["approve", "reject"]} for n in chat.WRITE_TOOLS if n in names
                    })],
                    checkpointer=InMemorySaver(),
                )
                self.ready.set_result([t.name for t in tools])
                while True:  # every request runs inside this one task, next to the MCP session
                    fut, payload, thread = await self.queue.get()
                    try:
                        fut.set_result(await self._stream(payload, thread))
                    except Exception as e:  # noqa: BLE001
                        fut.set_exception(e)
        except Exception as e:  # noqa: BLE001
            if not self.ready.done():
                self.ready.set_exception(e)

    async def _stream(self, payload, thread) -> dict:
        events, interrupt, tokens = [], None, 0
        async for chunk in self.agent.astream(payload, {"configurable": {"thread_id": thread}}, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "__interrupt__":
                    interrupt = update[0].value
                    continue
                for m in (update or {}).get("messages", []) if isinstance(update, dict) else []:
                    if isinstance(m, AIMessage):
                        usage = m.usage_metadata or {}
                        tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                        for call in m.tool_calls:
                            events.append({"kind": "call", "id": call.get("id"), "name": call["name"], "args": call["args"]})
                        if not m.tool_calls and chat.text_of(m).strip():
                            events.append({"kind": "answer", "text": chat.text_of(m).strip()})
                    elif isinstance(m, ToolMessage):
                        if m.name == "generate_quote_document" and m.status != "error":
                            doc = tool_json(m.content) or {}
                            if doc.get("file"):
                                events.append({"kind": "document", "file": doc["file"], "link": doc.get("link", "")})
                        events.append({"kind": "result", "name": m.name,
                                       "ok": m.status != "error", "chars": len(str(m.content)),
                                       "error": str(m.content)[:300] if m.status == "error" else ""})
        return {"events": events, "interrupt": interrupt, "tokens": tokens}

    def _submit(self, payload, thread) -> dict:
        fut = concurrent.futures.Future()
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (fut, payload, thread))
        return fut.result(timeout=600)

    def send(self, text: str, thread: str) -> dict:
        return self._submit({"messages": [("user", text)]}, thread)

    def resume(self, decisions: list[dict], thread: str) -> dict:
        return self._submit(Command(resume={"decisions": decisions}), thread)


# ---------------------------------------------------------------- page

@st.cache_resource(show_spinner="Starting the agent and connecting to Salesforce...")
def get_runtime() -> AgentRuntime:
    return AgentRuntime()


def render_events(events: list[dict]) -> None:
    calls = [e for e in events if e["kind"] in ("call", "result")]
    if calls:
        with st.expander(f"Agent steps ({sum(e['kind'] == 'call' for e in calls)} tool calls)", expanded=False):
            for e in calls:
                if e["kind"] == "call":
                    st.markdown(f"→ **{TOOL_LABELS.get(e['name'], e['name'])}** `{e['name']}`")
                    st.code(json.dumps(e["args"], indent=1), language="json")
                else:
                    st.markdown(f"← `{e['name']}`: {'ok' if e['ok'] else 'error'} ({e['chars']} chars)"
                                + (f"  \n_{e['error']}_" if e.get("error") else ""))
    for i, e in enumerate(events):
        if e["kind"] == "answer":
            st.markdown(e["text"])
        elif e["kind"] == "document":
            show_document(e, i)


def show_document(e: dict, i: int) -> None:
    path = Path(e["file"])
    if not path.exists():
        st.caption(f"(document no longer on disk: {path.name})")
        return
    html = path.read_text(encoding="utf-8")
    key = f"{path.name}-{i}-{len(st.session_state.history)}"
    c1, c2 = st.columns(2)
    # Streamlit runs on your own machine, so it can open the file in your browser.
    if c1.button("📄 Open quote document", key="open-" + key, type="primary", use_container_width=True):
        webbrowser.open(path.resolve().as_uri())
    c2.download_button("⬇ Download", html, file_name=path.name, mime="text/html",
                       key="dl-" + key, use_container_width=True)
    with st.expander("Preview", expanded=True):
        st.components.v1.html(html, height=900, scrolling=True)


def add_result(result: dict) -> None:
    st.session_state.history.append({"role": "assistant", "events": result["events"]})
    st.session_state.pending = result["interrupt"]
    st.session_state.tokens += result["tokens"]


st.set_page_config(page_title="Revenue Cloud Agent", page_icon="💬", layout="wide")
for key, default in (("thread", str(uuid.uuid4())), ("history", []), ("pending", None), ("tokens", 0)):
    st.session_state.setdefault(key, default)

try:
    runtime = get_runtime()
except Exception as e:  # noqa: BLE001
    st.error(f"Could not start the agent: {e}")
    st.stop()

with st.sidebar:
    st.header("Revenue Cloud Agent")
    st.caption(f"Model: {chat.MODEL}")
    st.caption(f"Org alias: {os.getenv('SF_ORG_ALIAS', 'rcadev')}")
    st.metric("Tokens this conversation", f"{st.session_state.tokens:,}")
    with st.expander("Tools"):
        for name in runtime.tool_names:
            st.markdown(f"- `{name}`" + ("  *(needs approval)*" if name in chat.WRITE_TOOLS else ""))
    if st.button("New conversation", use_container_width=True):
        st.session_state.update(thread=str(uuid.uuid4()), history=[], pending=None, tokens=0)
        st.rerun()
    st.divider()
    st.caption("Try: *On the Tablets Purchase quote, why are the Tablet Keyboard and the second WiFi Tablet priced at 0?*")

st.title("Revenue Cloud quoting agent")
st.caption("Ask about quotes and prices, or ask it to create quotes, add products, change quantities and apply discounts. "
           "Every change waits for your approval.")

for msg in st.session_state.history:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["text"])
        else:
            render_events(msg["events"])

pending = st.session_state.pending
if pending:
    with st.chat_message("assistant"):
        st.warning("The agent wants to change Salesforce. Approve or reject:")
        requests = pending.get("action_requests", [])
        for i, req in enumerate(requests):
            st.markdown(f"**{TOOL_LABELS.get(req.get('name'), req.get('name'))}** (`{req.get('name')}`)")
            st.code(json.dumps(req.get("args", {}), indent=1), language="json")
        reason = st.text_input("Reason, if rejecting", key="reject_reason")
        c1, c2 = st.columns(2)
        decision = None
        if c1.button("✅ Approve", type="primary", use_container_width=True):
            decision = [{"type": "approve"} for _ in requests]
        if c2.button("❌ Reject", use_container_width=True):
            decision = [{"type": "reject", "message": reason or "The user rejected this change."} for _ in requests]
        if decision:
            with st.spinner("Working..."):
                try:
                    add_result(runtime.resume(decision, st.session_state.thread))
                except Exception as e:  # noqa: BLE001
                    st.session_state.pending = None
                    st.session_state.history.append({"role": "assistant", "events": [{"kind": "answer", "text": f"Error: {e}"}]})
            st.rerun()

prompt = st.chat_input("Ask the agent...", disabled=bool(pending))
if prompt:
    st.session_state.history.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Thinking and calling Salesforce..."):
            try:
                add_result(runtime.send(prompt, st.session_state.thread))
            except Exception as e:  # noqa: BLE001
                st.session_state.history.append({"role": "assistant", "events": [{"kind": "answer", "text": f"Error: {e}"}]})
    st.rerun()
