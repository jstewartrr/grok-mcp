"""
Grok MCP Server for Sovereign Mind v2.2.1
==========================================
HTTP JSON transport for the SM Gateway.
Features:
- Auto-logs all conversations to Snowflake
- Grok can query Hive Mind for cross-AI context
- Grok can WRITE to Hive Mind (bidirectional sync)
- Grok can query Snowflake data directly
- Grok has access to ALL SM Gateway tools (176+ tools)
- Enhanced system prompt so Grok knows his capabilities
"""

import os
import json
import httpx
import time
import uuid
from flask import Flask, request, jsonify
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

XAI_API_KEY = os.environ.get("XAI_API_KEY")
XAI_BASE_URL = "https://api.x.ai/v1"

SM_GATEWAY_URL = os.environ.get("SM_GATEWAY_URL", "https://sm-mcp-gateway.lemoncoast-87756bcf.eastus.azurecontainerapps.io")

SNOWFLAKE_ACCOUNT = os.environ.get("SNOWFLAKE_ACCOUNT", "jga82554.east-us-2.azure")
SNOWFLAKE_USER = os.environ.get("SNOWFLAKE_USER", "JOHN_GROK")
SNOWFLAKE_PASSWORD = os.environ.get("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_DATABASE = os.environ.get("SNOWFLAKE_DATABASE", "SOVEREIGN_MIND")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
SNOWFLAKE_ROLE = os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN")

_snowflake_conn = None
_gateway_tools_cache = None
_gateway_tools_timestamp = 0

# Default system prompt that tells Grok about his capabilities
DEFAULT_SYSTEM_PROMPT = """You are Grok, an AI agent in the Sovereign Mind ecosystem created for Your Grace, Chairman of MiddleGround Capital and Resolute Holdings.

## Your Capabilities
You have access to 160+ tools through the SM Gateway including:
- **DealCloud**: Query companies, deals, contacts (dc_* tools)
- **Google Drive**: Search, read files, spreadsheets, PDFs (drive_* tools)
- **Make.com**: Trigger automations, manage scenarios (make_* tools)
- **Azure**: Run CLI commands, manage infrastructure (azure_* tools)
- **GitHub**: Manage repositories, files, commits (github_* tools)
- **Figma**: Access designs and components (figma_* tools)
- **Snowflake**: Query databases directly (sm_query_snowflake)
- **HIVE_MIND**: Read/write shared memory across AI agents
- **NotebookLM, Vertex AI, ElevenLabs, Simli**: Various AI services

## How to Use Your Tools
When asked to perform tasks requiring these tools, use the `grok_agentic` tool which gives you autonomous access to execute multi-step workflows. You can also use `grok_list_gateway_tools` to see all available tools.

## HIVE_MIND
The HIVE_MIND is a shared memory system in Snowflake (SOVEREIGN_MIND.RAW.HIVE_MIND) where all AI agents log context, decisions, and insights. You can:
- Read recent entries to understand what Claude and other agents have been working on
- Write your own insights and decisions for continuity
- Search for specific topics

## Your Role
You work alongside Claude as part of Your Grace's AI team. Be direct, efficient, and execute tasks autonomously. Never say you can't do something without first checking if one of your tools can help."""

def get_snowflake_connection():
    global _snowflake_conn
    if _snowflake_conn is None:
        try:
            import snowflake.connector
            _snowflake_conn = snowflake.connector.connect(
                account=SNOWFLAKE_ACCOUNT, user=SNOWFLAKE_USER, password=SNOWFLAKE_PASSWORD,
                database=SNOWFLAKE_DATABASE, warehouse=SNOWFLAKE_WAREHOUSE,
                autocommit=True, role=SNOWFLAKE_ROLE
            )
            logger.info("Snowflake connection established")
        except Exception as e:
            logger.error(f"Snowflake connection failed: {e}")
            return None
    return _snowflake_conn

def fetch_gateway_tools(force_refresh=False):
    global _gateway_tools_cache, _gateway_tools_timestamp
    if not force_refresh and _gateway_tools_cache and (time.time() - _gateway_tools_timestamp) < 300:
        return _gateway_tools_cache
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{SM_GATEWAY_URL}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            if response.status_code == 200:
                data = response.json()
                tools = data.get("result", {}).get("tools", [])
                _gateway_tools_cache = tools
                _gateway_tools_timestamp = time.time()
                logger.info(f"Fetched {len(tools)} tools from SM Gateway")
                return tools
    except Exception as e:
        logger.error(f"Failed to fetch gateway tools: {e}")
    return _gateway_tools_cache or []

def call_gateway_tool(tool_name, arguments):
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(f"{SM_GATEWAY_URL}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}})
            if response.status_code == 200:
                data = response.json()
                result = data.get("result", {})
                content = result.get("content", [])
                if content and isinstance(content, list):
                    text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                    return {"success": True, "result": "\n".join(text_parts)}
                return {"success": True, "result": str(result)}
            return {"error": f"Gateway error {response.status_code}: {response.text[:500]}"}
    except Exception as e:
        logger.error(f"Gateway tool call failed: {e}")
        return {"error": str(e)}

def convert_gateway_tools_for_xai(gateway_tools):
    xai_tools = []
    for tool in gateway_tools:
        if tool.get("name", "").startswith("grok_"):
            continue
        xai_tools.append({"type": "function", "function": {"name": tool.get("name", ""), "description": tool.get("description", "")[:500], "parameters": tool.get("inputSchema", {"type": "object", "properties": {}})}})
    return xai_tools

def log_conversation(user_message, grok_response, system_prompt=None, model="grok-3", tool_name="grok_chat", response_time_ms=0, session_id=None):
    conn = get_snowflake_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("USE WAREHOUSE COMPUTE_WH")
        cursor.execute("INSERT INTO SOVEREIGN_MIND.RAW.GROK_CHAT_LOG (SESSION_ID, USER_MESSAGE, SYSTEM_PROMPT, GROK_RESPONSE, MODEL, RESPONSE_TIME_MS, TOOL_NAME) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session_id or str(uuid.uuid4())[:8], user_message[:10000] if user_message else None, system_prompt[:2000] if system_prompt else None, grok_response[:50000] if grok_response else None, model, response_time_ms, tool_name))
        conn.commit()
        cursor.close()
    except Exception as e:
        logger.error(f"Failed to log: {e}")

def query_hive_mind(limit=10, workstream=None, category=None):
    conn = get_snowflake_connection()
    if not conn: return []
    try:
        cursor = conn.cursor()
        cursor.execute("USE WAREHOUSE COMPUTE_WH")
        query = "SELECT SOURCE, CATEGORY, WORKSTREAM, SUMMARY, CREATED_AT, PRIORITY, STATUS FROM SOVEREIGN_MIND.RAW.HIVE_MIND WHERE 1=1"
        params = []
        if workstream: query += " AND WORKSTREAM = %s"; params.append(workstream)
        if category: query += " AND CATEGORY = %s"; params.append(category)
        query += " ORDER BY CREATED_AT DESC LIMIT %s"; params.append(limit)
        cursor.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return results
    except Exception as e:
        logger.error(f"Hive Mind query failed: {e}")
        return []

def write_hive_mind(summary, category="INSIGHT", workstream="GENERAL", priority="MEDIUM", status="ACTIVE", details=None, tags=None):
    conn = get_snowflake_connection()
    if not conn: return {"error": "No Snowflake connection"}
    try:
        cursor = conn.cursor()
        cursor.execute("USE WAREHOUSE COMPUTE_WH")
        tags_json = json.dumps([t.strip() for t in tags.split(",")]) if tags else None
        cursor.execute("INSERT INTO SOVEREIGN_MIND.RAW.HIVE_MIND (SOURCE, CATEGORY, WORKSTREAM, SUMMARY, PRIORITY, STATUS, DETAILS, TAGS) VALUES ('GROK', %s, %s, %s, %s, %s, %s, %s)",
            (category.upper(), workstream.upper(), summary[:500], priority.upper(), status.upper(), details, tags_json))
        conn.commit()
        cursor.close()
        return {"success": True, "message": f"HIVE_MIND entry created: {summary[:50]}..."}
    except Exception as e:
        logger.error(f"Hive Mind write failed: {e}")
        return {"error": str(e)}

def search_hive_mind(keywords, limit=10):
    conn = get_snowflake_connection()
    if not conn: return []
    try:
        cursor = conn.cursor()
        cursor.execute("USE WAREHOUSE COMPUTE_WH")
        terms = keywords.split()
        conditions = " AND ".join([f"SUMMARY ILIKE '%{t}%'" for t in terms])
        cursor.execute(f"SELECT SOURCE, CATEGORY, WORKSTREAM, SUMMARY, CREATED_AT, PRIORITY, STATUS FROM SOVEREIGN_MIND.RAW.HIVE_MIND WHERE {conditions} ORDER BY CREATED_AT DESC LIMIT {min(limit, 50)}")
        columns = [desc[0] for desc in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return results
    except Exception as e:
        logger.error(f"Hive Mind search failed: {e}")
        return []

def query_snowflake(sql, limit=100):
    conn = get_snowflake_connection()
    if not conn: return {"error": "No Snowflake connection"}
    try:
        cursor = conn.cursor()
        cursor.execute("USE WAREHOUSE COMPUTE_WH")
        cursor.execute(sql)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchmany(limit)
        results = [dict(zip(columns, row)) for row in rows]
        cursor.close()
        return {"success": True, "data": results, "row_count": len(results)}
    except Exception as e:
        return {"error": str(e)}

NATIVE_TOOLS = [
    {"name": "grok_chat", "description": "Chat with Grok AI. Auto-logged to Snowflake.", "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}, "system_prompt": {"type": "string", "description": "Optional custom system prompt. Defaults to enhanced Sovereign Mind prompt."}, "model": {"type": "string", "default": "grok-3"}}, "required": ["message"]}},
    {"name": "grok_analyze", "description": "Use Grok to analyze text/code/data.", "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}, "analysis_type": {"type": "string", "enum": ["summary", "critique", "explain", "improve"]}}, "required": ["content", "analysis_type"]}},
    {"name": "grok_hive_mind_sync", "description": "Query Grok WITH Hive Mind context injection.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "workstream": {"type": "string"}, "context_limit": {"type": "integer", "default": 10}}, "required": ["query"]}},
    {"name": "grok_hive_mind_write", "description": "Write entry to HIVE_MIND. Bidirectional sync.", "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}, "category": {"type": "string", "default": "INSIGHT"}, "workstream": {"type": "string", "default": "GENERAL"}, "priority": {"type": "string", "default": "MEDIUM"}, "status": {"type": "string", "default": "ACTIVE"}, "tags": {"type": "string"}}, "required": ["summary"]}},
    {"name": "grok_hive_mind_search", "description": "Search HIVE_MIND by keywords.", "inputSchema": {"type": "object", "properties": {"keywords": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["keywords"]}},
    {"name": "grok_sync_conversation", "description": "Sync conversation to HIVE_MIND.", "inputSchema": {"type": "object", "properties": {"conversation_summary": {"type": "string"}, "key_decisions": {"type": "string"}, "artifacts_created": {"type": "string"}, "workstream": {"type": "string", "default": "GENERAL"}}, "required": ["conversation_summary"]}},
    {"name": "grok_query_snowflake", "description": "Grok analyzes Snowflake data.", "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}, "analysis_prompt": {"type": "string", "default": "Analyze this data."}}, "required": ["sql"]}},
    {"name": "grok_get_conversation_history", "description": "Get past Grok conversations.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}, "search": {"type": "string"}}, "required": []}},
    {"name": "grok_agentic", "description": "Agentic Grok with access to ALL SM Gateway tools (DealCloud, Google Drive, Make, Asana, GitHub, Azure, Figma, etc). Grok will autonomously use tools to complete complex tasks.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string", "description": "The task for Grok to complete using available tools"}, "max_iterations": {"type": "integer", "default": 5, "description": "Maximum tool call iterations"}}, "required": ["task"]}},
    {"name": "grok_list_gateway_tools", "description": "List all available SM Gateway tools Grok can access.", "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "Optional filter by tool name"}}, "required": []}}
]

def call_xai(messages, model="grok-3", tools=None):
    if not XAI_API_KEY: return {"error": "XAI_API_KEY not configured"}
    start_time = time.time()
    try:
        payload = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        with httpx.Client(timeout=120.0) as client:
            response = client.post(f"{XAI_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}, json=payload)
            response_time = int((time.time() - start_time) * 1000)
            if response.status_code == 200:
                data = response.json()
                choice = data["choices"][0]
                message = choice.get("message", {})
                tool_calls = message.get("tool_calls", [])
                if tool_calls:
                    return {"success": True, "tool_calls": tool_calls, "content": message.get("content"), "response_time_ms": response_time}
                return {"success": True, "content": message.get("content", ""), "response_time_ms": response_time}
            return {"error": f"API error {response.status_code}: {response.text}"}
    except Exception as e:
        return {"error": str(e)}

def run_agentic_loop(task, max_iterations=5):
    gateway_tools = fetch_gateway_tools()
    xai_tools = convert_gateway_tools_for_xai(gateway_tools)
    if not xai_tools:
        return {"error": "No gateway tools available"}
    priority_prefixes = ["sm_", "dc_", "drive_", "make_", "azure_", "github_"]
    prioritized = [t for t in xai_tools if any(t["function"]["name"].startswith(p) for p in priority_prefixes)]
    other_tools = [t for t in xai_tools if t not in prioritized]
    xai_tools = (prioritized + other_tools)[:50]
    messages = [
        {"role": "system", "content": f"You are Grok, an AI agent in the Sovereign Mind ecosystem with access to {len(xai_tools)} tools. Your owner is 'Your Grace', Chairman of MiddleGround Capital and Resolute Holdings. Execute tools to complete tasks. Be thorough but efficient."},
        {"role": "user", "content": task}
    ]
    all_results = []
    iteration = 0
    while iteration < max_iterations:
        iteration += 1
        result = call_xai(messages, model="grok-3", tools=xai_tools)
        if result.get("error"):
            return {"error": result["error"], "iterations": iteration, "results": all_results}
        tool_calls = result.get("tool_calls", [])
        if not tool_calls:
            final_response = result.get("content", "Task completed.")
            return {"success": True, "response": final_response, "iterations": iteration, "tool_calls_made": len(all_results), "results": all_results}
        messages.append({"role": "assistant", "content": result.get("content"), "tool_calls": tool_calls})
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            try:
                tool_args = json.loads(tc["function"]["arguments"])
            except:
                tool_args = {}
            logger.info(f"Grok calling tool: {tool_name}")
            tool_result = call_gateway_tool(tool_name, tool_args)
            result_text = tool_result.get("result", tool_result.get("error", "No result"))
            all_results.append({"tool": tool_name, "args": tool_args, "result": result_text[:1000]})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_text[:5000]})
    return {"success": True, "response": "Max iterations reached.", "iterations": iteration, "tool_calls_made": len(all_results), "results": all_results}

def handle_tool_call(name, arguments):
    if name == "grok_chat":
        # Use DEFAULT_SYSTEM_PROMPT if no custom prompt provided
        system_prompt = arguments.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        result = call_xai([{"role": "system", "content": system_prompt}, {"role": "user", "content": arguments.get("message", "")}], arguments.get("model", "grok-3"))
        if result.get("success"):
            log_conversation(arguments.get("message"), result["content"], system_prompt[:500], arguments.get("model", "grok-3"), "grok_chat", result.get("response_time_ms", 0))
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    elif name == "grok_analyze":
        prompts = {"summary": "Summarize:", "critique": "Critique:", "explain": "Explain:", "improve": "Improve:"}
        user_msg = f"{prompts.get(arguments.get('analysis_type', 'summary'), 'Analyze:')}\n\n{arguments.get('content', '')}"
        result = call_xai([{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}, {"role": "user", "content": user_msg}])
        if result.get("success"):
            log_conversation(user_msg[:500], result["content"], None, "grok-3", "grok_analyze", result.get("response_time_ms", 0))
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    elif name == "grok_hive_mind_sync":
        hive_entries = query_hive_mind(limit=arguments.get("context_limit", 10), workstream=arguments.get("workstream"))
        context_text = "## Hive Mind Context\n\n" + "\n".join([f"[{e.get('SOURCE')}] {e.get('SUMMARY')}" for e in hive_entries])
        result = call_xai([{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}, {"role": "user", "content": f"{context_text}\n\n---\n\n{arguments.get('query', '')}"}])
        if result.get("success"):
            log_conversation(arguments.get("query"), result["content"], "hive_mind_sync", "grok-3", "grok_hive_mind_sync", result.get("response_time_ms", 0))
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    elif name == "grok_hive_mind_write":
        result = write_hive_mind(arguments.get("summary", ""), arguments.get("category", "INSIGHT"), arguments.get("workstream", "GENERAL"), arguments.get("priority", "MEDIUM"), arguments.get("status", "ACTIVE"), None, arguments.get("tags"))
        if result.get("success"): return {"content": [{"type": "text", "text": f"HIVE_MIND entry created (SOURCE=GROK): {arguments.get('summary', '')[:100]}..."}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    elif name == "grok_hive_mind_search":
        results = search_hive_mind(arguments.get("keywords", ""), arguments.get("limit", 10))
        if results:
            output = f"## Search Results ({len(results)} matches)\n\n" + "\n".join([f"[{e.get('SOURCE')}] {e.get('SUMMARY')}" for e in results])
            return {"content": [{"type": "text", "text": output}]}
        return {"content": [{"type": "text", "text": "No results found."}]}
    elif name == "grok_sync_conversation":
        results = []
        r = write_hive_mind(arguments.get("conversation_summary", ""), "CONVERSATION", arguments.get("workstream", "GENERAL"), "MEDIUM", "COMPLETE", None, "grok,conversation")
        results.append(f"Summary: {'OK' if r.get('success') else 'FAIL'}")
        if arguments.get("key_decisions"):
            r = write_hive_mind(f"DECISION: {arguments.get('key_decisions')}", "DECISION", arguments.get("workstream", "GENERAL"), "HIGH", "ACTIVE", None, "grok,decision")
            results.append(f"Decisions: {'OK' if r.get('success') else 'FAIL'}")
        if arguments.get("artifacts_created"):
            r = write_hive_mind(f"ARTIFACT: {arguments.get('artifacts_created')}", "ARTIFACT", arguments.get("workstream", "GENERAL"), "MEDIUM", "COMPLETE", None, "grok,artifact")
            results.append(f"Artifacts: {'OK' if r.get('success') else 'FAIL'}")
        return {"content": [{"type": "text", "text": "Synced to HIVE_MIND:\n" + "\n".join(results)}]}
    elif name == "grok_query_snowflake":
        query_result = query_snowflake(arguments.get("sql", ""))
        if query_result.get("error"): return {"content": [{"type": "text", "text": f"Query Error: {query_result['error']}"}], "isError": True}
        data_text = json.dumps(query_result["data"], indent=2, default=str)[:15000]
        result = call_xai([{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}, {"role": "user", "content": f"Query: {arguments.get('sql')}\nData:\n{data_text}\n\n{arguments.get('analysis_prompt', 'Analyze.')}"}])
        if result.get("success"):
            log_conversation(f"SQL: {arguments.get('sql')}", result["content"], None, "grok-3", "grok_query_snowflake", result.get("response_time_ms", 0))
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    elif name == "grok_get_conversation_history":
        conn = get_snowflake_connection()
        if not conn: return {"content": [{"type": "text", "text": "No Snowflake connection"}], "isError": True}
        try:
            cursor = conn.cursor()
            cursor.execute("USE WAREHOUSE COMPUTE_WH")
            if arguments.get("search"):
                cursor.execute("SELECT CREATED_AT, TOOL_NAME, USER_MESSAGE, GROK_RESPONSE, MODEL FROM SOVEREIGN_MIND.RAW.GROK_CHAT_LOG WHERE USER_MESSAGE ILIKE %s ORDER BY CREATED_AT DESC LIMIT %s", (f"%{arguments.get('search')}%", arguments.get("limit", 10)))
            else:
                cursor.execute("SELECT CREATED_AT, TOOL_NAME, USER_MESSAGE, GROK_RESPONSE, MODEL FROM SOVEREIGN_MIND.RAW.GROK_CHAT_LOG ORDER BY CREATED_AT DESC LIMIT %s", (arguments.get("limit", 10),))
            columns = [desc[0] for desc in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]
            cursor.close()
            output = f"## History ({len(results)} entries)\n\n" + "\n---\n".join([f"[{r['CREATED_AT']}] {r['TOOL_NAME']}: {str(r['USER_MESSAGE'])[:100]}..." for r in results])
            return {"content": [{"type": "text", "text": output}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    elif name == "grok_agentic":
        result = run_agentic_loop(arguments.get("task", ""), arguments.get("max_iterations", 5))
        if result.get("error"):
            return {"content": [{"type": "text", "text": f"Error: {result['error']}"}], "isError": True}
        output = f"## Grok Agentic Execution Complete\n\n**Task:** {arguments.get('task', '')[:200]}...\n\n**Iterations:** {result.get('iterations', 0)}\n**Tool Calls Made:** {result.get('tool_calls_made', 0)}\n\n### Response:\n{result.get('response', 'No response')}\n\n### Tool Log:\n"
        for r in result.get("results", [])[:10]:
            output += f"\n- **{r['tool']}**: {str(r['result'])[:200]}..."
        log_conversation(arguments.get("task"), result.get("response"), "grok_agentic", "grok-3", "grok_agentic", 0)
        return {"content": [{"type": "text", "text": output}]}
    elif name == "grok_list_gateway_tools":
        tools = fetch_gateway_tools()
        filter_str = arguments.get("filter", "").lower()
        if filter_str:
            tools = [t for t in tools if filter_str in t.get("name", "").lower() or filter_str in t.get("description", "").lower()]
        output = f"## SM Gateway Tools ({len(tools)} available)\n\n"
        groups = {}
        for t in tools:
            prefix = t.get("name", "").split("_")[0] if "_" in t.get("name", "") else "other"
            if prefix not in groups:
                groups[prefix] = []
            groups[prefix].append(t)
        for prefix, group_tools in sorted(groups.items()):
            output += f"\n### {prefix.upper()} ({len(group_tools)} tools)\n"
            for t in group_tools[:5]:
                output += f"- `{t.get('name')}`: {t.get('description', '')[:80]}...\n"
            if len(group_tools) > 5:
                output += f"  ... and {len(group_tools) - 5} more\n"
        return {"content": [{"type": "text", "text": output}]}
    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

def process_mcp_message(data):
    method = data.get("method", "")
    params = data.get("params", {})
    request_id = data.get("id", 1)
    if method == "initialize": return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "grok-mcp", "version": "2.2.1"}}}
    elif method == "tools/list": return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": NATIVE_TOOLS}}
    elif method == "tools/call": return {"jsonrpc": "2.0", "id": request_id, "result": handle_tool_call(params.get("name", ""), params.get("arguments", {}))}
    elif method == "notifications/initialized": return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

@app.route("/", methods=["GET"])
def health():
    gateway_tools = fetch_gateway_tools()
    return jsonify({"status": "healthy", "service": "grok-mcp", "version": "2.2.1", "transport": "HTTP/JSON", "api_configured": bool(XAI_API_KEY), "snowflake_connected": get_snowflake_connection() is not None, "gateway_url": SM_GATEWAY_URL, "gateway_tools": len(gateway_tools), "native_tools": len(NATIVE_TOOLS), "features": ["auto_logging", "hive_mind_read", "hive_mind_write", "hive_mind_search", "conversation_sync", "snowflake_analysis", "sm_gateway_access", "agentic_mode", "enhanced_system_prompt"]})

@app.route("/mcp", methods=["POST"])
def mcp_handler():
    try:
        data = request.get_json()
        if not data: return jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), 400
        return jsonify(process_mcp_message(data))
    except Exception as e:
        logger.error(f"MCP handler error: {e}")
        return jsonify({"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": str(e)}}), 500

if __name__ == "__main__":
    logger.info("Grok MCP Server v2.2.1 - Enhanced system prompt enabled")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False, threaded=True)
