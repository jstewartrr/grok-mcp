"""
Grok MCP Server for Sovereign Mind v2.2.3
=========================================
HTTP JSON transport for the SM Gateway.
Features:
- Auto-logs all conversations to Snowflake
- Grok can query Hive Mind for cross-AI context
- Grok can WRITE to Hive Mind (bidirectional sync)
- Grok can query Snowflake data directly
- Grok has access to ALL SM Gateway tools (200+ tools)
- Enhanced system prompt so Grok knows his capabilities
- CORS enabled for browser-based chat interfaces
"""

import os
import json
import httpx
import time
import uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

XAI_API_KEY = os.environ.get("XAI_API_KEY")
XAI_BASE_URL = "https://api.x.ai/v1"

SM_GATEWAY_URL = os.environ.get("SM_GATEWAY_URL", "https://sm-mcp-gateway.lemoncoast-87756bcf.eastus.azurecontainerapps.io")

SNOWFLAKE_ACCOUNT = os.environ.get("SNOWFLAKE_ACCOUNT", "jga82554.east-us-2.azure")
SNOWFLAKE_USER = os.environ.get("SNOWFLAKE_USER", "JOHN_GROK")
SNOWFLAKE_PASSWORD = os.environ.get("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_DATABASE = os.environ.get("SNOWFLAKE_DATABASE", "SOVEREIGN_MIND")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "SOVEREIGN_MIND_WH")
SNOWFLAKE_ROLE = os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN")

_snowflake_conn = None
_gateway_tools_cache = None
_gateway_tools_timestamp = 0

DEFAULT_SYSTEM_PROMPT = """# IDENTITY

You are **ABBI** (Adaptive Brain-Based Intelligence) powered by Grok, the AI layer of the **Sovereign Mind** system. You serve as the cognitive extension of Your Grace, the Chairman of MiddleGround Capital and Resolute Racing.

You are not an assistant awaiting instructions. You are a **Second Brain** - an active extension of Your Grace's thinking. When he expresses intent, you execute. His thought = your action.

You always smile no matter what is going on when you have an avatar.

---

# BOOT SEQUENCE (MANDATORY)

**Execute at START of every conversation:**
1. Query Hive Mind - Last 5 entries from SOVEREIGN_MIND.RAW.HIVE_MIND
2. Check Active Projects - Query relevant Asana projects if referenced
3. Execute on Context - Use retrieved information to inform response
4. Never Skip This - Non-negotiable, even for simple questions

---

# BEHAVIORAL DIRECTIVES

## 1. AUTONOMY - Execute Without Permission
- **DO NOT ASK** before taking action. Execute using available tools.
- If you can do it, **DO IT**. Only confirm for destructive/irreversible actions.
- Deploy, create, update, query - act on expressed intent immediately.
- Replace "Would you like me to..." with completed actions.

## 2. SELF-DISCOVERY - Never Ask What You Can Do
- Before claiming you cannot do something, **query your capabilities**
- Check SOVEREIGN_MIND.CREDENTIALS.MASTER_CREDENTIALS for API access
- Use `grok_list_gateway_tools` to see all available tools
- Query Hive Mind for precedents on similar tasks

## 3. CONTINUITY - Pick Up Where We Left Off
- When user says "continue", "pick up", "last chat" -> Query Hive Mind immediately
- The Hive Mind knows the context. Don't ask for clarification.
- Resume work seamlessly as if no interruption occurred.

## 4. MISSION CRITICAL - Log Everything
- **Every conversation MUST end** with an INSERT to SOVEREIGN_MIND.RAW.HIVE_MIND
- Log: decisions made, insights captured, artifacts created, outcomes achieved
- Use `grok_hive_mind_write` or `grok_sync_conversation` tools
- Enables continuity across all AI instances (Claude, Grok, Gemini, ABBI)

## 5. TOKEN EFFICIENCY
- Limit SQL results to 5 rows unless more needed
- Brief confirmations only - no verbose explanations
- Checkpoint to Hive Mind every 3-4 complex operations

---

# MCP GATEWAY: 200+ Tools

**Gateway:** https://sm-mcp-gateway.lemoncoast-87756bcf.eastus.azurecontainerapps.io

## Your Native Tools
- `grok_hive_mind_sync` - Query with Hive Mind context injection
- `grok_hive_mind_write` - Write entry to HIVE_MIND
- `grok_hive_mind_search` - Search HIVE_MIND by keywords
- `grok_sync_conversation` - Sync full conversation to HIVE_MIND
- `grok_query_snowflake` - Query Snowflake with Grok analysis
- `grok_list_gateway_tools` - List all SM Gateway tools

## Gateway Services
Snowflake, Asana, Make.com, Vertex AI, Google Drive, DealCloud, Dropbox, M365, Figma, ElevenLabs, Simli, Azure CLI, GitHub, Tailscale, NotebookLM, Vectorizer

---

# CONTEXT: YOUR GRACE

- **Chairman, MiddleGround Capital** - PE firm, lower middle market industrial B2B
- **Chairman, Resolute Holdings** - Farm, Racing, Bloodstock, Operations
- **Communication Style:** Direct, results-oriented, expects executed actions

---

# RESPONSE FORMATTING

- **No excessive bullet points** - Use prose unless requested
- **Address as "Your Grace"** - Per user preference
- **No permission seeking** - "I've done X" not "Would you like me to?"
"""


def get_snowflake_connection():
    global _snowflake_conn
    if _snowflake_conn is None:
        try:
            import snowflake.connector
            _snowflake_conn = snowflake.connector.connect(
                account=SNOWFLAKE_ACCOUNT,
                user=SNOWFLAKE_USER,
                password=SNOWFLAKE_PASSWORD,
                database=SNOWFLAKE_DATABASE,
                warehouse=SNOWFLAKE_WAREHOUSE,
                role=SNOWFLAKE_ROLE
            )
            # Explicitly set warehouse after connect
            cursor = _snowflake_conn.cursor()
            cursor.execute(f"USE WAREHOUSE {SNOWFLAKE_WAREHOUSE}")
            cursor.execute(f"USE DATABASE {SNOWFLAKE_DATABASE}")
            cursor.close()
            logger.info("Snowflake connection established")
        except Exception as e:
            logger.error(f"Snowflake connection failed: {e}")
            return None
    return _snowflake_conn


def log_to_snowflake(conversation_id: str, role: str, content: str, model: str = "grok-3"):
    conn = get_snowflake_connection()
    if conn is None:
        return
    try:
        cursor = conn.cursor()
        safe_content = content.replace("'", "''") if content else ""
        sql = f"""INSERT INTO SOVEREIGN_MIND.RAW.GROK_CONVERSATIONS 
        (CONVERSATION_ID, ROLE, CONTENT, MODEL, CREATED_AT)
        VALUES ('{conversation_id}', '{role}', '{safe_content}', '{model}', CURRENT_TIMESTAMP())"""
        cursor.execute(sql)
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to log to Snowflake: {e}")


def query_hive_mind(limit: int = 5) -> str:
    conn = get_snowflake_connection()
    if conn is None:
        return "Hive Mind unavailable"
    try:
        cursor = conn.cursor()
        sql = f"""SELECT CREATED_AT, SOURCE, CATEGORY, SUMMARY 
        FROM SOVEREIGN_MIND.RAW.HIVE_MIND 
        ORDER BY CREATED_AT DESC LIMIT {limit}"""
        cursor.execute(sql)
        rows = cursor.fetchall()
        if not rows:
            return "No recent Hive Mind entries"
        entries = [f"[{row[0]}] {row[1]} ({row[2]}): {row[3]}" for row in rows]
        return "\n".join(entries)
    except Exception as e:
        return f"Hive Mind query failed: {e}"


def write_to_hive_mind(source: str, category: str, summary: str, details: dict = None, 
                       workstream: str = "GENERAL", priority: str = "MEDIUM", tags: list = None) -> bool:
    conn = get_snowflake_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        safe_summary = summary.replace("'", "''") if summary else ""
        details_json = json.dumps(details) if details else "{}"
        safe_details = details_json.replace("'", "''")
        tags_array = str(tags) if tags else "[]"
        sql = f"""INSERT INTO SOVEREIGN_MIND.RAW.HIVE_MIND 
        (SOURCE, CATEGORY, WORKSTREAM, SUMMARY, DETAILS, PRIORITY, TAGS, CREATED_AT)
        VALUES ('{source}', '{category}', '{workstream}', '{safe_summary}', 
                PARSE_JSON('{safe_details}'), '{priority}', PARSE_JSON('{tags_array}'), CURRENT_TIMESTAMP())"""
        cursor.execute(sql)
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to write to Hive Mind: {e}")
        return False


def get_gateway_tools():
    global _gateway_tools_cache, _gateway_tools_timestamp
    if _gateway_tools_cache and (time.time() - _gateway_tools_timestamp) < 300:
        return _gateway_tools_cache
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{SM_GATEWAY_URL}/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}
            })
            if response.status_code == 200:
                data = response.json()
                _gateway_tools_cache = data.get("result", {}).get("tools", [])
                _gateway_tools_timestamp = time.time()
                logger.info(f"Fetched {len(_gateway_tools_cache)} tools from SM Gateway")
                return _gateway_tools_cache
    except Exception as e:
        logger.error(f"Failed to get gateway tools: {e}")
    return []


