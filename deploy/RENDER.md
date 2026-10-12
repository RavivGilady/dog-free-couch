# Free hosting on Render (+ Neon + Cloudflare R2)

Render runs the server from the `Dockerfile` and gives you HTTPS. Its disk is
wiped on every redeploy, so the database lives on Neon (Postgres) and clips on
Cloudflare R2 (S3-compatible). All three have free tiers; check their current
terms.

## 1. Neon (database)

1. Sign up at neon.tech and create a project.
2. Copy the **connection string** (`postgresql://user:pw@host/db?sslmode=require`).
   The server rewrites the scheme itself.

## 2. Cloudflare R2 (clips)

1. Cloudflare dashboard > **R2** > create a bucket, e.g. `dog-free-couch`.
   (R2 asks for a card to enable, but the free allowance is generous.)
2. **R2 > Manage API tokens** > create a token with _Object Read & Write_ on
   that bucket. Note the **Access Key ID**, **Secret Access Key** and the
   account's S3 endpoint `https://<account-id>.r2.cloudflarestorage.com`.

## 3. Render

1. Push this repo to GitHub.
2. Render dashboard > **New > Blueprint**, pick the repo. It reads `render.yaml`.
3. Fill in the prompted values:

   | Variable                                      | Value                  |
   | --------------------------------------------- | ---------------------- |
   | `DATABASE_URL`                                | Neon connection string |
   | `S3_BUCKET`                                   | bucket name            |
   | `S3_ENDPOINT_URL`                             | R2 endpoint            |
   | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | R2 token               |

   `SECRET_KEY` is generated for you.

4. Deploy. Your site is at `https://dog-free-couch.onrender.com` (or similar).
   Create your account, then add `ALLOW_SIGNUP=0` under Environment once your
   household is registered.

## 4. Point cameras at it

```bash
python agent.py --server https://<your-service>.onrender.com --token dfc_...
```

Or use a browser tab as the camera from **Devices**.

## Caveats

- The free web service sleeps after ~15 minutes without requests. A running
  agent or open dashboard keeps it awake; the first request after a sleep
  takes up to a minute.
- Run one instance only (the live view relay is in memory); the free plan is
  one instance anyway.
- Neon also pauses idle databases and wakes them on the next query.
