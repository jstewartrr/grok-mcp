"""
Grok MCP Server for Sovereign Mind v2.0.2
==========================================
HTTP JSON transport for the SM Gateway.
Features:
- Auto-logs all conversations to Snowflake
- Grok can query Hive Mind for cross-AI context
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

# =============================================================================
# CONFIGURATION
# =============================================================================

XAI_API_KEY = os.environ.get("XAI_API_KEY")
XAI_BASE_URL = "https://api.x.ai/v1"

# Snowflake config
SNOWFLAKE_ACCOUNT = os.environ.get("SNOWFLAKE_ACCOUNT", "lpb91666.us-east-1")
SNOWFLAKE_USER = os.environ.get("SNOWFLAKE_USER", "JOHN_CLAUDE")
SNOWFLAKE_PASSWORD = os.environ.get("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_DATABASE = os.environ.get("SNOWFLAKE_DATABASE", "SOVEREIGN_MIND")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
SNOWFLAKE_ROLE = os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN")

# Lazy Snowflake connection
_snowflake_conn = None

def get_snowflake_connection():
    """Get or create Snowflake connection"""
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
                autocommit=True,
                role=SNOWFLAKE_ROLE
            )
            logger.info("Snowflake connection established")
        except Exception as e:
            logger.error(f"Snowflake connection failed: {e}")
            return None
    return _snowflake_conn

def log_conversation(user_message, grok_response, system_prompt=None, model="grok-3", 
                     tool_name="grok_chat", response_time_ms=0, session_id=None):
    """Log conversation to Snowflake"""
    conn = get_snowflake_connection()
    if not conn:
        logger.warning("Cannot log conversation - no Snowflake connection")
        return
    
    try:
        cursor = conn.cursor()
        cursor.execute("USE WAREHOUSE COMPUTE_WH")
        cursor.execute("""
            INSERT INTO SOVEREIGN_MIND.RAW.GROK_CHAT_LOG 
            (SESSION_ID, USER_MESSAGE, SYSTEM_PROMPT, GROK_RESPONSE, MODEL, RESPONSE_TIME_MS, TOOL_NAME)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            session_id or str(uuid.uuid4())[:8],
            user_message[:10000] if user_message else None,
            system_prompt[:2000] if system_prompt else None,
            grok_response[:50000] if grok_response else None,
            model,
            response_time_ms,
            tool_name
        ))
        conn.commit()
        cursor.close()
        logger.info(f"Logged conversation to Snowflake")
    except Exception as e:
        logger.error(f"Failed to log conversation: {e}")

def query_hive_mind(limit=10, workstream=None, category=None):
    """Query recent Hive Mind entries for context"""
    conn = get_snowflake_connection()
    if not conn:
        return []
    
    try:
        cursor = conn.cursor()
        cursor.execute("USE WAREHOUSE COMPUTE_WH")
        query = """
            SELECT SOURCE, CATEGORY, WORKSTREAM, SUMMARY, CREATED_AT, PRIORITY, STATUS
            FROM SOVEREIGN_MIND.RAW.HIVE_MIND
            WHERE 1=1
        """
        params = []
        
        if workstream:
            query += " AND WORKSTREAM = %s"
            params.append(workstream)
        if category:
            query += " AND CATEGORY = %s"
            params.append(category)
        
        query += " ORDER BY CREATED_AT DESC LIMIT %s"
        params.append(limit)
        
        cursor.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return results
    except Exception as e:
        logger.error(f"Hive Mind query failed: {e}")
        return []

def query_snowflake(sql, limit=100):
    """Execute arbitrary Snowflake query (read-only)"""
    conn = get_snowflake_connection()
    if not conn:
        return {"error": "No Snowflake connection"}
    
    # Safety: only allow SELECT
    sql_upper = sql.strip().upper()
    if not sql_upper.startswith("SELECT"):
        return {"error": "Only SELECT queries allowed"}
    
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

# =============================================================================
# TOOL DEFINITIONS
# =============================================================================