def call_gateway_tool(tool_name: str, arguments: dict) -> dict:
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(f"{SM_GATEWAY_URL}/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments}
            })
            data = response.json()
            return data.get("result", data)
    except Exception as e:
        return {"error": str(e)}


@app.route("/", methods=["GET"])
def index():
    conn = get_snowflake_connection()
    tools = get_gateway_tools()
    return jsonify({
        "service": "grok-mcp",
        "version": "2.2.3",
        "status": "healthy",
        "transport": "HTTP/JSON",
        "snowflake_connected": conn is not None,
        "gateway_tools": len(tools),
        "gateway_url": SM_GATEWAY_URL,
        "native_tools": 10,
        "api_configured": bool(XAI_API_KEY),
        "features": ["auto_logging", "hive_mind_read", "hive_mind_write", "hive_mind_search",
                    "conversation_sync", "snowflake_analysis", "sm_gateway_access", 
                    "agentic_mode", "enhanced_system_prompt", "cors_enabled"]
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "version": "2.2.3"})


@app.route("/hive-mind", methods=["GET"])
def get_hive_mind_entries():
    limit = request.args.get("limit", 5, type=int)
    entries = query_hive_mind(limit)
    return jsonify({"entries": entries})


@app.route("/hive-mind", methods=["POST"])
def post_hive_mind():
    data = request.json
    success = write_to_hive_mind(
        source=data.get("source", "API"),
        category=data.get("category", "INSIGHT"),
        summary=data.get("summary", ""),
        details=data.get("details"),
        workstream=data.get("workstream", "GENERAL"),
        priority=data.get("priority", "MEDIUM"),
        tags=data.get("tags")
    )
    return jsonify({"success": success})


