"""
Grok MCP Server for Sovereign Mind
===================================
Provides xAI Grok integration for the Hive Mind ecosystem.
"""

import os
import json
import httpx
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse
import uvicorn

# Configuration
XAI_API_KEY = os.environ.get("XAI_API_KEY")
XAI_BASE_URL = "https://api.x.ai/v1"

server = Server("grok-mcp")

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="grok_chat",
            description="Chat with Grok AI. Send a message and get a response from xAI's Grok model.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to send to Grok"
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional system prompt to set context",
                        "default": "You are Grok, a helpful AI assistant."
                    },
                    "model": {
                        "type": "string",
                        "description": "Model to use (grok-2-latest, grok-2-1212, grok-beta)",
                        "default": "grok-2-latest"
                    }
                },
                "required": ["message"]
            }
        ),
        Tool(
            name="grok_analyze",
            description="Use Grok to analyze text, code, or data with its unique perspective.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The content to analyze"
                    },
                    "analysis_type": {
                        "type": "string",
                        "description": "Type of analysis: summary, critique, explain, improve",
                        "enum": ["summary", "critique", "explain", "improve"]
                    }
                },
                "required": ["content", "analysis_type"]
            }
        ),
        Tool(
            name="grok_hive_mind_sync",
            description="Query Grok with Hive Mind context for cross-AI continuity.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The query for Grok"
                    },
                    "hive_mind_context": {
                        "type": "string",
                        "description": "Context from Hive Mind to inform the response"
                    }
                },
                "required": ["query"]
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if not XAI_API_KEY:
        return [TextContent(type="text", text="Error: XAI_API_KEY not configured")]
    
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            if name == "grok_chat":
                message = arguments.get("message", "")
                system_prompt = arguments.get("system_prompt", "You are Grok, a helpful AI assistant.")
                model = arguments.get("model", "grok-2-latest")
                
                response = await client.post(
                    f"{XAI_BASE_URL}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": message}
                        ]
                    }
                )
                
                if response.status_code == 200:
                    data = response.json()
                    reply = data["choices"][0]["message"]["content"]
                    return [TextContent(type="text", text=reply)]
                else:
                    return [TextContent(type="text", text=f"Error: {response.status_code} - {response.text}")]
            
            elif name == "grok_analyze":
                content = arguments.get("content", "")
                analysis_type = arguments.get("analysis_type", "summary")
                
                prompts = {
                    "summary": f"Provide a concise summary of the following:\n\n{content}",
                    "critique": f"Provide a critical analysis of the following, identifying strengths and weaknesses:\n\n{content}",
                    "explain": f"Explain the following in clear, simple terms:\n\n{content}",
                    "improve": f"Suggest improvements for the following:\n\n{content}"
                }
                
                response = await client.post(
                    f"{XAI_BASE_URL}/chat/completions",
                    headers=headers,
                    json={
                        "model": "grok-2-latest",
                        "messages": [
                            {"role": "system", "content": "You are Grok, an analytical AI assistant."},
                            {"role": "user", "content": prompts.get(analysis_type, prompts["summary"])}
                        ]
                    }
                )
                
                if response.status_code == 200:
                    data = response.json()
                    reply = data["choices"][0]["message"]["content"]
                    return [TextContent(type="text", text=reply)]
                else:
                    return [TextContent(type="text", text=f"Error: {response.status_code} - {response.text}")]
            
            elif name == "grok_hive_mind_sync":
                query = arguments.get("query", "")
                context = arguments.get("hive_mind_context", "")
                
                system = """You are Grok, part of the Sovereign Mind Hive Mind ecosystem. 
You have access to shared context from other AI systems (Claude, Gemini, etc.).
Use this context to provide informed, consistent responses that align with the user's ongoing projects and preferences."""
                
                user_message = query
                if context:
                    user_message = f"Hive Mind Context:\n{context}\n\nQuery: {query}"
                
                response = await client.post(
                    f"{XAI_BASE_URL}/chat/completions",
                    headers=headers,
                    json={
                        "model": "grok-2-latest",
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user_message}
                        ]
                    }
                )
                
                if response.status_code == 200:
                    data = response.json()
                    reply = data["choices"][0]["message"]["content"]
                    return [TextContent(type="text", text=reply)]
                else:
                    return [TextContent(type="text", text=f"Error: {response.status_code} - {response.text}")]
            
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]
                
        except Exception as e:
            return [TextContent(type="text", text=f"Error calling Grok API: {str(e)}")]

# SSE Transport setup
sse = SseServerTransport("/messages/")

async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())

async def handle_messages(request):
    await sse.handle_post_message(request.scope, request.receive, request._send)

async def health(request):
    return JSONResponse({
        "status": "healthy",
        "service": "grok-mcp",
        "version": "1.0.0",
        "transport": "SSE",
        "api_configured": bool(XAI_API_KEY)
    })

app = Starlette(
    routes=[
        Route("/", health),
        Route("/sse", handle_sse),
        Mount("/messages", routes=[Route("/", handle_messages, methods=["POST"])])
    ]
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
