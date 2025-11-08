# working stable actuator
import os
import json
import re
import aiohttp
import httpx
import time
from fastapi import FastAPI, Request, BackgroundTasks
from dotenv import load_dotenv
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient

# ================================================================
# 🌐 Azure Slack → GPT (AssistantAgent) → MCP Actuator
# ================================================================

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT")
AZURE_API_VERSION = os.getenv("MODEL_API_VERSION")
AZURE_DEPLOYMENT = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4o")
MCP_URL = "https://alpacashit-h3edbzd5hgabh6hs.westeurope-01.azurewebsites.net/mcp"
MCP_VERSION = "2024-11-05"

app = FastAPI(title="Autonomous Azure Slack-MCP Actuator", version="3.7")

# ================================================================
# 🧭 Logging Helper
# ================================================================
def log_big(title: str):
    print(f"\n\n🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷")
    print(f"🔹 {time.strftime('%H:%M:%S')} | {title.upper()}")
    print(f"🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷🔷\n", flush=True)

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ================================================================
# ⚙️ MCP HTTP Client
# ================================================================
class MCPHTTPClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session_id = None
        self.http = None

    async def __aenter__(self):
        self.http = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.http:
            await self.http.close()

    async def _post(self, payload: dict):
        log_big(f"MCP POST → {payload.get('method', 'unknown')}")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-protocol-version": MCP_VERSION,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id

        async with self.http.post(self.base_url, json=payload, headers=headers, timeout=45) as resp:
            text = await resp.text()
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
            log(f"📨 MCP STATUS {resp.status}")
            if resp.status >= 400:
                raise RuntimeError(f"MCP HTTP {resp.status}: {text[:300]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                lines = [json.loads(l[len('data:'):]) for l in text.splitlines() if l.startswith("data:")]
                return lines[-1] if lines else text

    async def initialize(self):
        log_big("MCP INITIALIZE")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "slack-azure-actuator", "version": "3.7"},
            },
        }
        return await self._post(payload)

    async def list_tools(self):
        log_big("MCP LIST TOOLS")
        payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        result = await self._post(payload)
        tools = result.get("result", {}).get("tools", [])
        log(f"🧰 TOOLS FOUND: {[t['name'] for t in tools]}")
        return tools

    async def call_tool(self, tool_name: str, args: dict):
        log_big(f"MCP CALL TOOL → {tool_name}")
        payload = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        }
        log(f"📦 ARGS: {json.dumps(args, indent=2)}")
        result = await self._post(payload)
        log(f"✅ MCP RESPONSE RECEIVED for {tool_name}")
        return result


# ================================================================
# 💹 Stock Price Fetch via MCP
# ================================================================
async def get_stock_price(symbol: str) -> float:
    """Fetch current stock price via MCP get_stock_quote."""
    log_big(f"FETCH STOCK PRICE FOR {symbol}")
    try:
        async with MCPHTTPClient(MCP_URL) as mcp:
            await mcp.initialize()
            result = await mcp.call_tool("get_stock_quote", {"symbol": symbol})
            text = json.dumps(result)
            match = re.search(r"(Ask|Bid|Last|Price)\s*[:=]\s*(\d+(\.\d+)?)", text)
            if match:
                price = float(match.group(2))
                log(f"💰 Parsed {symbol} price ≈ ${price}")
                return price
            log(f"⚠️ No numeric price found in MCP quote response for {symbol}.")
            return 0.0
    except Exception as e:
        log(f"❌ get_stock_price() failed: {e}")
        return 0.0