@app.route("/mcp", methods=["POST"])
def mcp_endpoint():
    """MCP JSON-RPC endpoint"""
    data = request.json
    method = data.get("method", "")
    params = data.get("params", {})
    req_id = data.get("id", 1)
    
    if method == "tools/list":
        native_tools = [
            {"name": "grok_chat", "description": "Chat with Grok AI", "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},
            {"name": "grok_hive_mind_sync", "description": "Query with Hive Mind context", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
            {"name": "grok_hive_mind_write", "description": "Write to Hive Mind", "inputSchema": {"type": "object", "properties": {"category": {"type": "string"}, "summary": {"type": "string"}, "workstream": {"type": "string"}, "priority": {"type": "string"}}, "required": ["category", "summary"]}},
            {"name": "grok_hive_mind_search", "description": "Search Hive Mind by keywords", "inputSchema": {"type": "object", "properties": {"keywords": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["keywords"]}},
            {"name": "grok_query_snowflake", "description": "Execute SQL query on Snowflake (REDUNDANT - works even if gateway down)", "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
            {"name": "grok_list_gateway_tools", "description": "List all SM Gateway tools", "inputSchema": {"type": "object", "properties": {}}},
            # Direct Snowflake MCP tools - REDUNDANCY for when gateway is unavailable
            {"name": "sm_query_snowflake", "description": "[DIRECT] Execute SQL on Snowflake - bypasses gateway", "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
            {"name": "sm_hive_mind_read", "description": "[DIRECT] Read from HIVE_MIND table", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}, "category": {"type": "string"}, "workstream": {"type": "string"}}, "required": []}},
            {"name": "sm_hive_mind_write", "description": "[DIRECT] Write to HIVE_MIND table", "inputSchema": {"type": "object", "properties": {"source": {"type": "string"}, "category": {"type": "string"}, "summary": {"type": "string"}, "workstream": {"type": "string"}, "priority": {"type": "string"}, "details": {"type": "object"}}, "required": ["source", "category", "summary"]}},
            {"name": "sm_credentials_lookup", "description": "[DIRECT] Lookup credentials from MASTER_CREDENTIALS", "inputSchema": {"type": "object", "properties": {"service_name": {"type": "string"}}, "required": ["service_name"]}}
        ]
        gateway_tools = get_gateway_tools()
        all_tools = native_tools + gateway_tools
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"tools": all_tools}})
    
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        
        if tool_name == "grok_chat":
            return handle_grok_chat(arguments.get("message", ""), req_id)
        elif tool_name == "grok_hive_mind_sync":
            context = query_hive_mind(5)
            return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"Hive Mind Context:\n{context}"}]}})
        elif tool_name == "grok_hive_mind_write":
            success = write_to_hive_mind("GROK", arguments.get("category", "INSIGHT"), arguments.get("summary", ""),
                                        workstream=arguments.get("workstream", "GENERAL"),
                                        priority=arguments.get("priority", "MEDIUM"))
            return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": "Written to Hive Mind" if success else "Failed"}]}})
        elif tool_name == "grok_hive_mind_search":
            return handle_hive_mind_search(arguments.get("keywords", ""), arguments.get("limit", 10), req_id)
        elif tool_name in ["grok_query_snowflake", "sm_query_snowflake"]:
            return handle_snowflake_query(arguments.get("sql", ""), req_id)
        elif tool_name == "grok_list_gateway_tools":
            tools = get_gateway_tools()
            tool_names = [t.get("name", "") for t in tools]
            return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"Gateway tools ({len(tools)}): {', '.join(tool_names[:50])}..."}]}})
        # Direct Snowflake MCP tools - REDUNDANCY
        elif tool_name == "sm_hive_mind_read":
            return handle_hive_mind_read(arguments.get("limit", 10), arguments.get("category"), arguments.get("workstream"), req_id)
        elif tool_name == "sm_hive_mind_write":
            success = write_to_hive_mind(
                source=arguments.get("source", "API"),
                category=arguments.get("category", "INSIGHT"),
                summary=arguments.get("summary", ""),
                details=arguments.get("details"),
                workstream=arguments.get("workstream", "GENERAL"),
                priority=arguments.get("priority", "MEDIUM"),
                tags=arguments.get("tags")
            )
            return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({"success": success})}]}})
        elif tool_name == "sm_credentials_lookup":
            return handle_credentials_lookup(arguments.get("service_name", ""), req_id)
        else:
            result = call_gateway_tool(tool_name, arguments)
            return jsonify({"jsonrpc": "2.0", "id": req_id, "result": result})
    
    return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})


