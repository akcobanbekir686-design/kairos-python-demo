"""Private subprocess entry. Credentials/context arrive on stdin, never argv.
No logs, DB access or retries. Parent owns termination and reaping.
"""
import json
import sys
from providers import _perform_http, ProviderError

def main():
    try:
        args=json.loads(sys.stdin.buffer.read(1024*1024))
        result={'data':_perform_http(*args)}
    except ProviderError as exc:
        result={'error':{'code':exc.code,'status':exc.status,'retry_safe':exc.retry_safe}}
    except Exception:
        result={'error':{'code':'provider_invalid_response','status':502,'retry_safe':False}}
    sys.stdout.write(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
