# Our own stories

Each article is one file in this folder.

1. Copy `_TEMPLATE.md` and rename the copy, for example `2026-11-10-diwali-in-dublin.md`.
   The file name becomes the web address: `irishindiatimes.com/news/2026-11-10-diwali-in-dublin.html`.
2. Fill in `title`, `date` (YYYY-MM-DD) and `author`. `summary` is optional but recommended.
3. Optional photo: put the image in `stories/images/` and write its file name after `image:`.
   Only use photos you took yourself or have permission to use.
4. Write the article below the `---` line.
5. Commit and push. The story appears on the next edition, or straight away with
   Actions -> Daily edition -> Run workflow.

Stories show in "From our newsroom" on the front page for 14 days (`newsroom_days` in config.json)
and stay on the "Our stories" page for good. Files starting with `_` are ignored.
