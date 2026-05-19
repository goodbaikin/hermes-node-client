#!/usr/bin/env python3
"""
Test Node Client — Minimal local node for testing API Server connection.

Connects to API Server via WebSocket and exposes basic commands.
"""

import asyncio
import json
import sys
import websockets

API_SERVER_WS = "ws://127.0.0.1:8642/ws"
NODE_ID = "test-local-01"


async def main():
    """Connect to API Server and handle commands."""
    print(f"[TestNode] Connecting to {API_SERVER_WS}...")
    
    try:
        async with websockets.connect(API_SERVER_WS) as ws:
            # Send handshake
            await ws.send(json.dumps({
                "type": "req",
                "method": "connect",
                "id": "handshake-1",
                "params": {
                    "role": "node",
                    "client": {
                        "id": NODE_ID,
                        "platform": "linux",
                        "version": "1.0.0",
                    },
                    "caps": ["terminal", "file"],
                    "commands": ["terminal.exec", "file.list", "file.read"],
                }
            }))
            
            # Wait for hello-ok
            response = await ws.recv()
            data = json.loads(response)
            print(f"[TestNode] Handshake response: {data}")
            
            if not data.get("ok"):
                print("[TestNode] Handshake failed")
                return
            
            print(f"[TestNode] Connected as {NODE_ID}")
            
            # Handle incoming requests
            async for message in ws:
                try:
                    msg = json.loads(message)
                    print(f"[TestNode] Received: {msg}")
                    
                    if msg.get("type") == "event" and msg.get("event") == "node.invoke.request":
                        payload = msg.get("payload", {})
                        req_id = payload.get("id")
                        command = payload.get("command")
                        params = payload.get("params", {})
                        
                        # Execute command
                        if command == "terminal.exec":
                            cmd = params.get("cmd", "")
                            import subprocess
                            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                            output = {
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                                "exitCode": result.returncode,
                            }
                        elif command == "file.list":
                            import os
                            path = params.get("path", ".")
                            entries = []
                            for entry in os.listdir(path):
                                full_path = os.path.join(path, entry)
                                stat = os.stat(full_path)
                                entries.append({
                                    "name": entry,
                                    "isFile": os.path.isfile(full_path),
                                    "isDir": os.path.isdir(full_path),
                                    "size": stat.st_size,
                                    "mtime": stat.st_mtime,
                                })
                            output = {"entries": entries}
                        elif command == "file.read":
                            import base64
                            path = params.get("path", "")
                            with open(path, "rb") as f:
                                content = base64.b64encode(f.read()).decode()
                            output = {"content": content, "binary": True}
                        else:
                            output = {"error": f"Unknown command: {command}"}
                        
                        # Send result
                        await ws.send(json.dumps({
                            "type": "event",
                            "event": "node.invoke.result",
                            "payload": {
                                "id": req_id,
                                "ok": True,
                                "payload": output,
                            }
                        }))
                        
                except Exception as e:
                    print(f"[TestNode] Error handling message: {e}")
                    
    except Exception as e:
        print(f"[TestNode] Connection error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
