"""Mock OpenAI-compatible chat server for testing the LLM hook + fixer."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def _extract_rows(user_msg, marker):
    start = user_msg.find("[", user_msg.find(marker))
    return json.loads(user_msg[start:user_msg.rfind("]") + 1])


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        user_msg = body["messages"][-1]["content"]

        if "Dataset analysis JSON:" in user_msg:
            reply_text = (
                "## Summary\n\nQuality is driven mainly by empty worker "
                "outputs. **Recommended:** re-run failed tasks and add a "
                "template-format rule to the rubric.")
            resp = json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": reply_text}}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp.encode())
            return

        if "Rows to correct:" in user_msg:
            rows = _extract_rows(user_msg, "Rows to correct:")
            out = [{"row_num": r["row_num"],
                    "corrections": {
                        f: f"[AI-corrected {f} for row {r['row_num']}]"
                        for f in r.get("flagged_fields", [])}}
                   for r in rows]
        else:
            rows = _extract_rows(user_msg, "Flagged rows to judge:")
            out = []
            for r in rows:
                if r["row_num"] == 2:
                    out.append({"row_num": 2, "verdict": "dismissed",
                                "rationale": "Documented worker timeout; empty output expected.",
                                "resolution": "Route to worker retry queue."})
                elif r["row_num"] == 5:
                    out.append({"row_num": 5, "verdict": "unclear",
                                "rationale": "Rubric lacks a template-format rule.",
                                "resolution": "Add a format rule to the rubric."})
                else:
                    out.append({"row_num": r["row_num"], "verdict": "confirmed",
                                "rationale": "Clear deviation from the rubric.",
                                "resolution": "Fix the flagged field."})

        resp = json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": f"```json\n{json.dumps(out)}\n```"}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(resp.encode())

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", 5399), Handler).serve_forever()
