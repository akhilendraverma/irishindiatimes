# IrishIndiaTimes: a daily news site for Indians in Ireland

A small, free-to-run newspaper website. Every morning it:

1. pulls headlines from Irish, Indian and Asian news feeds (RSS/Atom),
2. keeps the stories that matter to Indians in Ireland and the wider Asian community,
3. removes duplicates and anything older than 36 hours,
4. publishes a newspaper-style page that links each story to its source,
5. adds a **Sources & Disclaimer** footer listing every publisher cited that day,
6. saves a copy to the **Archive**.

It uses only the Python standard library, so there is nothing to install.

## Sections

| Section | Where the stories come from |
|---|---|
| Indians in Ireland | Google News searches (Indian community, visas, the embassy, Diwali in Irish cities, etc.) |
| Immigration, Visas & Living | RTÉ, The Irish Times, Irish Independent and TheJournal.ie, filtered by keywords (visa, work permit, Indian, Asian, rent…) |
| Ireland: Top Stories | RTÉ, The Irish Times, Irish Independent |
| India | The Hindu, The Indian Express |
| हिंदी समाचार | Dainik Jagran |
| Asia | BBC Asia, plus Al Jazeera filtered to Asian countries |
| UK & World | BBC News, Sky News, Yahoo News |

No section shows more than 4 stories from one publisher (`max_per_source`), so a single outlet can't take over a section. If a publisher's direct feed is broken or empty, a feed can name a `fallback` URL (a Google News search for that publisher's site) to use instead.

You can change all of this in `config.json`.

## Go live in about 10 minutes (free, on GitHub Pages)

1. Create a new **public** repository on GitHub and upload every file in this folder, including the hidden `.github` folder.
2. In the repo, open **Settings → Pages**. Under *Build and deployment*, choose **Deploy from a branch**, then branch `main` and folder `/docs`.
3. Open the **Actions** tab, pick **Daily edition**, and click **Run workflow** to publish the first edition.
4. Your site will appear at `https://akhilendraverma.github.io/<repo-name>/`.

After that, a new edition is published every day at 06:15 UTC. To use your own domain, add it under Settings → Pages → Custom domain.

## Run it on your own computer

```bash
python build.py            # live feeds, writes docs/
python build.py --sample   # offline preview with placeholder stories
```

Open `docs/index.html` in a browser to see the result. Requires Python 3.9 or newer.

## Customising

- **Name, tagline, contact email:** the `site` block in `config.json`. Put a real contact email there before you go live.
- **Add a feed:** add `{ "source": "...", "homepage": "...", "url": "..." }` to a section.
- **Keyword filtering:** set `"keywords"` on a section, or on a single feed, and only matching stories are kept. Matching ignores case and accepts word prefixes, so `racis` matches both "racism" and "racist".
- **New Google News topic:** copy one of the `news.google.com/rss/search?q=...` URLs and change the query.
- **Styling:** `static/style.css`.

If a feed is down, the build skips it and carries on. If *every* feed fails, the build stops and the previous edition stays online.

## Copyright and the disclaimer

The site shows only headlines, a short excerpt (trimmed to about 240 characters) and a link to the original article. That is the normal way to use RSS feeds. Even so:

- Check each publisher's RSS terms, especially before you add ads or charge for the site. Some feeds, including Google News RSS, are offered for personal or non-commercial use.
- Keep the footer disclaimer and a working contact email so publishers can ask for removal.
- If you later want original reporting or summaries, write them yourself. Don't copy full articles.
