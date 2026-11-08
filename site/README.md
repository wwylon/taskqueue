# Public marketing site (`site/`)

Static landing for GitHub Pages. **Not** the operator/help tree under `docs/`.

## Enable Pages (maintainer)

1. Push `site/` to public `main` (via house publish).
2. Repo **Settings → Pages**: Source = Deploy from a branch → `main` → folder **`/site`**.
3. Confirm https://wwylon.github.io/taskqueue/
4. `gh repo edit wwylon/taskqueue --homepage https://wwylon.github.io/taskqueue/`
5. **Settings → Social preview**: upload `../docs/assets/social.jpg` (or the copy `social.jpg` here).
6. [Google Search Console](https://search.google.com/search-console): add URL-prefix property
   `https://wwylon.github.io/taskqueue/`, verify, submit `sitemap.xml`, URL Inspection on `/`.

Assets (`icon.png`, `hero.jpg`, `social.jpg`) are copies of `docs/assets/` for Pages-relative URLs.
