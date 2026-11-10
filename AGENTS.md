# Notes for agents working on Bongo Matrix v3

- v3 is a ground-up rewrite. v2 lives on `archive/v2`: read it for behaviour
  and assets, never copy its structure back in.
- Keep it small. Prefer one clear file over a framework. If a change needs
  the core to know about a specific module, the design is wrong.
- Every screen is a module folder in `bongo/modules/` (see docs/MODULES.md).
  Variants of the same screen are styles of one module, not new modules.
- Frames are 64×32 RGB Pillow images. Check visual work with
  `python -m bongo preview <module> --style <style>` and look at the GIF.
- Run `python -m pytest -q` before committing. The contract tests cover every
  module and style automatically.
- Track the remaster in docs/ROADMAP.md.