# ================================================================
# 📣 Slack Response Utilities
# ================================================================
async def post_to_slack(channel: str, text: str, thread_ts: str = None):
    """Post a message to Slack (optionally in a thread)."""
    try:
        headers = {
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        async with httpx.AsyncClient() as client:
            resp = await client.post("https://slack.com/api/chat.postMessage", json=payload, headers=headers)
            data = resp.json()
            if not data.get("ok"):
                log(f"⚠️ Slack API error: {data}")
            else:
                log(f"✅ Posted to Slack: {text[:80]}...")
    except Exception as e:
        log(f"❌ post_to_slack() failed: {e}")


# ================================================================
# 💬 Friendly GPT Response Generator
# ================================================================
async def generate_gpt_reply(context: str) -> str:
    """Use GPT to generate a friendly conversational Slack reply."""
    model_client = AzureOpenAIChatCompletionClient(
        azure_endpoint=AZURE_ENDPOINT,
        azure_deployment=AZURE_DEPLOYMENT,
        api_version=AZURE_API_VERSION,
        model="gpt-4o-2024-11-20",
    )
    system_message = (
        "You are a friendly financial assistant replying to a Slack user. "
        "Summarize what just happened in a short, natural, friendly way, with emojis if appropriate."
    )
    agent = AssistantAgent(
        name="SlackFriendlyResponder",
        model_client=model_client,
        system_message=system_message,
    )
    user_msg = TextMessage(content=context, source="user")
    result = await agent.run(task=user_msg)
    if hasattr(result, "messages") and result.messages:
        return result.messages[-1].content
    return "✅ Trade executed successfully!"


# ================================================================
# 🧠 GPT Reasoning — via AssistantAgent
# ================================================================
async def analyze_intent_with_gpt(message: str, mcp_tools: list):
    log_big("GPT INTENT ANALYSIS — VIA ASSISTANT AGENT")
    tools_summary = "\n".join(
        [f"• {t['name']} —\n    {t.get('description', '').strip()}\n\n  Args: {t.get('inputSchema', {}).get('properties', {})}"
         for t in mcp_tools]
    )

    system_message = f"""
You are a financial trading assistant connected to an Alpaca MCP trading server.
You can handle both **queries** (like balance, positions) and **actions** (buy, sell, close).
Select exactly one MCP tool that fits the user’s intent.

Rules:
- For trades with '$' → notional; with 'shares' → quantity.
- For queries → pick relevant tool (`get_account_info`, etc.)
- For trades → always use `place_stock_order`.
- Return JSON only.
- Tool name must match exactly from the list below.

### Available Tools:
{tools_summary}
"""

    model_client = AzureOpenAIChatCompletionClient(
        azure_endpoint=AZURE_ENDPOINT,
        azure_deployment=AZURE_DEPLOYMENT,
        api_version=AZURE_API_VERSION,
        model="gpt-4o-2024-11-20",
    )
    decision_agent = AssistantAgent(
        name="SlackDecisionAgent",
        model_client=model_client,
        system_message=system_message,
    )
    user_input = TextMessage(content=message, source="user")

    try:
        result = await decision_agent.run(task=user_input)
        final_msg = result.messages[-1].content if hasattr(result, "messages") and result.messages else str(result)
        json_start = final_msg.find("{")
        json_end = final_msg.rfind("}")
        if json_start >= 0 and json_end >= 0:
            parsed = json.loads(final_msg[json_start:json_end + 1])
            log(f"✅ Parsed JSON decision: {json.dumps(parsed, indent=2)}")
            return parsed
        return {"tool": "none", "args": {}}
    except Exception as e:
        log(f"❌ AssistantAgent decision failed: {e}")
        return {"tool": "none", "args": {}}


# ================================================================
# 💬 Slack Utilities
# ================================================================
async def fetch_parent_message(channel: str, thread_ts: str):
    log_big("FETCH PARENT MESSAGE")
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    params = {"channel": channel, "ts": thread_ts, "limit": 1}
    async with httpx.AsyncClient() as client:
        r = await client.get("https://slack.com/api/conversations.replies", params=params, headers=headers)
        data = r.json()
        if data.get("ok") and data.get("messages"):
            parent = data["messages"][0].get("text", "")
            log(f"🪶 PARENT MESSAGE: {parent[:120]}...")
            return parent
        log("⚠️ Failed to fetch parent message.")
        return ""


# ================================================================
# 🚀 Slack Event Webhook (loop-safe)
# ================================================================
@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    log_big("INCOMING SLACK EVENT")
    body = await request.json()
    log(json.dumps(body, indent=2))

    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    event = body.get("event", {})
    if not event:
        log("⚠️ No event found in payload.")
        return {"ok": True}

    bot_user_id = body.get("authorizations", [{}])[0].get("user_id")
    event_user = event.get("user")
    bot_id = event.get("bot_id")

    if bot_id or (event_user and event_user == bot_user_id):
        log_big("🚫 Ignoring bot/self event to prevent infinite loop.")
        log(f"🔸 bot_id={bot_id} | user={event_user} | bot_user_id={bot_user_id}")
        return {"ok": True}

    log(f"✅ Proceeding with user message from {event_user}")
    background_tasks.add_task(
        process_slack_reply,
        event_user,
        event.get("text"),
        event.get("channel"),
        event.get("thread_ts") or event.get("ts"),
    )
    return {"ok": True}


# ================================================================
# 🧩 Core Logic — End-to-End Workflow (shows real MCP output)
# ================================================================
async def process_slack_reply(user, reply_text, channel, thread_ts):
    log_big("PROCESSING SLACK REPLY EVENT")
    parent_text = await fetch_parent_message(channel, thread_ts)
    full_message = f"Parent: {parent_text}\nUser reply: {reply_text}"

    async with MCPHTTPClient(MCP_URL) as mcp:
        await mcp.initialize()
        tools = await mcp.list_tools()
        decision = await analyze_intent_with_gpt(full_message, tools)
        log(f"🎯 GPT DECISION: {json.dumps(decision, indent=2)}")

        tool = decision.get("tool")
        args = decision.get("args", {})

        if tool == "place_stock_order":
            symbol = args.get("symbol")
            notional = args.get("notional")
            if notional and symbol:
                price = await get_stock_price(symbol)
                args["quantity"] = round(notional / price, 3) if price > 0 else 1.0
            args.pop("notional", None)
            if not args.get("quantity"):
                args["quantity"] = 1.0

        if tool and tool.lower() != "none":
            log_big(f"EXECUTING MCP TOOL → {tool}")
            result = await mcp.call_tool(tool, args)
            log(f"📈 MCP RESULT: {json.dumps(result, indent=2)}")

            # 🧠 Extract readable content from MCP response
            mcp_text = ""
            try:
                mcp_text = result.get("result", {}).get("structuredContent", {}).get("result", "")
                if not mcp_text:
                    contents = result.get("result", {}).get("content", [])
                    if contents and isinstance(contents, list):
                        mcp_text = contents[0].get("text", "")
            except Exception as e:
                log(f"⚠️ Failed to parse MCP result text: {e}")

            # 📝 Compose readable summary
            summary = (
                f"✅ *Executed:* `{tool}`\n\n"
                f"📊 *Result:*\n```\n{mcp_text.strip()[:1500]}\n```"
            )

            # 💬 Friendly GPT wrap-up
            friendly = await generate_gpt_reply(summary)
            final_msg = f"{summary}\n\n{friendly}"

            await post_to_slack(channel, final_msg, thread_ts)
        else:
            await post_to_slack(channel, "🤔 No actionable command detected.", thread_ts)


# ================================================================
# 🩺 Health Check
# ================================================================
@app.get("/")
async def root():
    return {"status": "ok", "message": "Autonomous Slack→GPT(Agent)→MCP actuator active"}
