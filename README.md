# sm_digest (YouTube)

Picks the videos worth watching from your subscribed channels every ~2 days and publishes them with GitHub Pages.

## Setup
1. Repo secrets (Settings > Secrets and variables > Actions): `OPENROUTER_API_KEY`, `YOUTUBE_API_KEY`.
2. Put `subscriptions.csv` (Google Takeout > YouTube > subscriptions) in the repo root, or list channel IDs in `channels.txt`.
3. Settings > Pages > Deploy from a branch > `main` / `/docs`.
4. Actions tab > "Build digest" > Run workflow. After that it runs automatically.

Edit `config.json` for your taste/counts, `channel_rules.json` for per-channel rules, and `MODEL` in `.github/workflows/digest.yml` to change the model.

