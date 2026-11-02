"""Root-controlled admission barrier; never an authoritative old-command receipt."""
from pathlib import Path

FLAG = Path('/run/atlas-gate-maintenance')


def blocks(request):
    path = request.url.path
    return (request.method == 'POST' and FLAG.is_file() and
            (path == '/harness/agent/sessions' or
             (path.startswith('/harness/agent/sessions/') and path.endswith('/prompt'))))
