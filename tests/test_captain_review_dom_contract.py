"""Execute the DOM reader against a synthetic, secret-free JavaScript DOM facade.

No Chrome/profile/site is opened. This tests selector semantics, not live-site access.
"""

import json
import shutil
import subprocess

import pytest

from fpl_bot.captain_review_browser import _SNAPSHOT_JAVASCRIPT, _snapshot_page

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node required for offline DOM reader test")
HARNESS = r"""
const f = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const element = v => ({
  ...v, innerText: v.text || '', textContent: v.text || '',
  tagName: v.tag || 'BUTTON', className: v.classes || '',
  hidden: false, parentElement: null, value: v.value || '',
  classList: { contains: name => (v.classes || '').split(' ').includes(name) },
  getClientRects: () => v.visible === false ? [] : [{}],
  getAttribute: key => key === 'placeholder' ? v.placeholder : null,
  querySelectorAll: selector => (v.icons || []).filter(c =>
    c.split(' ').includes(selector.slice(4))),
});
const headers = f.headers.map(element);
const table = { parentElement: null, querySelectorAll: selector =>
  selector === 'thead tr' ? [{ querySelectorAll: () => headers }] : [] };
global.window = {getComputedStyle: () => ({display: 'block', visibility: 'visible'})};
global.document = {body: {innerText: ''}, querySelectorAll: selector => {
  if (selector === 'table') return [table];
  if (selector === 'input') return [element(f.search)];
  if (selector === 'button,[role="button"],[role="combobox"],select')
    return f.controls.map(element);
  throw Error('Unexpected selector');
}};
process.stdout.write(JSON.stringify(eval('(' + f.script + ')')()));
"""


def read_dom(*, icon="lucide lucide-chevron-down", active=True, search="", controls=None):
    fixture = {
        "script": _SNAPSHOT_JAVASCRIPT,
        "headers": [
            {
                "tag": "TH",
                "text": "GW8",
                "classes": "after:bg-fpl-accent" if active else "",
                "icons": [icon],
            },
            {"tag": "TH", "text": "TOTAL", "icons": ["lucide lucide-arrow-up-down"]},
        ],
        "controls": controls
        if controls is not None
        else [{"text": "All players"}, {"text": "£Min-£Max"}, {"text": "Points"}],
        "search": {"placeholder": "Search players...", "value": search},
    }
    result = subprocess.run(
        [NODE, "-e", HARNESS],
        input=json.dumps(fixture),
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    raw = json.loads(result.stdout)
    page = type("Page", (), {"evaluate": lambda _, script: raw})()
    return _snapshot_page(page)


@pytest.mark.parametrize(
    "icon,direction",
    [
        ("lucide lucide-chevron-down w-3 h-3 text-fpl-accent", "descending"),
        ("lucide lucide-chevron-up", "ascending"),
        ("lucide lucide-arrow-up-down", "none"),
        ("unknown-icon", "unrecognized"),
    ],
)
def test_inspected_header_markers_without_aria_sort(icon, direction):
    state = read_dom(icon=icon)
    header = state.tables[0].header_rows[0][0]
    assert header.sort_direction == direction
    assert header.sort_active is True
    assert state.tables[0].header_rows[0][1].sort_active is False
    assert state.scope.verified


def test_underline_is_required_independently_of_chevron():
    assert not read_dom(active=False).tables[0].header_rows[0][0].sort_active


def test_search_value_is_never_exported_and_filtered_scope_fails_closed():
    state = read_dom(search="PRIVATE_SEARCH_SENTINEL")
    assert not state.scope.search_empty
    assert "PRIVATE_SEARCH_SENTINEL" not in repr(state)


@pytest.mark.parametrize(
    "controls",
    [
        [{"text": "Defenders"}, {"text": "£Min-£Max"}, {"text": "Points"}],
        [{"text": "All players"}, {"text": "£4-£8"}, {"text": "Points"}],
        [{"text": "All players"}, {"text": "£Min-£Max"}, {"text": "Minutes"}],
        [{"text": "All players", "visible": False}, {"text": "£Min-£Max"}, {"text": "Points"}],
        [
            {"text": "All players"},
            {"text": "All players"},
            {"text": "£Min-£Max"},
            {"text": "Points"},
        ],
    ],
)
def test_unknown_hidden_or_ambiguous_scope_controls_are_rejected(controls):
    assert not read_dom(controls=controls).scope.verified
