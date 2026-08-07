# Ek channel par kai YouTube channels

Ab dashboard ka har channel (kids, crime, trending …) **kai YouTube channels**
par upload kar sakta hai. Har juda hua YouTube channel yahan ek **account**
kehlata hai.

## Copyright/duplicate ka masla kaise hal hota hai

Har account ko **apne alag topics** milte hain. Topic history poore channel ki
ek hi hoti hai, aur daily batch ek account ka kaam mukammal karne ke baad hi
agle account ke topics chunta hai — is liye jo topic ek YouTube channel par ja
chuka, woh doosre ko kabhi nahi milta.

Test se sabit:

```
main   gets: fun_numbers_0022, fun_animals_0036
abc    gets: fun_numbers_0023, fun_animals_0037
total topics: 4 | unique: 4   →  koi video do channels par nahi jati
```

## Dashboard se (aasan tareeqa)

**Create Videos** tab kholein. Upar ek naya box hai:
**"YouTube channels connected to <channel>"**

1. Neeche wale form mein bharein:
   - **Account id** — chhota sa naam, jaise `abc` (lowercase, spaces nahi)
   - **YouTube channel name** — jo aap ko yaad rahe, jaise `Kids ABC World`
   - **Own client_secret file** — khaali chhor dein (neeche quota wala hissa parhein)
2. **Add YouTube channel** dabayein
3. Nayi row par **Connect** dabayein → Google window khulegi →
   **wohi YouTube channel chunein jo is account ke liye hai**
   (ghalat channel chunne se videos ghalat jagah chali jayengi)
4. Bas. Agli daily run se dono channels par alag alag videos jayengi.

Kisi bhi waqt row se **Disconnect** ya **Remove** kar sakte hain.
`main` account remove nahi hota — woh channel ka asli account hai.

## Command line se

```
python -m src.cli accounts list   --channel kids
python -m src.cli accounts add    --channel kids --account abc --name "Kids ABC World"
python -m src.cli authorize       --channel kids --account abc
python -m src.cli accounts remove --channel kids --account abc
```

`accounts list` aisa dikhata hai:

```
  ACCOUNT      TODAY    NAME                TOKEN                  CONNECTED
  main         2/2      Kids Learning       token.json             yes
  abc          0/2      Kids ABC World      token_kids_abc.json    no
```

## Roz ki limit

Har account ki apni limit hoti hai. `.env` mein:

```
YOUTUBE_UPLOAD_DAILY_LIMIT_KIDS=2          # kids ke har account ke liye
YOUTUBE_UPLOAD_DAILY_LIMIT_KIDS_ABC=3      # sirf kids/abc ke liye
```

Format: `YOUTUBE_UPLOAD_DAILY_LIMIT_<CHANNEL>_<ACCOUNT>`

## Quota — yeh zaroor parhein

Upload quota **Google Cloud project** ko milti hai, YouTube channel ko nahi.

- Jo accounts ek hi `client_secret` use karte hain, woh **ek hi quota** baantte hain
- Kisi account ko apni alag quota deni ho to usay apna client_secret dein:
  1. Naya Cloud project banayein (steps: `ADD_MORE_UPLOAD_QUOTA.md`)
  2. Uska JSON is folder mein rakhein, jaise `client_secret_kids_abc.json`
  3. Account add karte waqt **Own client_secret file** mein wohi naam likhein
  4. Phir us account ko dobara Connect karein

Kaunsa account kis project par hai, yeh dekhne ke liye:

```
python -m src.cli quota-status
```

```
CHANNEL      ACCOUNT    TODAY    CLOUD PROJECT                CONNECTED
kids         main       2/2      your-shared-project    yes
kids         abc        0/2      kids-abc-uploader            yes
```

## Kya nahi badla

- Purani har video `main` account ki mani jati hai
- Agar aap koi account add nahi karte, sab kuch bilkul pehle jaisa chalta hai
- Har account ki apni analytics, apna token, apni limit
