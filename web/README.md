# Internship Scanner — Web (fully client-side)

A browser dashboard that scans Gmail for internship/co-op opportunities. Unlike a
normal webapp, **there is no backend**: the Gmail fetch, the rule-based filter, and
the LLM analysis all run **in the user's browser**. The only thing hosted is static
HTML/JS — email content never crosses a server.

```
Static host (Cloudflare Pages / Vercel)  ──serves JS only──▶  each user's browser
   Google OAuth (gmail.readonly, in-browser)
   Gmail REST  ──email──▶  browser only
   LLM:  WebLLM (WebGPU, in-browser)   or   the user's own local Ollama
   IndexedDB  ◀── scan cache + results + seen-set
```

This is the in-browser port of the local CLI in [`../cli/`](../cli/). The two share
nothing at runtime.

## Requirements (per user)

- A **WebGPU** browser (desktop Chrome/Edge, or Safari 18+) with ~8 GB+ RAM **for the
  WebLLM backend** — OR a local **Ollama** install for the Ollama backend.
- A Google account added as a **test user** on the OAuth consent screen (below).

## 1. Google Cloud OAuth setup (one-time)

1. [console.cloud.google.com](https://console.cloud.google.com/) → create/select a project.
2. **APIs & Services → Library →** enable **Gmail API**.
3. **OAuth consent screen:** User type **External**, publishing status **Testing**.
   Add each user (you + friends) under **Test users** (up to 100, no Google
   verification needed). Add the scope `.../auth/gmail.readonly`.
4. **Credentials → Create credentials → OAuth client ID →** Application type
   **Web application**. Under **Authorized JavaScript origins** add:
   - `http://localhost:3000` (local dev)
   - your deployed origin, e.g. `https://your-app.pages.dev`
5. Copy the **Client ID** (it is public by design — no client secret is used).

> Restricted Gmail scopes only require Google's (paid) security assessment to go
> *fully public*. In **Testing** mode with named test users, none of that applies —
> tokens just need periodic re-consent.

## 2. Local development

```bash
cd web
cp .env.local.example .env.local      # then paste your Client ID
npm install
npm run dev                           # http://localhost:3000
```

`.env.local`:

```
NEXT_PUBLIC_GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
```

## 3. Choosing an LLM backend (in the UI)

- **In-browser (WebLLM):** zero install. The app probes WebGPU/RAM and auto-selects a
  model (override in the dropdown). First run downloads the model (~1–5 GB, cached
  after). Needs WebGPU.
- **Local Ollama:** point it at your own `http://localhost:11434`. Email stays on your
  device. Because the deployed page is HTTPS, start Ollama allowing the site origin:

  ```bash
  OLLAMA_ORIGINS="https://your-app.pages.dev" ollama serve
  ```

  (Browsers exempt `http://localhost` from mixed-content blocking, so no proxy is
  needed.)

## 4. Deploy (free, static)

`npm run build` emits a static site to `web/out/`.

**Cloudflare Pages / Vercel:**
- Build command: `npm run build`
- Output directory: `out`
- Environment variable: `NEXT_PUBLIC_GOOGLE_CLIENT_ID`
- After the first deploy, add the deployed origin to the OAuth client's **Authorized
  JavaScript origins** (step 1.4).

> GitHub Pages works too, but if you serve from a sub-path (`/repo/`) set `basePath`
> in `next.config.ts`. Cloudflare Pages / Vercel serve at the root, so no change needed.

## 5. Onboard others

Add each person's Google address as a **test user** (step 1.3). They open the deployed
URL, sign in, pick a backend, and scan. Everything runs in their own browser.

## Privacy — how to verify

Open DevTools → **Network** during a scan. You should see requests **only** to Google
(`accounts.google.com`, `gmail.googleapis.com`) and, for WebLLM, the model CDN
(`huggingface.co` / `raw.githubusercontent.com`). **No** request carries email content
to any backend — that's the guarantee. With the Ollama backend, the only extra traffic
is to `localhost:11434` on the user's own machine.