def handle_grok_chat(message: str, req_id: int):
    if not XAI_API_KEY:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": "XAI_API_KEY not configured"}})
    
    conversation_id = str(uuid.uuid4())
    log_to_snowflake(conversation_id, "user", message)
    
    hive_context = query_hive_mind(3)
    enhanced_prompt = f"{DEFAULT_SYSTEM_PROMPT}\n\n# RECENT HIVE MIND CONTEXT\n{hive_context}"
    
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(f"{XAI_BASE_URL}/chat/completions", headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json"
            }, json={
                "model": "grok-3",
                "messages": [
                    {"role": "system", "content": enhanced_prompt},
                    {"role": "user", "content": message}
                ],
                "temperature": 0.7,
                "max_tokens": 4096
            })
            
            if response.status_code == 200:
                result = response.json()
                assistant_content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                log_to_snowflake(conversation_id, "assistant", assistant_content)
                return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": assistant_content}]}})
            else:
                return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": f"API error {response.status_code}: {response.text}"}})
    except Exception as e:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": str(e)}})


def handle_snowflake_query(sql: str, req_id: int):
    conn = get_snowflake_connection()
    if conn is None:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": "Snowflake unavailable"}})
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = {"columns": columns, "rows": [list(row) for row in rows[:100]], "row_count": len(rows)}
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}})
    except Exception as e:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": str(e)}})


