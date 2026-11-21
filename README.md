# sm_digest

Reddit + YouTube digest, rebuilt every 2 days by GitHub Actions and published with GitHub Pages.

## Setup
1. **Secrets** (repo Settings > Secrets and variables > Actions):
   - `ANTHROPIC_API_KEY` (required)
   - `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` (recommended; Reddit often blocks anonymous requests from GitHub's servers. Create a "script" app at https://www.reddit.com/prefs/apps)
   - `YOUTUBE_API_KEY` (optional; adds exact video length and view counts)
2. **YouTube subscriptions**: put `subscriptions.csv` from Google Takeout in the repo root
   (takeout.google.com > deselect all > YouTube and YouTube Music > "subscriptions"), or list channel IDs in `channels.txt`.
3. **Pages**: Settings > Pages > Deploy from a branch > `main` / `/docs`.
4. **Run it**: Actions tab > "Build digest" > Run workflow. After that it runs automatically (checks daily, builds every ~2 days).

Edit `config.json` to change subreddits, counts, and your taste descriptions.
