# Uploadz

Public site for the Uploadz TikTok integration: [Terms of Service](https://italovinicius18.github.io/uploadz/terms.html) and [Privacy Policy](https://italovinicius18.github.io/uploadz/privacy.html).

## Command-line uploader

`uploadz.py` publishes videos to your own TikTok account through the official Content Posting API (standard library only, Python 3.9+).

1. Put the app credentials in `~/.config/uploadz/credentials.env` (never in this repo):
   ```
   TIKTOK_CLIENT_KEY=...
   TIKTOK_CLIENT_SECRET=...
   ```
2. `uploadz login` and follow the link. The [callback page](https://italovinicius18.github.io/uploadz/callback.html) shows the code to paste back.
3. `uploadz whoami` shows the account and the privacy levels TikTok allows right now.
4. `uploadz post clip.mp4 --privacy SELF_ONLY --caption "text #tags"`

Unaudited (sandbox) apps can only post `SELF_ONLY`. Tests: `python3 -m unittest discover -s tests`.
