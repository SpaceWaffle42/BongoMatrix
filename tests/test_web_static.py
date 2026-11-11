"""The dashboard's static files are served by the Flask app and hold the expected views."""
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from bongo.live import Live
from bongo.pairing import Pairing
from bongo.registry import discover
from bongo.web import STATIC, create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(modules=discover(), live=Live(), pairing=Pairing(tmp_path / "paired.json"),
                     config_path=tmp_path / "config.json")
    return app.test_client()


def test_shell_is_served_for_the_root_and_for_app_routes(client):
    for path in ("/", "/edit/2", "/settings", "/logs", "/pair", "/playlist"):
        response = client.get(path)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        for marker in ('id="view"', 'id="sidenav"', 'id="tabbar"', 'id="toasts"', 'id="draft-apply"', 'src="js/app.js"', 'href="app.css"'):
            assert marker in html, (path, marker)


def test_assets_are_served(client):
    css = client.get("/app.css")
    assert css.status_code == 200 and "--amber: #FFB547" in css.get_data(as_text=True)
    app_js = client.get("/js/app.js").get_data(as_text=True)
    for route in ("now.js", "editor.js", "settings.js", "logs.js", "pair.js"):
        assert route in app_js
        assert client.get(f"/js/{route}").status_code == 200
    assert "export function drawLed" in client.get("/js/led.js").get_data(as_text=True)
    assert "/api/frame.png" in client.get("/js/now.js").get_data(as_text=True)


def test_index_html_is_well_formed():
    class Checker(HTMLParser):
        VOID = {"meta", "link", "br", "img", "input", "hr", "source", "base"}

        def __init__(self):
            super().__init__()
            self.stack = []

        def handle_starttag(self, tag, attrs):
            if tag not in self.VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            assert self.stack and self.stack[-1] == tag, (tag, self.stack)
            self.stack.pop()

    checker = Checker()
    checker.feed((STATIC / "index.html").read_text())
    assert checker.stack == []


def test_demo_assets_cover_every_module_style_and_stay_small():
    modules = json.loads((STATIC / "js" / "demo-modules.json").read_text())
    modules += json.loads((STATIC / "js" / "demo-proposed.json").read_text())
    if not (STATIC / "demo" / "index.json").exists():
        pytest.skip("demo sprite sheets are generated for published demos, not kept in the repo")
    index = json.loads((STATIC / "demo" / "index.json").read_text())
    for module in modules:
        for style in module["styles"]:
            assert f"{module['id']}/{style}" in index["sheets"]
    total = sum(p.stat().st_size for p in Path(STATIC / "demo").iterdir()) + (STATIC / "js" / "demo-modules.json").stat().st_size
    assert total < 1_000_000  # ~80 animated styles (proposed modules included), 16 frames each


def test_phone_tab_bar_stays_on_one_row():
    css = (STATIC / "app.css").read_text()
    rule = css[css.index("  .tabbar {"):].split("}", 1)[0]
    assert "grid-auto-flow: column" in rule and "repeat(4" not in rule
