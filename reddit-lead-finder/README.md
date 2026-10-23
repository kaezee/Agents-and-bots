# Reddit lead finder

Watches chosen subreddits for posts and comments where one of your products
would genuinely help, and sends each match to a Discord channel for that
product. **It only alerts.** It never posts, votes or replies on Reddit: you
read the thread and write every reply yourself.

- Reads Reddit's public RSS feeds, so no API key or Reddit app approval needed.
- Python 3.11+, standard library only: nothing to install, costs nothing.
- Run it on your own computer. Reddit often blocks cloud IPs (GitHub Actions etc.).

## Setup (about 5 minutes)

1. **Discord.** Make a server with one channel per product, starting with
   `#augur`. Channel settings → Integrations → Webhooks → New Webhook → Copy URL.
2. **Secrets.** Copy `.env.example` to `.env` and paste the webhook URL in.
   `.env` is git-ignored; never commit it.
3. **Config.** In `products.toml`, put your Reddit username in `user_agent` and
   in `ignore_authors`.
4. **Try it:**
   ```sh
   python3 bot.py --once --dry-run --backfill   # prints what it would send
   python3 bot.py                               # runs, polling every 5 minutes
   ```

The first real run records what's already in the feeds and alerts on nothing,
so you aren't flooded with old threads. From then on you get only new matches.
`seen.json` remembers what it has already looked at; delete it to start over.

To run it in the background, leave it running in a terminal, or schedule
`python3 bot.py --once` every 5 minutes (cron on Mac/Linux, Task Scheduler on
Windows).

## How matching works

Each product lists subreddits in two modes:

- `keyword_subreddits`: new posts and new comments, alerted only when a
  keyword matches (title + body for posts, the comment text for comments).
- `every_post_subreddits`: every new post alerts (e.g. beta-tester subs).

Keywords and excludes are case-insensitive whole phrases, so `NPS` doesn't
match "snapshots". Anything containing an exclude word is skipped in both modes.
Each alert shows the subreddit, thread title, matching keyword and its group,
the first lines, and a link.

## Adding a product

Copy the Augur `[[product]]` block in `products.toml`, fill in every field, make
a new Discord channel and webhook, and add it to `.env` under the new
`webhook_env` name. Add the next product once the Augur channel shows at least
one in five alerts is worth replying to.

## Replying well

The bot finds threads; whether a reply helps or reads as spam is up to you.
Only reply where your product actually answers the question, say it's yours,
and follow each subreddit's self-promotion rules.

## Tests

```sh
python3 -m unittest
```
