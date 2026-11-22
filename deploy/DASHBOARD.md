# Dashboard aur seekhne wala agent: server par lagana

Dashboard ek chhoti web app hai jo server par `crypto-dashboard` service ban kar chalti hai. Ye bot ki files sirf **parhti** hai (systemd ne poori disk us ke liye read-only kar di hai), is liye bot ko chhed nahi sakti. Ye sirf server ke andar `127.0.0.1:8787` par sunti hai; bahar se link **Tailscale** deta hai (HTTPS ke saath), is liye internet par koi aur ise dekh bhi nahi sakta.

`learning_agent` 5th bot service hai: losses aur roz ki market ko Google Gemini (free) se parhta hai aur sabaq `data/lessons.jsonl` mein likhta hai. Ye bot ke rules **kabhi khud nahi badalta**.

## 1. Naya code server par (har update par yahi)

Laptop par (CMD) pehle push:
```
cd /d "D:\PRIVATE\oracle_server_project\project\HamzaAnusTradingBot"
git add -A
git commit -m "Dashboard aur learning agent"
git push origin main
```
Server par (ssh ke baad):
```
cd ~/trading_bot
GIT_SSH_COMMAND="ssh -i ~/.ssh/trading_bot_deploy" git pull
sudo rsync -a --exclude .git ~/trading_bot/ /opt/crypto-trend-bot/
cd /opt/crypto-trend-bot
sudo sed -i 's/\r$//' deploy/setup_server.sh
sudo bash deploy/setup_server.sh
```
`rsync` purana data (`data/`, `trade_state.json`) nahi mitata. Setup script bot ki services restart karta hai (paper position file mein save rehti hai) aur dashboard start karta hai. Agar likha aaye **"Port 8787 is already used"** to aap ka doosra project wo port le raha hai: `sudo nano /etc/crypto-bot.env` mein `DASHBOARD_PORT=8788` kar ke script dobara chalayen.

## 2. Login password set karna

```
sudo /opt/crypto-trend-bot/.venv/bin/python /opt/crypto-trend-bot/dashboard/set_password.py
sudo systemctl restart crypto-dashboard
```
Username aur password (kam az kam 10 harf) poochega. Password type karte waqt screen par nahi dikhega, ye normal hai. File mein sirf password ka encrypted hash jata hai. Password bhool jayen to yahi dobara chala dein.

Check:
```
systemctl is-active crypto-dashboard
curl -s localhost:8787/api/me
```
`active` aur `{"user":null}` aaye to dashboard chal raha hai.

## 3. Tailscale: phone aur laptop par link

**Server par:**
```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```
Ek link aayega, use browser mein khol kar Google/GitHub/Microsoft se Tailscale account banayen (free). Phir:
```
sudo tailscale serve --bg 8787
```
Agar ye kahe ke HTTPS on nahi, to jo link deta hai use khol kar **Enable HTTPS** dabayen aur command dobara chalayen. Aakhir mein ek link dikhayega, jaise:
`https://maaktradingagent.tail1234.ts.net/` ← yahi aap ka dashboard link hai. (`tailscale serve status` se dobara dekh sakte hain.)

**Laptop par:** tailscale.com/download se Windows app install karein, **usi account** se login, phir link browser mein kholen.

**Phone par:** Play Store / App Store se **Tailscale** app, usi account se login aur VPN on. Phir Chrome (Android) ya Safari (iPhone) mein link kholen, login karein, aur:
- Android Chrome: ⋮ menu → **Add to Home screen / Install app**
- iPhone Safari: Share button → **Add to Home Screen**

Ab icon se app ki tarah khulega. Jis device par Tailscale nahi, wahan link nahi khulega: yahi hifazat hai.

Tailscale aap ke doosre project ya us ke ports ko nahi chhedta, aur server par koi naya port internet ke liye nahi khulta.

## 4. Gemini key (seekhne wala system)

1. aistudio.google.com par Google account se login → **Get API key** → **Create API key** → copy.
2. Server par: `sudo nano /etc/crypto-bot.env` → `GEMINI_API_KEY=` ke aage paste (spaces ke bagair). Ctrl+O, Enter, Ctrl+X.
3. `sudo systemctl restart crypto-learning_agent crypto-dashboard`

Key chat mein kisi ko na bhejein. Free tier mein Google bheja gaya text apne AI ko behtar karne ke liye use kar sakta hai, is liye agent sirf trade aur market data bhejta hai, keys ya emails kabhi nahi. Pehli dafa agent 2017 se ab tak ka BTC data download karke purani losses parhta hai (kuch minute). Roz ki market report subah 8 baje (Pakistan) banti hai. Rozana zyada se zyada 20 calls (`GEMINI_MAX_CALLS_PER_DAY`).

Agar Gemini model ka naam badal jaye aur Seekhna page par error aaye: `GEMINI_MODEL=` mein aistudio.google.com par dikhne wala free "flash" model ka naam likh dein.

## Roz ke kaam ke commands

| Kya | Command |
|---|---|
| Dashboard chal raha hai? | `systemctl status crypto-dashboard --no-pager` |
| Dashboard ka log | `journalctl -u crypto-dashboard -n 30 --no-pager` |
| Seekhne wale agent ka log | `journalctl -u crypto-learning_agent -n 30 --no-pager` |
| Dashboard ka link dobara dekhna | `tailscale serve status` |
| Password badalna | upar step 2 |

## Masle

- **Login par "Server par password set nahi"**: step 2 chalayen, phir `sudo systemctl restart crypto-dashboard`.
- **"Bohat ghalat koshishen"**: 5 ghalat password ke baad 15 minute lock. Intezar karein.
- **Phone par link nahi khulta**: phone ki Tailscale app mein VPN on hai aur wahi account hai? `tailscale status` server par dono devices dikhaye.
- **Agents page par learning_agent "Not installed"**: `sudo bash deploy/setup_server.sh` dobara chalayen.
