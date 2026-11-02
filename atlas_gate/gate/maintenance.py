"""Root-controlled admission barrier; never an authoritative old-command receipt."""
from pathlib import Path

FLAG = Path('/run/atlas-gate-maintenance')
ADMISSION_FLAG = Path('/run/atlas-gate-admission')


def blocks(request):
    path = request.url.path
    return (request.method == 'POST' and (FLAG.is_file() or ADMISSION_FLAG.is_file()) and
            (path == '/harness/agent/sessions' or
             (path.startswith('/harness/agent/sessions/') and path.endswith('/prompt')) or
             path.startswith('/harness/llm/v1/')))
