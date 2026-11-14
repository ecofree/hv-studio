"""Repair two test harness assumptions exposed by the first Windows run."""
from pathlib import Path
import ast

p = Path('tests/test_episode_execution.py')
s = p.read_text('utf-8')
old = "root=tmp_path/'episodes';root.mkdir()"
assert s.count(old) == 1
s = s.replace(old, "root=tmp_path/'episodes';root.mkdir(exist_ok=True)")
ast.parse(s); p.write_text(s, encoding='utf-8', newline='\n')

p = Path('tests/test_api_auth_gate.py')
s = p.read_text('utf-8')
old = '''def test_all_write_routes_keep_shared_auth_dependency():
    from fastapi.routing import APIRoute
    from app.main import app
    def includes(dep):
        return dep.call is auth.require_token or any(includes(child) for child in dep.dependencies)
    routes=[r for r in app.routes if isinstance(r,APIRoute) and r.methods-{'GET','HEAD','OPTIONS'}]
    assert routes
    assert all(includes(route.dependant) for route in routes)
'''
new = '''def test_all_write_routes_keep_shared_auth_dependency(monkeypatch):
    # Exercise the public HTTP boundary, including lazily included routers.
    # An invalid token must reject every documented write before its handler.
    import re
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(auth, 'configured_token', lambda: 'test-only-token')
    writes = [(method, path) for path, operations in app.openapi()['paths'].items()
              for method in operations if method in {'post', 'put', 'patch', 'delete'}]
    assert writes
    with TestClient(app) as client:
        for method, path in writes:
            url = re.sub(r'\\{[^}]+\\}', 'offline-auth-fixture', path)
            response = client.request(method, url, json={}, headers={'X-HV-Token':'wrong'})
            assert response.status_code == 401, (method, path, response.text)
'''
assert s.count(old) == 1
s = s.replace(old, new)
ast.parse(s); p.write_text(s, encoding='utf-8', newline='\n')
