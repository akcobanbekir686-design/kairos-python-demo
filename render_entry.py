"""Render Free ephemeral integration service. Session issuance stays server-side."""
import os
import threading
from http.server import ThreadingHTTPServer
from mobile_python import MobileHandler

class Handler(MobileHandler):
    def do_GET(self):
        if self.path == '/healthz':
            self.reply(200, {'status':'ok'})
            return
        super().do_GET()

def serve():
    with ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','10000'))),Handler) as server:
        server.db_path = '/tmp/kairos-demo.sqlite'
        server.create_lock = threading.Lock()
        server.serve_forever()

if __name__ == '__main__':
    serve()
