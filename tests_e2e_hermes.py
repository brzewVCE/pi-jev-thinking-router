"""E2E: Hermes middleware chain -> jev-router core daemon (fallback path)."""
import sys, time, threading
sys.path.insert(0, '/opt/data/workspace/jev-router-core/core/src')

from jevrouter.app import create_app
from jevrouter.config import load_config
import uvicorn

cfg = load_config('/opt/data/workspace/jev-router-core/examples/config.example.yaml')
srv = uvicorn.Server(uvicorn.Config(create_app(cfg), host='127.0.0.1', port=8792, log_level='error'))
threading.Thread(target=srv.run, daemon=True).start()
time.sleep(2)

from hermes_cli.plugins import get_plugin_manager
m = get_plugin_manager()
import importlib.util
spec = importlib.util.spec_from_file_location('jev_router_adapter', '/opt/data/plugins/jev-router/__init__.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

class Ctx:
    def register_middleware(self, kind, cb):
        m._middleware.setdefault(kind, []).append(cb)

mod.register(Ctx())
print('registered kinds:', sorted(m._middleware.keys()))

from hermes_cli.middleware import apply_llm_request_middleware
req = {"model": "current-model", "messages": [{"role": "user", "content": "napraw bug w logowaniu"}]}
res = apply_llm_request_middleware(req, session_id='test-1', turn_id=1)
print('AFTER MW model:', res.payload.get('model'))
print('AFTER MW reasoning_effort:', res.payload.get('reasoning_effort'))
print('changed:', res.changed)
print('trace:', res.trace)

# tool_request path: delegate_task child routing
from hermes_cli.middleware import apply_tool_request_middleware
treq = {"task": "przeszukaj repo i napraw testy", "goal": "fix tests"}
tres = apply_tool_request_middleware('delegate_task', treq, session_id='test-1')
print('CHILD args model:', tres.payload.get('model'))
print('CHILD changed:', tres.changed)
print('E2E DONE')
