"""Media library pagination: 10 items per page by default, with 20/30/40/50 options."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "app" / "templates" / "dashboard.html"

PROBE = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const pick = name => {
  const start = source.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('missing ' + name);
  let depth = 0, index = source.indexOf('{', start);
  for (; index < source.length; index++) {
    if (source[index] === '{') depth++;
    if (source[index] === '}' && --depth === 0) break;
  }
  return source.slice(start, index + 1);
};
const constant = name => {
  const match = source.match(new RegExp('const ' + name + '=[^;]+;'));
  if (!match) throw new Error('missing ' + name);
  return match[0];
};
const store = new Map(JSON.parse(process.argv[2]));
global.localStorage = {getItem: key => store.has(key) ? store.get(key) : null, setItem: (key, value) => store.set(key, String(value))};
const code = [constant('MEDIA_PAGE_SIZES'), constant('MEDIA_PAGE_SIZE_KEY'), pick('mediaPage'), pick('readMediaPageSize'), pick('writeMediaPageSize')].join(';');
eval(code + ';globalThis.MEDIA_PAGE_SIZES=MEDIA_PAGE_SIZES;globalThis.mediaPage=mediaPage;globalThis.readMediaPageSize=readMediaPageSize;globalThis.writeMediaPageSize=writeMediaPageSize;');
const items = Array.from({length: 23}, (_, index) => 'item' + index);
const view = result => ({...result, items: result.items.slice(0, 1).concat(result.items.slice(-1)), count: result.items.length});
console.log(JSON.stringify({
  sizes: MEDIA_PAGE_SIZES,
  storedSize: readMediaPageSize(),
  first: view(mediaPage(items, 0, 10)),
  last: view(mediaPage(items, 2, 10)),
  beyond: view(mediaPage(items, 9, 10)),
  negative: view(mediaPage(items, -4, 10)),
  twenty: view(mediaPage(items, 1, 20)),
  empty: mediaPage([], 3, 10),
  invalidSize: view(mediaPage(items, 0, 7)),
  written: (writeMediaPageSize(30), readMediaPageSize()),
  rejected: (writeMediaPageSize(999), readMediaPageSize()),
}));
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class MediaPaginationTests(unittest.TestCase):
    def probe(self, stored=()):
        result = subprocess.run(
            ["node", "-e", PROBE, str(DASHBOARD), json.dumps(list(stored))],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return json.loads(result.stdout)

    def test_page_sizes_default_to_ten_with_larger_options(self):
        report = self.probe()
        self.assertEqual(report["sizes"], [10, 20, 30, 40, 50])
        self.assertEqual(report["storedSize"], 10)

    def test_pages_slice_items_and_clamp_out_of_range_pages(self):
        report = self.probe()
        self.assertEqual(report["first"], {"items": ["item0", "item9"], "count": 10, "page": 0, "pages": 3, "start": 1, "end": 10, "total": 23, "size": 10})
        self.assertEqual(report["last"], {"items": ["item20", "item22"], "count": 3, "page": 2, "pages": 3, "start": 21, "end": 23, "total": 23, "size": 10})
        self.assertEqual(report["beyond"], report["last"])
        self.assertEqual(report["negative"], report["first"])
        self.assertEqual(report["twenty"]["count"], 3)
        self.assertEqual(report["twenty"]["pages"], 2)

    def test_empty_groups_and_invalid_sizes_are_safe(self):
        report = self.probe()
        self.assertEqual(report["empty"], {"items": [], "page": 0, "pages": 1, "start": 0, "end": 0, "total": 0, "size": 10})
        self.assertEqual(report["invalidSize"]["size"], 10)
        self.assertEqual(report["invalidSize"]["count"], 10)

    def test_chosen_page_size_is_remembered_and_unknown_values_ignored(self):
        report = self.probe()
        self.assertEqual(report["written"], 30)
        self.assertEqual(report["rejected"], 30)
        self.assertEqual(self.probe([["matrix.mediaPageSize", "50"]])["storedSize"], 50)
        self.assertEqual(self.probe([["matrix.mediaPageSize", "12"]])["storedSize"], 10)

    def test_library_renders_pager_controls(self):
        body = DASHBOARD.read_text()
        for text in ("id=\"media-page-size\"", "Items per page", "'Previous page'", "'Next page'", "aria-live", "media-pager"):
            self.assertIn(text, body)
        self.assertIn("mediaPageState", body)


if __name__ == "__main__":
    unittest.main()
