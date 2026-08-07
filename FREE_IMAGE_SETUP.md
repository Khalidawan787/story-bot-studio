# Free image generation setup (kids channel ke liye cartoon images)

> **Status: setup ho chuka hai (5 Aug 2026).** Cloudflare Workers AI `.env`
> mein configure hai aur kids channel ki cartoon images bana raha hai.
> Yeh guide reference ke liye hai — agar token kabhi badalna ho ya kisi doosre
> PC par setup karna ho.

## Masla

Kids channel ko **cartoon illustrations** chahiye — asli photos nahi. Is liye
Pexels/Openverse jaisi stock photo services kids ke liye use nahi hotin. Kids ko
sirf AI se banayi hui tasveer chahiye, aur abhi:

- **OpenAI** — "Billing hard limit has been reached" (band)
- **Pollinations** — HTTP 429/500 (free anonymous access ab rate-limited hai)

Is liye kids ke videos ruk rahe hain.

## Hal: Cloudflare Workers AI (free, credit card ki zaroorat nahi)

Cloudflare roz free image generation deta hai (FLUX / SDXL models), aur yeh
**cartoon art banata hai**, photos nahi — bilkul wahi jo kids channel ko chahiye.

### Steps (5 minute)

1. https://dash.cloudflare.com/sign-up par free account banayein
   (sirf email + password, card nahi maanga jata)

2. Login ke baad **Account ID** copy karein:
   - Left menu mein kisi bhi section par jayein (jaise **Workers & Pages**)
   - Right side / URL mein 32 characters ka Account ID milega
   - Ya URL dekhein: `dash.cloudflare.com/<YEH-AAP-KA-ACCOUNT-ID>/...`

3. **API token** banayein:
   - Upar right corner → profile icon → **My Profile**
   - **API Tokens** → **Create Token**
   - Neeche **Custom token** → **Get started**
   - Permissions: **Account** → **Workers AI** → **Read** aur phir ek aur row
     mein **Account** → **Workers AI** → **Edit**
     (agar sirf ek option mile to **Edit** chun lein)
   - **Continue to summary** → **Create Token**
   - Token sirf ek dafa dikhega — **copy kar lein**

4. Dono values `.env` file mein daal dein:

   ```
   CLOUDFLARE_ACCOUNT_ID=yahan-account-id
   CLOUDFLARE_API_TOKEN=yahan-token
   ```

   (Ya file mein rakh dein: `data/cloudflare_account_id.txt` aur
   `data/cloudflare_api_token.txt` — dono tareeqe chalte hain.)

5. Check karein ke kaam kar raha hai:

   ```
   python -m src.cli test-images --channel kids
   ```

   `Cloudflare (free)  OK` aana chahiye.

6. Dashboard restart kar dein (START_DASHBOARD.bat).

## Provider ki tarteeb (system khud is order mein try karta hai)

| # | Provider | Kis channel ke liye | Cost |
|---|----------|---------------------|------|
| 1 | OpenAI (gpt-image) | sab | paid — abhi billing limit par band |
| 2 | **Cloudflare Workers AI** | **sab, kids samet** | **free** |
| 3 | Pollinations | sab | free — abhi rate-limited |
| 4 | Pexels stock photos | sirf genre channels (trending/crime/love/horror/motivation) | free |
| 5 | Openverse / Wikimedia | sirf genre channels | free |

Kids channel sirf 1, 2, 3 use kar sakta hai — is liye Cloudflare zaroori hai.

## Doosra free option: Pollinations token

Agar Cloudflare nahi karna to Pollinations ka free token bhi chalta hai
(code mein support pehle se maujood hai):

1. https://auth.pollinations.ai par free token banayein (`pk_...` se shuru hota hai)
2. Dashboard → API section → Pollinations key field mein paste karein
   (ya `data/pollinations_api_key.txt` mein rakh dein)

Magar Cloudflare zyada bharosemand hai — Pollinations ka free tier aksar
429/500 deta rehta hai.

## Har waqt check karne ka tareeqa

```
python -m src.cli test-images --channel kids
python -m src.cli test-images --channel trending
```

Yeh batata hai kaunsa provider chal raha hai aur kaunsa nahi — video banane se
pehle hi pata chal jata hai.