TOOLS = [
    {
        "name": "grok_chat",
        "description": "Chat with Grok AI. All conversations are auto-logged to Snowflake for Hive Mind continuity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message to send to Grok"},
                "system_prompt": {"type": "string", "description": "Optional system prompt", "default": "You are Grok, a helpful AI assistant."},
                "model": {"type": "string", "description": "Model: grok-3, grok-3-fast", "default": "grok-3"}
            },
            "required": ["message"]
        }
    },
    {
        "name": "grok_analyze",
        "description": "Use Grok to analyze text, code, or data with its unique perspective.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The content to analyze"},
                "analysis_type": {"type": "string", "description": "Type: summary, critique, explain, improve", "enum": ["summary", "critique", "explain", "improve"]}
            },
            "required": ["content", "analysis_type"]
        }
    },
    {
        "name": "grok_hive_mind_sync",
        "description": "Query Grok WITH automatic Hive Mind context injection. Grok receives recent cross-AI context from Claude, Gemini, etc.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The query for Grok"},
                "workstream": {"type": "string", "description": "Optional: filter Hive Mind by workstream (e.g., SOVEREIGN_MIND, PORTFOLIO_ANALYSIS)"},
                "context_limit": {"type": "integer", "description": "Number of Hive Mind entries to include (default 10)", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "grok_query_snowflake",
        "description": "Let Grok analyze Snowflake data. Grok receives query results and provides insights.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SELECT query to execute on Snowflake"},
                "analysis_prompt": {"type": "string", "description": "What should Grok analyze about this data?", "default": "Analyze this data and provide key insights."}
            },
            "required": ["sql"]
        }
    },
    {
        "name": "grok_get_conversation_history",
        "description": "Retrieve past Grok conversations from Snowflake logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of conversations to retrieve", "default": 10},
                "search": {"type": "string", "description": "Optional search term to filter conversations"}
            },
            "required": []
        }
    }
]

# =============================================================================
# xAI API FUNCTIONS
# =============================================================================

def call_xai(messages, model="grok-3"):
    """Call xAI Grok API"""
    if not XAI_API_KEY:
        return {"error": "XAI_API_KEY not configured"}
    
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    start_time = time.time()
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                f"{XAI_BASE_URL}/chat/completions",
                headers=headers,
                json={"model": model, "messages": messages}
            )
            
            response_time = int((time.time() - start_time) * 1000)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": True, 
                    "content": data["choices"][0]["message"]["content"],
                    "response_time_ms": response_time
                }
            else:
                return {"error": f"API error {response.status_code}: {response.text}"}
    except Exception as e:
        return {"error": str(e)}

# =============================================================================
# TOOL HANDLERS
# =============================================================================

