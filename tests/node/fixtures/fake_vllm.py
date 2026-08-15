"""Stand-in for `vllm serve` used only by
tests/node/test_vllm_process.py's process-group-kill test. Spawns a child
process (mimicking vLLM's own worker subprocess) and serves the same
/health and /v1/chat/completions surface the real fake server fixture in
test_vllm_process.py implements, so this script can be driven through
VLLMProcess exactly like the real thing.
"""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[1])
pid_file = sys.argv[2]
child_pid_file = sys.argv[3]

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])

with open(pid_file, "w") as f:
    f.write(str(os.getpid()))
with open(child_pid_file, "w") as f:
    f.write(str(child.pid))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
