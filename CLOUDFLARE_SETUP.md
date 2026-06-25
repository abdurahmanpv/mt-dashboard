# Cloudflare Pages Setup Guide
## CEO Subscription Dashboard — One-time setup (~30 minutes)

This guide gets you from zero to a private, auto-updating dashboard URL that any stakeholder can open in a browser. No server, no Azure, no SharePoint.

---

## What you'll have at the end

| What | Details |
|---|---|
| Dashboard URL | `https://ceo-subscription-dashboard.pages.dev` (or a custom domain) |
| Auth | Email OTP — stakeholder types their email, gets a code, logs in |
| Updates | Automatic — every time `daily_refresh.py` runs and pushes the HTML, Cloudflare redeploys |
| Cost | Free (Cloudflare Pages free tier: unlimited requests, 1 build/minute) |

---

## Part 1 — Create a Cloudflare account (use the team email)

1. Go to [https://dash.cloudflare.com/sign-up](https://dash.cloudflare.com/sign-up)
2. Use the **team/shared email** (e.g. `datateamindia@way.com`) — not a personal email.
   This means the account survives personnel changes. Anyone with access to that inbox can reset the password.
3. Verify the email and log in.
4. On the left sidebar, note your **Account ID** — you'll need it later.
   It looks like: `4f8a1b2c3d4e5f6a7b8c9d0e1f2a3b4c`

---

## Part 2 — Create the Pages project

1. In the Cloudflare dashboard, click **Workers & Pages** in the left sidebar.
2. Click **Create** → **Pages** → **Connect to Git**.
3. Connect your GitHub account and select the repository that contains `CEO_Subscription_Dashboard.html`.
4. Configure the build:

   | Setting | Value |
   |---|---|
   | Project name | `ceo-subscription-dashboard` |
   | Production branch | `main` |
   | Build command | *(leave blank — no build step needed)* |
   | Build output directory | `/` (root) |

5. Click **Save and Deploy**. Cloudflare will deploy immediately from the current `main` branch.
6. Once deployed, your URL is: `https://ceo-subscription-dashboard.pages.dev`

---

## Part 3 — Restrict access with Cloudflare Access (email OTP)

This makes the dashboard private — only email addresses you approve can log in.

1. In Cloudflare dashboard, click **Zero Trust** in the left sidebar.
   (If you haven't used it before, it will ask you to create a Zero Trust org name — use e.g. `way-data`)
2. Go to **Access** → **Applications** → **Add an Application**.
3. Select **Self-hosted**.
4. Fill in:

   | Field | Value |
   |---|---|
   | Application name | `CEO Dashboard` |
   | Session duration | `24 hours` (stakeholders won't need to re-auth daily) |
   | Application domain | `ceo-subscription-dashboard.pages.dev` |

5. Click **Next** → configure the policy:

   | Field | Value |
   |---|---|
   | Policy name | `Team Access` |
   | Action | Allow |
   | Include rule | Emails → list specific emails (e.g. CEO, CFO) |

   Alternatively use **Email domain** = `way.com` to allow anyone with a `@way.com` address.

6. Click **Next** → under **Authentication methods**, ensure **One-time PIN** is enabled.
   This means: user visits the URL → types their email → receives a 6-digit code → logged in.
   No password management required.

7. Click **Add application**. Done.

Now when a stakeholder visits the URL, they'll see a Cloudflare login screen, enter their email, and get a code. The dashboard loads after they enter it.

---

## Part 4 — Add GitHub Secrets for auto-deploy

The GitHub Actions workflow (`deploy_dashboard.yml`) needs two secrets to push to Cloudflare.

### 4a — Create a Cloudflare API Token

1. In Cloudflare dashboard → top-right avatar → **My Profile** → **API Tokens**.
2. Click **Create Token**.
3. Use template **Edit Cloudflare Workers** → then customise:
   - Permissions: `Cloudflare Pages — Edit`
   - Account Resources: your account
4. Click **Continue to summary** → **Create Token**.
5. **Copy the token now** — you won't see it again.

### 4b — Add secrets to GitHub

1. In your GitHub repo → **Settings** → **Secrets and variables** → **Actions**.
2. Add two secrets:

   | Name | Value |
   |---|---|
   | `CLOUDFLARE_API_TOKEN` | the token you just copied |
   | `CLOUDFLARE_ACCOUNT_ID` | your Cloudflare Account ID (from Part 1, step 4) |

---

## Part 5 — Connect HTML generation to the deploy

The `build_dashboard.py` already generates `CEO_Subscription_Dashboard.html` alongside the xlsx. You need to make sure the daily GitHub Actions job **commits and pushes that file** after generation.

In your existing `daily_refresh.yml` (GitHub Actions), add these steps after the Python script runs:

```yaml
      - name: Commit updated dashboard
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add CEO_Subscription_Dashboard.html
          git diff --cached --quiet || git commit -m "chore: refresh dashboard $(date -u +%Y-%m-%d)"

      - name: Push HTML
        uses: ad-m/github-push-action@v0.8.0
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          branch: main
```

The push to `main` triggers `deploy_dashboard.yml`, which deploys the new HTML to Cloudflare Pages within ~30 seconds.

---

## What happens on a typical day

```
3:00 AM PST  →  daily_refresh.yml runs on GitHub Actions
                  MySQL query → compute KPIs → write .xlsx + .html
                  git commit CEO_Subscription_Dashboard.html
                  git push → triggers deploy_dashboard.yml
3:02 AM PST  →  deploy_dashboard.yml deploys new HTML to Cloudflare Pages
                  stakeholders see fresh data when they open the URL
```

---

## If you leave the org

The Cloudflare account is tied to `datateamindia@way.com`. Whoever controls that inbox can:
1. Log in to Cloudflare → My Profile → Change email
2. Or add a new team member as an org member in the Zero Trust team settings

The GitHub secrets (`CLOUDFLARE_API_TOKEN`) can be rotated by any repo admin in 2 minutes.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Deployment fails in GitHub Actions | Check that `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are correct in repo secrets |
| Stakeholder can't log in | In Cloudflare Access → Applications → `CEO Dashboard` → Policies, make sure their email or domain is in the Include rule |
| Dashboard shows old data | The HTML was not committed/pushed by the daily job — check `daily_refresh.yml` logs |
| Chart doesn't appear | Browser is offline or Chart.js CDN is blocked — data tables still work, chart requires internet |