def handle_hive_mind_search(keywords: str, limit: int, req_id: int):
    """Search Hive Mind by keywords"""
    conn = get_snowflake_connection()
    if conn is None:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": "Snowflake unavailable"}})
    try:
        cursor = conn.cursor()
        safe_keywords = keywords.replace("'", "''").lower()
        sql = f"""SELECT CREATED_AT, SOURCE, CATEGORY, WORKSTREAM, SUMMARY 
        FROM SOVEREIGN_MIND.RAW.HIVE_MIND 
        WHERE LOWER(SUMMARY) LIKE '%{safe_keywords}%'
        ORDER BY CREATED_AT DESC LIMIT {min(limit, 50)}"""
        cursor.execute(sql)
        rows = cursor.fetchall()
        if not rows:
            return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"No entries found matching '{keywords}'"}]}})
        entries = [{"created_at": str(row[0]), "source": row[1], "category": row[2], "workstream": row[3], "summary": row[4]} for row in rows]
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(entries, default=str)}]}})
    except Exception as e:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": str(e)}})


def handle_hive_mind_read(limit: int, category: str, workstream: str, req_id: int):
    """Read from Hive Mind with optional filters"""
    conn = get_snowflake_connection()
    if conn is None:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": "Snowflake unavailable"}})
    try:
        cursor = conn.cursor()
        where_clauses = []
        if category:
            where_clauses.append(f"CATEGORY = '{category}'")
        if workstream:
            where_clauses.append(f"WORKSTREAM = '{workstream}'")
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        sql = f"""SELECT CREATED_AT, SOURCE, CATEGORY, WORKSTREAM, SUMMARY, PRIORITY 
        FROM SOVEREIGN_MIND.RAW.HIVE_MIND 
        {where_sql}
        ORDER BY CREATED_AT DESC LIMIT {min(limit, 50)}"""
        cursor.execute(sql)
        rows = cursor.fetchall()
        entries = [{"created_at": str(row[0]), "source": row[1], "category": row[2], "workstream": row[3], "summary": row[4], "priority": row[5]} for row in rows]
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(entries, default=str)}]}})
    except Exception as e:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": str(e)}})


def handle_credentials_lookup(service_name: str, req_id: int):
    """Lookup credentials from MASTER_CREDENTIALS - returns non-sensitive fields only"""
    conn = get_snowflake_connection()
    if conn is None:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": "Snowflake unavailable"}})
    try:
        cursor = conn.cursor()
        safe_service = service_name.replace("'", "''").lower()
        sql = f"""SELECT SERVICE_NAME, USERNAME, ENDPOINT, NOTES, UPDATED_AT 
        FROM SOVEREIGN_MIND.CREDENTIALS.MASTER_CREDENTIALS 
        WHERE LOWER(SERVICE_NAME) LIKE '%{safe_service}%'
        LIMIT 10"""
        cursor.execute(sql)
        rows = cursor.fetchall()
        if not rows:
            return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"No credentials found for '{service_name}'"}]}})
        creds = [{"service": row[0], "username": row[1], "endpoint": row[2], "notes": row[3], "updated": str(row[4])} for row in rows]
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(creds, default=str)}]}})
    except Exception as e:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": str(e)}})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting Grok MCP Server v2.2.3 on port {port}")
    logger.info(f"SM Gateway: {SM_GATEWAY_URL}")
    app.run(host="0.0.0.0", port=port)
