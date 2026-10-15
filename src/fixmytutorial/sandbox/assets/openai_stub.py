"""Offline OpenAI-compatible server so LLM tutorials can be verified without a real API key."""
import hashlib
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = "This is an offline reply from the Fix My Tutorial stand-in server. Your code reached the API correctly."
MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-3.5-turbo", "text-embedding-3-small"]
USAGE = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


def vec(text, dim=1536):
    h = hashlib.sha256(text.encode()).digest()
    return [(h[i % 32] / 255.0) - 0.5 for i in range(dim)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/").endswith("/models"):
            return self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "stub"} for m in MODELS]})
        self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        model = body.get("model", "gpt-4o-mini")
        path = self.path.split("?")[0].rstrip("/")
        now = int(time.time())
        if path.endswith("/chat/completions"):
            if body.get("stream"):
                return self._stream(model, now)
            return self._send(200, {
                "id": "chatcmpl-stub", "object": "chat.completion", "created": now, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY},
                             "finish_reason": "stop", "logprobs": None}], "usage": USAGE})
        if path.endswith("/completions"):
            return self._send(200, {
                "id": "cmpl-stub", "object": "text_completion", "created": now, "model": model,
                "choices": [{"index": 0, "text": REPLY, "finish_reason": "stop", "logprobs": None}],
                "usage": USAGE})
        if path.endswith("/embeddings"):
            inputs = body.get("input", "")
            inputs = [inputs] if isinstance(inputs, str) else inputs
            return self._send(200, {"object": "list", "model": model, "usage": {"prompt_tokens": 5, "total_tokens": 5},
                                    "data": [{"object": "embedding", "index": i, "embedding": vec(str(t))}
                                             for i, t in enumerate(inputs)]})
        if path.endswith("/responses"):
            return self._send(200, {
                "id": "resp_stub", "object": "response", "created_at": now, "model": model, "status": "completed",
                "output": [{"type": "message", "id": "msg_stub", "status": "completed", "role": "assistant",
                            "content": [{"type": "output_text", "text": REPLY, "annotations": []}]}],
                "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}})
        self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def _stream(self, model, now):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for word in REPLY.split(" "):
            chunk = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": now, "model": model,
                     "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        final = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": now, "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
