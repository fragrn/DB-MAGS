"""Bounded stdio JSON-RPC client for a local Codex app-server."""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path


class CodexDriver:
    def __init__(self, binary=None, timeout=600, sandbox="read-only"):
        self.binary = binary or os.environ.get("INPUT_ANALYSIS_CODEX_BINARY") or shutil.which("codex")
        if not self.binary:
            raise FileNotFoundError("Install/authenticate Codex or set INPUT_ANALYSIS_CODEX_BINARY")
        self.timeout, self.sandbox = timeout, sandbox

    def decide(self, prompt, directory, workdir):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "prompt.txt").write_text(prompt)
        messages = queue.Queue()
        stderr = (directory / "stderr.log").open("w")
        proc = subprocess.Popen([self.binary, "app-server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=stderr, text=True)
        def reader():
            for line in proc.stdout:
                messages.put(line)
            messages.put(None)
        threading.Thread(target=reader, daemon=True).start()
        deadline = time.monotonic() + self.timeout
        serial = 0
        def send(method, params, request=True):
            nonlocal serial
            serial += 1
            payload = {"jsonrpc": "2.0", "method": method, "params": params}
            if request:
                payload["id"] = serial
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
            return serial
        try:
            with (directory / "events.jsonl").open("w") as log:
                def receive():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Codex decision timed out")
                    try:
                        line = messages.get(timeout=remaining)
                    except queue.Empty:
                        raise TimeoutError("Codex decision timed out") from None
                    if line is None:
                        raise RuntimeError("Codex app-server exited before completion")
                    log.write(line); log.flush()
                    event = json.loads(line)
                    if "method" in event and "id" in event:
                        proc.stdin.write(json.dumps({"jsonrpc":"2.0", "id":event["id"], "error":{"code":-32601,"message":"Interactive requests unavailable"}})+"\n")
                        proc.stdin.flush()
                    return event
                def request(method, params):
                    ident = send(method, params)
                    while True:
                        event = receive()
                        if event.get("id") == ident:
                            if "error" in event:
                                raise RuntimeError(str(event["error"]))
                            return event["result"]
                request("initialize", {"clientInfo":{"name":"dbmags", "version":"1.0"}})
                send("initialized", {}, False)
                result = request("thread/start", {"cwd":str(Path(workdir).resolve()),
                    "approvalPolicy":"never", "sandbox":self.sandbox,
                    "developerInstructions":"Make database reproduction decisions. Do not execute experiments or modify repository files. Return one JSON object only. Read local schemas/tools if needed; controller owns execution."})
                thread = result["thread"]["id"]
                (directory / "thread.json").write_text(json.dumps({"thread_id":thread}))
                request("turn/start", {"threadId":thread, "input":[{"type":"text", "text":prompt}]})
                final = ""
                while True:
                    event = receive()
                    params = event.get("params", {})
                    if event.get("method") == "item/completed":
                        item = params.get("item", {})
                        if item.get("type") == "agentMessage":
                            final = item.get("text", "")
                    if event.get("method") == "turn/completed":
                        if params.get("turn", {}).get("status") != "completed":
                            raise RuntimeError(str(params))
                        break
                (directory / "response.txt").write_text(final)
                payload = json.loads(final)
                if not isinstance(payload, dict):
                    raise ValueError("Codex decision must be a JSON object")
                return payload
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
            proc.stdin.close(); proc.stdout.close(); stderr.close()
