# Security

This repo and its GitHub Pages site are **public**. Anyone can read the code,
`paper_config.json` and the paper-trading results. Nothing secret is stored
in them.

## Where each secret lives

| Secret | Stored in | Who can read it |
|---|---|---|
| Anthropic / Alpaca keys, Google client secret + refresh token | GitHub Actions **secrets** (encrypted) | Only workflow runs on this repo. Not visible in the UI, logs or forks; pull requests from forks get no secrets |
| GitHub token for the Setup page | Your browser only: this tab, or this device if you tick *Remember* | You |
| Local bot keys (Claude, Gmail, Robinhood) | `~/.tradebot/config.enc` on your computer, AES-encrypted with your master password | You, with the password |

The Google connection uses the `drive.file` scope: it can only see files the
bot itself created, not the rest of your Drive.

## Things to keep doing

1. **Never put your Robinhood token in GitHub.** Paper mode doesn't need it.
   Real trading stays on your own computer.
2. **GitHub token:** fine-grained, this repo only, with an expiration
   (30–90 days). Don't tick *Remember on this device* on a shared computer. It
   can change this repo's code, so treat it like a password. Revoke it at
   github.com/settings/personal-access-tokens if it may have leaked.
3. **Turn on two-factor authentication** for GitHub, Google and Anthropic.
4. **Set a monthly spend limit** on your Anthropic API key (console.anthropic.com).
5. **Only open the dashboard from `https://<you>.github.io/robinhood-trade/`.**
   All your `*.github.io` sites share one browser storage area, so don't host
   untrusted pages there.
6. **Review pull requests from strangers before merging.** Merged code runs
   with your secrets.

## Protections built in

* **Site:** a Content-Security-Policy restricts scripts and network calls to
  GitHub, Google and one pinned library CDN. All data is HTML-escaped before
  display. Data-loading URL parameters work only on `localhost`.
* **Workflows:** the test workflow is read-only. The bot workflow runs only on
  schedule or on a manual trigger by someone with write access, and validates
  its inputs.
* **Local app:** listens only on 127.0.0.1 and refuses other host names
  (DNS-rebinding protection). The browser cookie holds only a random session
  ID; your password and keys stay in the app's memory. Cookies are
  `SameSite=Strict`. Demo mode can't change or wipe your settings.