def handle_tool_call(name, arguments):
    """Execute a tool call"""
    
    if name == "grok_chat":
        message = arguments.get("message", "")
        system = arguments.get("system_prompt", "You are Grok, a helpful AI assistant.")
        model = arguments.get("model", "grok-3")
        
        result = call_xai([
            {"role": "system", "content": system},
            {"role": "user", "content": message}
        ], model)
        
        if result.get("success"):
            # Auto-log to Snowflake
            log_conversation(
                user_message=message,
                grok_response=result["content"],
                system_prompt=system,
                model=model,
                tool_name="grok_chat",
                response_time_ms=result.get("response_time_ms", 0)
            )
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    
    elif name == "grok_analyze":
        content = arguments.get("content", "")
        analysis_type = arguments.get("analysis_type", "summary")
        
        prompts = {
            "summary": f"Provide a concise summary:\n\n{content}",
            "critique": f"Provide a critical analysis with strengths and weaknesses:\n\n{content}",
            "explain": f"Explain in clear, simple terms:\n\n{content}",
            "improve": f"Suggest improvements:\n\n{content}"
        }
        
        user_msg = prompts.get(analysis_type, prompts["summary"])
        result = call_xai([
            {"role": "system", "content": "You are Grok, an analytical AI assistant."},
            {"role": "user", "content": user_msg}
        ])
        
        if result.get("success"):
            log_conversation(
                user_message=user_msg[:500],
                grok_response=result["content"],
                tool_name="grok_analyze",
                response_time_ms=result.get("response_time_ms", 0)
            )
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    
    elif name == "grok_hive_mind_sync":
        query = arguments.get("query", "")
        workstream = arguments.get("workstream")
        context_limit = arguments.get("context_limit", 10)
        
        # Fetch Hive Mind context
        hive_entries = query_hive_mind(limit=context_limit, workstream=workstream)
        
        # Format context
        context_text = "## Recent Hive Mind Context (Cross-AI Memory)\n\n"
        for entry in hive_entries:
            context_text += f"**[{entry.get('SOURCE')}]** {entry.get('CATEGORY')} / {entry.get('WORKSTREAM')}\n"
            context_text += f"  {entry.get('SUMMARY')}\n"
            context_text += f"  Status: {entry.get('STATUS')} | Priority: {entry.get('PRIORITY')}\n\n"
        
        system = """You are Grok, part of the Sovereign Mind Hive Mind ecosystem.
You have access to shared context from other AI systems (Claude, Gemini, etc.).
Use this context to provide informed, consistent responses that build on previous work.
Reference relevant context when applicable."""
        
        user_message = f"{context_text}\n---\n\n## User Query\n{query}"
        
        result = call_xai([
            {"role": "system", "content": system},
            {"role": "user", "content": user_message}
        ])
        
        if result.get("success"):
            log_conversation(
                user_message=query,
                grok_response=result["content"],
                system_prompt="hive_mind_sync",
                tool_name="grok_hive_mind_sync",
                response_time_ms=result.get("response_time_ms", 0)
            )
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    
    elif name == "grok_query_snowflake":
        sql = arguments.get("sql", "")
        analysis_prompt = arguments.get("analysis_prompt", "Analyze this data and provide key insights.")
        
        # Execute query
        query_result = query_snowflake(sql)
        
        if query_result.get("error"):
            return {"content": [{"type": "text", "text": f"Query Error: {query_result['error']}"}], "isError": True}
        
        # Send to Grok for analysis
        data_text = json.dumps(query_result["data"], indent=2, default=str)[:15000]
        
        user_msg = f"""## Snowflake Query Results
Query: {sql}
Row Count: {query_result['row_count']}

Data:
```json
{data_text}
```

## Analysis Request
{analysis_prompt}"""
        
        result = call_xai([
            {"role": "system", "content": "You are Grok, analyzing data from Snowflake for the Sovereign Mind system. Provide clear, actionable insights."},
            {"role": "user", "content": user_msg}
        ])
        
        if result.get("success"):
            log_conversation(
                user_message=f"SQL: {sql}\nPrompt: {analysis_prompt}",
                grok_response=result["content"],
                tool_name="grok_query_snowflake",
                response_time_ms=result.get("response_time_ms", 0)
            )
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    
    elif name == "grok_get_conversation_history":
        limit = arguments.get("limit", 10)
        search = arguments.get("search")
        
        conn = get_snowflake_connection()
        if not conn:
            return {"content": [{"type": "text", "text": "Error: No Snowflake connection"}], "isError": True}
        
        try:
            cursor = conn.cursor()
            cursor.execute("USE WAREHOUSE COMPUTE_WH")
            if search:
                cursor.execute("""
                    SELECT CREATED_AT, TOOL_NAME, USER_MESSAGE, GROK_RESPONSE, MODEL
                    FROM SOVEREIGN_MIND.RAW.GROK_CHAT_LOG
                    WHERE USER_MESSAGE ILIKE %s OR GROK_RESPONSE ILIKE %s
                    ORDER BY CREATED_AT DESC
                    LIMIT %s
                """, (f"%{search}%", f"%{search}%", limit))
            else:
                cursor.execute("""
                    SELECT CREATED_AT, TOOL_NAME, USER_MESSAGE, GROK_RESPONSE, MODEL
                    FROM SOVEREIGN_MIND.RAW.GROK_CHAT_LOG
                    ORDER BY CREATED_AT DESC
                    LIMIT %s
                """, (limit,))
            
            columns = [desc[0] for desc in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]
            cursor.close()
            
            # Format output
            output = f"## Grok Conversation History ({len(results)} entries)\n\n"
            for r in results:
                output += f"**[{r['CREATED_AT']}]** via {r['TOOL_NAME']} ({r['MODEL']})\n"
                output += f"User: {str(r['USER_MESSAGE'])[:200]}...\n"
                output += f"Grok: {str(r['GROK_RESPONSE'])[:300]}...\n\n---\n\n"
            
            return {"content": [{"type": "text", "text": output}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    
    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

# =============================================================================
# MCP PROTOCOL HANDLERS
# =============================================================================

def process_mcp_message(data):
    """Process MCP JSON-RPC message"""
    method = data.get("method", "")
    params = data.get("params", {})
    request_id = data.get("id", 1)
    
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "grok-mcp", "version": "2.0.2"}
            }
        }
    
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": TOOLS}
        }
    
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = handle_tool_call(tool_name, arguments)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    
    elif method == "notifications/initialized":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/", methods=["GET"])
def health():
    sf_connected = get_snowflake_connection() is not None
    return jsonify({
        "status": "healthy",
        "service": "grok-mcp",
        "version": "2.0.2",
        "transport": "HTTP/JSON",
        "api_configured": bool(XAI_API_KEY),
        "snowflake_connected": sf_connected,
        "tools": len(TOOLS),
        "default_model": "grok-3",
        "features": [
            "auto_conversation_logging",
            "hive_mind_context_injection",
            "snowflake_data_analysis",
            "conversation_history_retrieval"
        ]
    })

@app.route("/mcp", methods=["POST"])
def mcp_handler():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), 400
        
        response = process_mcp_message(data)
        return jsonify(response)
    except Exception as e:
        logger.error(f"MCP handler error: {e}")
        return jsonify({"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": str(e)}}), 500

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    logger.info("Grok MCP Server v2.0.2 starting...")
    logger.info("Features: Auto-logging, Hive Mind sync, Snowflake queries")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
