# Har channel ko apni YouTube upload quota kaise dein

## Masla kya hai

YouTube Data API upload quota **Google Cloud project** ko milti hai, channel ko nahi.

- Ek project = **10,000 units/din**
- Ek video upload = **1,600 units**
- Yaani **~6 uploads/din per project**, chahe us project se kitne bhi channels juday hon

Abhi saare channels ek hi project `valley-fulfillment-488503` use kar rahe hain,
is liye teenon channels mil kar sirf 6 videos/din upload kar sakte hain.

Project badalne ka faida **nahi** hai — naye project ko bhi wahi 6 milenge.
Faida sirf **project add karne** se hai.

## Abhi ki halat dekhne ke liye

```
python -m src.cli quota-status
```

## Ek channel ko apne project par shift karna (ek dafa ka kaam)

Maan lein aap `crime` channel ko alag karna chahte hain:

1. https://console.cloud.google.com par jayein → upar project dropdown →
   **New Project** → naam rakhein jaise `crime-uploader` → Create

2. Naye project ke andar: **APIs & Services → Library** → "YouTube Data API v3"
   search karein → **Enable**

3. **APIs & Services → OAuth consent screen**
   - User Type: **External** → Create
   - App name, support email, developer email bhar dein → Save
   - **Audience → Test users → Add users**: apna Gmail add karein
     (warna authorize karte waqt "app not verified" par atak jayega)

4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app** → Create → **Download JSON**

5. Downloaded file ko is folder mein rakh dein, naam bilkul yeh ho:

   ```
   client_secret_crime.json
   ```

   (format hamesha `client_secret_<channel-id>.json` — jaise
   `client_secret_kids.json`, `client_secret_trending.json`)

6. Purana token delete karein aur dobara authorize karein:

   ```
   del token_crime.json
   python -m src.cli authorize --channel crime
   ```

   Browser khulega → **usi channel ka** YouTube account chunein.
   Ghalat account chunne se videos ghalat channel par chali jayengi.

7. `.env` mein us channel ki limit barha dein:

   ```
   YOUTUBE_UPLOAD_DAILY_LIMIT_CRIME=6
   ```

8. Confirm karein:

   ```
   python -m src.cli quota-status
   ```

   Ab `crime` ke saamne naya project name dikhna chahiye.

9. Dashboard restart kar dein (START_DASHBOARD.bat).

## Zaroori baatein

- **Code change ki zaroorat nahi.** System khud `client_secret_<channel>.json`
  dhoondta hai; na mile to shared `client_secret.json` use karta hai. Is liye
  aap ek waqt mein ek channel shift kar sakte hain, kuch nahi tootega.
- Har project ki limit **6/din** hi rahegi. Us se zyada chahiye to us project ke
  liye Google se quota increase request karni hogi
  (APIs & Services → YouTube Data API v3 → Quotas → edit → request).
- Jo channels ek project share kar rahe hain, unki `.env` limits ka **jama 6 se
  zyada na ho**, warna HTTP 429 aayega. `quota-status` yeh khud check karke
  warning deta hai.
