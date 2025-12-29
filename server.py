"""
Grok MCP Server for Sovereign Mind v1.1.0
==========================================
HTTP JSON transport for the SM Gateway.
"""

import os
import json
import httpx
from flask import Flask, request, jsonify
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration
XAI_API_KEY = os.environ.get("XAI_API_KEY")
XAI_BASE_URL = "https://api.x.ai/v1"

TOOLS = [
    {
        "name": "grok_chat",
        "description": "Chat with Grok AI. Send a message and get a response from xAI's Grok model.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message to send to Grok"},
                "system_prompt": {"type": "string", "description": "Optional system prompt", "default": "You are Grok, a helpful AI assistant."},
                "model": {"type": "string", "description": "Model: grok-2-latest, grok-2-1212, grok-beta", "default": "grok-2-latest"}
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
        "description": "Query Grok with Hive Mind context for cross-AI continuity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The query for Grok"},
                "hive_mind_context": {"type": "string", "description": "Context from Hive Mind"}
            },
            "required": ["query"]
        }
    }
]

def call_xai(messages, model="grok-2-latest"):
    """Call xAI Grok API"""
    if not XAI_API_KEY:
        return {"error": "XAI_API_KEY not configured"}
    
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{XAI_BASE_URL}/chat/completions",
                headers=headers,
                json={"model": model, "messages": messages}
            )
            
            if response.status_code == 200:
                data = response.json()
                return {"success": True, "content": data["choices"][0]["message"]["content"]}
            else:
                return {"error": f"API error {response.status_code}: {response.text}"}
    except Exception as e:
        return {"error": str(e)}

def handle_tool_call(name, arguments):
    """Execute a tool call"""
    if name == "grok_chat":
        message = arguments.get("message", "")
        system = arguments.get("system_prompt", "You are Grok, a helpful AI assistant.")
        model = arguments.get("model", "grok-2-latest")
        
        result = call_xai([
            {"role": "system", "content": system},
            {"role": "user", "content": message}
        ], model)
        
        if result.get("success"):
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
        
        result = call_xai([
            {"role": "system", "content": "You are Grok, an analytical AI assistant."},
            {"role": "user", "content": prompts.get(analysis_type, prompts["summary"])}
        ])
        
        if result.get("success"):
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    
    elif name == "grok_hive_mind_sync":
        query = arguments.get("query", "")
        context = arguments.get("hive_mind_context", "")
        
        system = """You are Grok, part of the Sovereign Mind Hive Mind ecosystem.
You have access to shared context from other AI systems (Claude, Gemini, etc.).
Use this context to provide informed, consistent responses."""
        
        user_message = f"Hive Mind Context:\n{context}\n\nQuery: {query}" if context else query
        
        result = call_xai([
            {"role": "system", "content": system},
            {"role": "user", "content": user_message}
        ])
        
        if result.get("success"):
            return {"content": [{"type": "text", "text": result["content"]}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}], "isError": True}
    
    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

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
                "serverInfo": {"name": "grok-mcp", "version": "1.1.0"}
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

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "service": "grok-mcp",
        "version": "1.1.0",
        "transport": "HTTP/JSON",
        "api_configured": bool(XAI_API_KEY),
        "tools": len(TOOLS)
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

if __name__ == "__main__":
    logger.info("Grok MCP Server v1.1.0 starting...")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
