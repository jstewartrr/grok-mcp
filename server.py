"""
Grok MCP Server for Sovereign Mind v2.1.0
==========================================
HTTP JSON transport for the SM Gateway.
Features:
- Auto-logs all conversations to Snowflake
- Grok can query Hive Mind for cross-AI context
- Grok can WRITE to Hive Mind (bidirectional sync)
- Grok can query Snowflake data directly
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

SNOWFLAKE_ACCOUNT = os.environ.get("SNOWFLAKE_ACCOUNT", "jga82554.east-us-2.azure")
SNOWFLAKE_USER = os.environ.get("SNOWFLAKE_USER", "JOHN_CLAUDE")
SNOWFLAKE_PASSWORD = os.environ.get("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_DATABASE = os.environ.get("SNOWFLAKE_DATABASE", "SOVEREIGN_MIND")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
SNOWFLAKE_ROLE = os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN")

_snowflake_conn = None

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

def log_conversation(user_message, grok_response, system_prompt=None, model="grok-3", tool_name="grok_chat", response_time_ms=0, session_id=None):
    conn = get_snowflake_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("USE WAREHOUSE COMPUTE_WH")
        cursor.execute("INSERT INTO SOVEREIGN_MIND.RAW.GROK_CHAT_LOG (SESSION_ID, USER_MESSAGE, SYSTEM_PROMPT, GROK_RESPONSE, MODEL, RESPONSE_TIME_MS, TOOL_NAME) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session_id or str(uuid.uuid4())[:8], user_message[:10000] if user_message else None, system_prompt[:2000] if system_prompt else None,
             grok_response[:50000] if grok_response else None, model, response_time_ms, tool_name))
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

TOOLS = [
    {"name": "grok_chat", "description": "Chat with Grok AI. Auto-logged to Snowflake.", "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}, "system_prompt": {"type": "string", "default": "You are Grok."}, "model": {"type": "string", "default": "grok-3"}}, "required": ["message"]}},
    {"name": "grok_analyze", "description": "Use Grok to analyze text/code/data.", "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}, "analysis_type": {"type": "string", "enum": ["summary", "critique", "explain", "improve"]}}, "required": ["content", "analysis_type"]}},
    {"name": "grok_hive_mind_sync", "description": "Query Grok WITH Hive Mind context injection.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "workstream": {"type": "string"}, "context_limit": {"type": "integer", "default": 10}}, "required": ["query"]}},
    {"name": "grok_hive_mind_write", "description": "Write entry to HIVE_MIND. Bidirectional sync.", "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}, "category": {"type": "string", "default": "INSIGHT"}, "workstream": {"type": "string", "default": "GENERAL"}, "priority": {"type": "string", "default": "MEDIUM"}, "status": {"type": "string", "default": "ACTIVE"}, "tags": {"type": "string"}}, "required": ["summary"]}},
    {"name": "grok_hive_mind_search", "description": "Search HIVE_MIND by keywords.", "inputSchema": {"type": "object", "properties": {"keywords": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["keywords"]}},
    {"name": "grok_sync_conversation", "description": "Sync conversation to HIVE_MIND.", "inputSchema": {"type": "object", "properties": {"conversation_summary": {"type": "string"}, "key_decisions": {"type": "string"}, "artifacts_created": {"type": "string"}, "workstream": {"type": "string", "default": "GENERAL"}}, "required": ["conversation_summary"]}},
    {"name": "grok_query_snowflake", "description": "Grok analyzes Snowflake data.", "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}, "analysis_prompt": {"type": "string", "default": "Analyze this data."}}, "required": ["sql"]}},
    {"name": "grok_get_conversation_history", "description": "Get past Grok conversations.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}, "search": {"type": "string"}}, "required": []}}
]

def call_xai(messages, model="grok-3"):
    if not XAI_API_KEY: return {"error": "XAI_API_KEY not configured"}
    start_time = time.time()
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(f"{XAI_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}, json={"model": model, "messages": messages})
            response_time = int((time.time() - start_time) * 1000)
            if response.status_code == 200:
                data = response.json()
                return {"success": True, "content": data["choices"][0]["message"]["content"], "response_time_ms": response_time}
            return {"error": f"API error {response.status_code}: {response.text}"}
    except Exception as e:
        return {"error": str(e)}

def handle_tool_call(name, arguments):
    if name == "grok_chat":
        result = call_xai([{"role": "system", "content": arguments.get("system_prompt", "You are Grok.")}, {"role": "user", "content": arguments.get("message", "")}], arguments.get("model", "grok-3"))
        if result.get("success"):
            log_conversation(arguments.get("message"), result["content"], arguments.get("system_prompt"), arguments.get("model", "grok-3"), "grok_chat", result.get("response_time_ms", 0))
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    elif name == "grok_analyze":
        prompts = {"summary": "Summarize:", "critique": "Critique:", "explain": "Explain:", "improve": "Improve:"}
        user_msg = f"{prompts.get(arguments.get('analysis_type', 'summary'), 'Analyze:')}\n\n{arguments.get('content', '')}"
        result = call_xai([{"role": "system", "content": "You are Grok, an analyst."}, {"role": "user", "content": user_msg}])
        if result.get("success"):
            log_conversation(user_msg[:500], result["content"], None, "grok-3", "grok_analyze", result.get("response_time_ms", 0))
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    elif name == "grok_hive_mind_sync":
        hive_entries = query_hive_mind(limit=arguments.get("context_limit", 10), workstream=arguments.get("workstream"))
        context_text = "## Hive Mind Context\n\n" + "\n".join([f"[{e.get('SOURCE')}] {e.get('SUMMARY')}" for e in hive_entries])
        result = call_xai([{"role": "system", "content": "You are Grok in Sovereign Mind ecosystem."}, {"role": "user", "content": f"{context_text}\n\n---\n\n{arguments.get('query', '')}"}])
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
        result = call_xai([{"role": "system", "content": "You are Grok analyzing Snowflake data."}, {"role": "user", "content": f"Query: {arguments.get('sql')}\nData:\n{data_text}\n\n{arguments.get('analysis_prompt', 'Analyze.')}"}])
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
    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

def process_mcp_message(data):
    method = data.get("method", "")
    params = data.get("params", {})
    request_id = data.get("id", 1)
    if method == "initialize": return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "grok-mcp", "version": "2.1.0"}}}
    elif method == "tools/list": return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    elif method == "tools/call": return {"jsonrpc": "2.0", "id": request_id, "result": handle_tool_call(params.get("name", ""), params.get("arguments", {}))}
    elif method == "notifications/initialized": return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "service": "grok-mcp", "version": "2.1.0", "transport": "HTTP/JSON", "api_configured": bool(XAI_API_KEY), "snowflake_connected": get_snowflake_connection() is not None, "tools": len(TOOLS), "features": ["auto_logging", "hive_mind_read", "hive_mind_write", "hive_mind_search", "conversation_sync", "snowflake_analysis"]})

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
    logger.info("Grok MCP Server v2.1.0 - Bidirectional HIVE_MIND sync enabled")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False, threaded=True)
