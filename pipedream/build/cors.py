"""A tiny CORS-enabled static host so the Pipedream editor page can fetch
the workflow source instead of it being typed in keystroke by keystroke."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

class H(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()
    def log_message(self, *a):
        pass

ThreadingHTTPServer(("127.0.0.1", 8123), H).serve_forever()
