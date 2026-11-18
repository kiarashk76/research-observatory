# Third-party components

The MIT license in this repository covers the platform code, not every dependency under the same terms.

The Notes/Chat frontend uses Tiptap, ProseMirror, KaTeX, Markdown-It, markdown-it-texmath and their dependencies. The build uses esbuild; browser tests/PDF exports use Playwright and a separately downloaded Chromium browser. Their package lockfile records exact dependency versions and package integrity. Bundled frontend code preserves esbuild's linked legal comments in `labcore/dashboard/notes-vendor/notes-editor.js.LEGAL.txt`.

KaTeX fonts and code include their original notices; see `katex-LICENSE.txt` and the dependency package for any additional font notices. Additional primary package licenses are copied here for convenient distribution of the bundled assets. Transitive licenses remain in the installed package distributions and the generated bundle's legal comments.

Python dependencies, Claude Code, Slack services, GPU/CUDA software, Poppler, Tailscale and Caddy retain their own licenses/terms. This repository does not redistribute account access or grant rights to those services, research papers, model weights or third-party datasets. Install optional runtime binaries using their own distribution instructions.
