"""Local bearer-session reference. Issuance is trusted, not an HTTP endpoint."""
import hashlib
import secrets
import time
from contextlib import contextmanager
from core import Core
from origin import Origin

class Unauthorized(Exception): pass

def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

class SessionStore(Origin):
    def __init__(self,path):
        super().__init__(path)
        self.db.execute('''CREATE TABLE IF NOT EXISTS sessions(
          token_digest TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL)''')

    def issue_session(self,owner,ttl=3600):
        # Only a trusted verified-login/new-start coordinator may call this.
        if not isinstance(owner,str) or not owner or not 0<ttl<=86400: raise ValueError('session')
        token=secrets.token_urlsafe(32)
        with Core.atomic(self):
            self.db.execute('INSERT INTO sessions VALUES(?,?,?)',
                            (token_digest(token),owner,time.time()+ttl))
        return token

class AuthorizedStore(SessionStore):
    def __init__(self,path,token):
        self.credential=token_digest(token)
        self.request_owner=None
        super().__init__(path)
        try:
            with self.atomic(): pass  # Early rejection; actual operation checks again.
        except BaseException:
            self.close(); raise

    @contextmanager
    def atomic(self):
        # Authorization and the entire inherited mutation use the same writer lock.
        with Core.atomic(self):
            row=self.db.execute('SELECT owner,expires_at FROM sessions WHERE token_digest=?',
                                (self.credential,)).fetchone()
            if row is None or row['expires_at']<=time.time(): raise Unauthorized()
            if self.request_owner is not None and row['owner']!=self.request_owner: raise Unauthorized()
            self.request_owner=row['owner']
            yield
