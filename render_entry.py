"""Render Free ephemeral integration service. Session issuance stays server-side."""
import os
import threading
from http.server import ThreadingHTTPServer
from mobile_python import MobileHandler
from mobile_demo import DemoSessions

SITE = "https://kairos-aeon-mobil-onizleme.akcobanbekir686.chatgpt.site"

class Handler(MobileHandler):
    def end_headers(self):
        if self.path.startswith('/demo/') and self.headers.get('Origin') == SITE:
            self.send_header('Access-Control-Allow-Origin', SITE)
            self.send_header('Vary', 'Origin')
        super().end_headers()

    def do_OPTIONS(self):
        if not self.path.startswith('/demo/') or self.headers.get('Origin') != SITE:
            self.reply(403, {'error':'origin'})
            return
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Max-Age', '600')
        self.end_headers()

    def demo(self):
        if self.headers.get('Origin') not in (None, SITE):
            self.reply(403, {'error':'origin'})
            return
        self.server.demos.handle(self)

    def do_POST(self):
        if self.path.startswith('/demo/'):
            return self.demo()
        super().do_POST()

    def do_DELETE(self):
        if self.path.startswith('/demo/'):
            return self.demo()
        super().do_DELETE()

    def do_GET(self):
        if self.path.startswith('/demo/'):
            return self.demo()
        if self.path == '/healthz':
            self.reply(200, {'status':'ok'})
            return
        super().do_GET()

def serve():
    with ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','10000'))),Handler) as server:
        server.db_path = '/tmp/kairos-demo.sqlite'
        server.demos = DemoSessions()
        server.create_lock = threading.Lock()
        server.serve_forever()

if __name__ == '__main__':
    serve()
