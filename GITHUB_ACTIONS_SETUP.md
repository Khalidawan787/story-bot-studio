# Roz ki videos GitHub par — PC band ho to bhi

Ab GitHub khud roz aap ki videos banata aur YouTube par upload karta hai.
Aap ka PC on ho ya na ho, bijli ho ya na ho — koi farq nahi padta.

## Chalane se pehle: ek dafa ka setup

```
.venv\Scripts\python.exe scripts\setup_github_actions.py
```

Yeh command aap ke PC ki private files (`.env`, OAuth clients, YouTube tokens)
ko pack kar ke GitHub ke **repository secret** `BOT_SECRETS_B64` mein daal deti
hai. Values kabhi screen par nahi aatin aur na hi command history mein jati
hain — seedha `gh secret set` ko pipe hoti hain.

**Jab bhi koi token ya key badle, yeh command dobara chala dein.**

## Test karein

```
gh workflow run daily-videos.yml
gh run watch
```

Ya GitHub par: repo → **Actions** → **Daily videos** → **Run workflow**

Pehli dafa `Build only` mode mein chala kar dekh lein (upload off):
Run workflow → **Upload to YouTube** ka tick hata dein.

## Roz kab chalta hai

```
cron: 30 2 * * *      →  02:30 UTC  =  subah 07:30 Pakistan
```

GitHub ka scheduler aksar 5–20 minute late hota hai — yeh normal hai.

Waqt badalna ho to `.github/workflows/daily-videos.yml` mein cron badal dein.

## Kya kya chalta hai

| Step | Kaam |
|---|---|
| Restore state | pichhle run ka database aur topics wapas laata hai |
| Install ffmpeg | Linux par ffmpeg + DejaVu font |
| Restore private files | secret se `.env`, tokens, client secrets nikaalta hai |
| quota-status | dikhata hai aaj kaunsa channel kitna upload kar sakta hai |
| daily-all | har channel ki Shorts banata aur upload karta hai |
| retry-uploads | jo reh gayi thin unhein upload karta hai |
| Save state | database wapas `bot-state` branch par push karta hai |
| Artifact | banayi hui videos 7 din tak download ke liye rakhta hai |

## State kahan rehti hai

Ek alag branch **`bot-state`** par, jismein sirf `bot.sqlite3` aur `data/*.json`
hote hain. Yeh zaroori hai: iske baghair har run khaali shuru hota, wahi topics
dobara chunta, aur wahi videos dobara upload kar deta.

Har run ek hi commit force-push karta hai, is liye repo roz 1 MB nahi barhta.

Yeh branch **delete na karein**.

## Shorts hi kyun, long videos kyun nahi

Ek Google Cloud project roz taqreeban **6 uploads** deta hai. Aap ke 3 connected
channels × 2 Shorts = 6. Long videos bhi shamil kar dein to yeh budget khatam ho
jayega aur uploads HTTP 429 se fail hone lagenge.

Long video chahiye to manually chalayein: Run workflow → **Also make one
~5-minute video per channel** ka tick laga dein. (Us din Shorts kam kar lein.)

## ⚠ 7 din wala masla — yeh sab se ahem hai

Kids channel ka naya Cloud project `youtube-data-api-501120` abhi Google ki
nazar mein **Testing** mode mein hai. Testing mode mein Google ka refresh token
**7 din baad expire** ho jata hai.

Yaani taqreeban har hafte:

1. GitHub ka run kids par fail karega (`invalid_grant`)
2. Aap ko apne PC par dashboard khol kar **Connect & verify** dabana hoga
3. Phir `scripts\setup_github_actions.py` dobara chalana hoga

Crime aur Trending purane project par hain jiske tokens hafton se chal rahe
hain, to woh mutasir nahi honge.

Isse hamesha ke liye bachne ka ek hi raasta hai: Google se app **verify**
karwana (privacy policy page, homepage aur ek demo video jama karna padta hai,
2–6 hafte lagte hain). Tab tak hafte mein ek dafa ka yeh chhota kaam karna hoga.

## Kharcha

Repo **private** hai, to GitHub Actions ke free 2,000 minute/mahina lagte hain.
Ek din ka run taqreeban 30–60 minute leta hai → mahine mein 900–1,800 minute.
Free tier ke andar hai, magar zyada margin nahi.

Kam karne ke tareeqe:
- Repo public kar dein → Actions bilkul free, koi limit nahi
  (magar code sab ko nazar aayega; secrets phir bhi mehfooz rehte hain)
- Ya `--count 1` kar dein taake har channel ki 1 Short bane

Kitne minute istemal huay dekhne ke liye:
GitHub → Settings → Billing → Plans and usage

## Masla ho to

```
gh run list --workflow daily-videos.yml     # pichhle runs
gh run view --log-failed                     # jo step fail hua uska log
```

Log mein har channel ka nateeja saaf likha hota hai, aur run ke aakhir mein
`quota-status` dobara chalta hai taake pata chale kya upload hua.

Banayi hui videos GitHub par run ke page se **Artifacts** mein download ho
sakti hain (7 din tak).

## PC wala dashboard band nahi hua

Dono saath chal sakte hain. PC ka dashboard jab chahein khol kar manually video
bana sakte hain — dono ek hi YouTube limits aur ek hi duplicate check use karte
hain. Bas dhyan rahe ke dono ki `bot.sqlite3` alag hai, is liye ek hi din dono
jagah se bohot saari videos na bana dein.
