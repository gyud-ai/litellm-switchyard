"""Exercise the built container over real local HTTP, without external backends."""

import argparse
import json
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx


def free_port() -> int:
    """Reserve an ephemeral local port long enough to discover its number."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main(image: str) -> None:
    """Start a disposable gateway container and verify its real HTTP behavior."""
    requests: list[dict[str, Any]] = []
    fail_a = threading.Event()

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Keep synthetic backend access logs out of the test output."""

        def do_POST(self) -> None:
            """Record synthetic requests and return OpenAI-compatible responses."""
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append(
                {
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("authorization"),
                    "session": self.headers.get("x-session-id"),
                }
            )
            if self.path.startswith("/a/") and fail_a.is_set():
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            if body.get("stream"):
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                chunks = [
                    {"model": body["model"], "choices": [{"delta": {"content": "hello"}}]},
                    {"model": body["model"], "choices": [], "usage": {"total_tokens": 8}},
                ]
                for chunk in chunks:
                    data = ("data: " + json.dumps(chunk) + "\n\n").encode()
                    for part in (data[:7], data[7:]):
                        self.wfile.write(part)
                        self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "model": body["model"],
                            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                        }
                    ).encode()
                )

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{backend.server_port}"
    port = free_port()
    name = f"gateway-smoke-{uuid.uuid4().hex[:10]}"
    started = False
    try:
        with tempfile.TemporaryDirectory(prefix="gateway-smoke-") as directory:
            config = Path(directory) / "config.jsonc"
            config.write_text(
                json.dumps(
                    {
                        "server": {"api_key": "test-client-key", "host": "127.0.0.1", "port": port},
                        "models": {
                            "cheap": {
                                "model_id": "cheap-backend",
                                "endpoints": [
                                    {
                                        "name": label,
                                        "base_url": f"{base}/{label}/v1",
                                        "api_key": "test-backend-key",
                                    }
                                    for label in ("a", "b")
                                ],
                            },
                            "capable": {
                                "model_id": "capable-backend",
                                "endpoints": [
                                    {
                                        "name": "c",
                                        "base_url": f"{base}/c/v1",
                                        "api_key": "test-backend-key",
                                    }
                                ],
                            },
                        },
                        "pairs": {
                            "pair": {"capable": "capable", "efficient": "cheap"},
                            "pair-two": {"capable": "cheap", "efficient": "capable"},
                        },
                    }
                )
            )
            config.chmod(0o644)
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-d",
                    "--name",
                    name,
                    "--network",
                    "host",
                    "-v",
                    f"{config}:/app/config.jsonc:ro",
                    image,
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            started = True
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=15,
                headers={"authorization": "Bearer test-client-key", "x-session-id": "test-session"},
            ) as client:
                for _ in range(100):
                    try:
                        if client.get("/health/readiness").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError("container did not become ready")
                assert client.get("/v1/models").status_code == 200
                body = {
                    "model": "pair",
                    "messages": [{"role": "user", "content": "secret-prompt-marker"}],
                }
                for endpoint in ("a", "b"):
                    response = client.post("/v1/chat/completions", json=body)
                    assert response.status_code == 200, response.text
                    assert response.json()["model"] == "pair"
                    assert response.headers["x-gateway-endpoint"] == endpoint
                response = client.post("/v1/chat/completions", json=body | {"model": "pair-two"})
                assert response.headers["x-gateway-model"] == "capable"
                assert all(row["authorization"] == "Bearer test-backend-key" for row in requests)
                assert all(row["session"] == "test-session" for row in requests)
                fail_a.set()
                response = client.post("/v1/chat/completions", json=body)
                assert response.headers["x-gateway-endpoint"] == "b"
                assert [r["path"] for r in requests[-2:]] == [
                    "/a/v1/chat/completions",
                    "/b/v1/chat/completions",
                ]
                response = client.post("/v1/chat/completions", json=body | {"stream": True})
                assert response.status_code == 200
                assert '"model":"pair"' in response.text
                assert response.text.endswith("data: [DONE]\n\n")
                result_text = json.dumps(
                    [{"id": i, "status": "ok", "region": "west", "count": 1} for i in range(300)]
                )
                history = [
                    {"role": "system", "content": "retain-system"},
                    {"role": "user", "content": "fetch records"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call",
                                "type": "function",
                                "function": {"name": "fetch", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call", "content": result_text},
                    {"role": "assistant", "content": "done"},
                    {"role": "user", "content": "summarize"},
                ]
                response = client.post("/v1/chat/completions", json=body | {"messages": history})
                assert response.status_code == 200
                sent = requests[-1]["body"]["messages"]
                assert history[0] in sent and history[-1] in sent
                tool = next(row for row in sent if row["role"] == "tool")
                assert len(tool["content"]) < len(result_text)
                response = client.post(
                    "/v1/chat/completions",
                    json=body | {"messages": history},
                    headers={"x-headroom-bypass": "true"},
                )
                assert response.status_code == 200
                assert history[3] in requests[-1]["body"]["messages"]
                history[3]["content"] = "torch.cuda.OutOfMemoryError: CUDA out of memory"
                response = client.post(
                    "/v1/chat/completions", json=body | {"messages": history[:4]}
                )
                assert response.status_code == 200
                assert response.headers["x-gateway-model"] == "capable"
            log = subprocess.run(
                ["docker", "logs", name], check=True, capture_output=True, text=True, timeout=10
            )
            combined = log.stdout + log.stderr
            assert all(
                secret not in combined
                for secret in ("secret-prompt-marker", "test-client-key", "test-backend-key", base)
            )
            records = [json.loads(line) for line in log.stdout.splitlines()]
            assert any(record.get("compression") == "savings" for record in records)
            assert any(record.get("event") == "retry" for record in records)
            assert sum(record.get("event") == "request" for record in records) == 8
            print(
                json.dumps(
                    {
                        "container_smoke": "passed",
                        "http_requests": 8,
                        "routing": "both_pairs_and_escalation",
                        "streaming": "passed",
                        "compression": "savings_and_bypass",
                        "privacy": "passed",
                    }
                )
            )
    finally:
        if started:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="switchyard-gateway:check")
    main(parser.parse_args().image)
