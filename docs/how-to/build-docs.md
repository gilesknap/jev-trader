# Build the docs

The docs are Markdown (MyST) in `docs/`, built with Sphinx. Their dependencies are in the `docs`
dependency group, so the runtime install doesn't carry them.

Build locally, with warnings as errors, as the published build does:

```bash
uv run --group docs sphinx-build -W --keep-going docs build/html
```

Then open `build/html/index.html`. `build/` and `docs/_generated/` are git-ignored. The library
features table and the source map are generated from the code at build time, and the command-line
reference from the `trader` argument parser.

## Publishing

`.github/workflows/docs.yml` builds the docs on every push and pull request, and publishes them to
GitHub Pages on a push to `main`. The whole workflow runs only in a **public** repository: in a
private repository every job is skipped, since Pages there is either unavailable or public. So in
a private copy, build the docs locally as above.

To publish from a public repository, set **Settings → Pages → Source** to **GitHub Actions**.

## Writing

- Plain text only: the deploy tool refuses binary files and invisible or bidirectional Unicode
  characters. Draw diagrams in Mermaid (a `mermaid` code fence), never as images.
- Describe the code as it is; check a claim against `src/trader/` before writing it.
- Pages follow the [Diátaxis](https://diataxis.fr) split: tutorials, how-to guides, explanations
  and reference.
