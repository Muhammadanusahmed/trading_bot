# Running the bot 24/7 on a cloud server (Oracle Cloud Free Tier)

These steps put the same four scripts on a small Linux server that never turns off. If a script crashes it restarts by itself, and everything starts again automatically after a server reboot.

Why not Vercel: it only runs short web requests, doesn't keep files like `trade_state.json`, and its servers run in the US by default, where Binance blocks connections.

## 1. Create the server (Oracle Cloud Always Free)

1. Sign up at oracle.com/cloud/free. It asks for a card to verify you, but Always Free resources aren't charged.
   - **Home region:** pick one outside the US, for example **Germany Central (Frankfurt)**, **UAE East (Dubai)** or **India West (Mumbai)**. You can't change this later.
2. In the console go to **Compute → Instances → Create instance**:
   - Image: **Canonical Ubuntu 24.04**
   - Shape: **VM.Standard.A1.Flex** (Ampere, 1 OCPU and 6 GB is plenty) or **VM.Standard.E2.1.Micro**. Both are marked "Always Free eligible".
   - SSH keys: choose **Generate a key pair for me** and click **Save private key**. Keep that file safe; it's your only way in.
3. Click **Create**, wait until the instance shows **Running**, then copy its **Public IP address**.

If it says the A1 shape is "out of capacity", try the E2.1.Micro shape or another availability domain.

In PowerShell, move the key into your bot folder's parent and lock it down (Windows refuses keys other users can read):
```
move "$HOME\Downloads\ssh-key-*.key" "D:\HamzaKhan\AI Agent\oracle.key"
icacls "D:\HamzaKhan\AI Agent\oracle.key" /inheritance:r /grant:r "$($env:USERNAME):R"
```

## 2. Stop the bot on your laptop

Press **Ctrl+C** in each of the four PowerShell windows. Only one copy of the bot should ever run, or both will trade at the same time.

While you're in PowerShell, print your testnet keys and email password so you can paste them into the server later:
```
echo $env:EXCHANGE_API_KEY
echo $env:EXCHANGE_API_SECRET
echo $env:SMTP_USER
echo $env:SMTP_PASSWORD
```

## 3. Upload the bot folder

In PowerShell on your laptop (replace `SERVER_IP`):
```
scp -i "D:\HamzaKhan\AI Agent\oracle.key" -r "D:\HamzaKhan\AI Agent\crypto-trend-bot" ubuntu@SERVER_IP:/tmp/
```
Type `yes` if it asks about the fingerprint. This also copies `trade_state.json`, so the server knows about the testnet position that's already open.

## 4. Run the setup

Log in to the server:
```
ssh -i "D:\HamzaKhan\AI Agent\oracle.key" ubuntu@SERVER_IP
```
Then:
```
sudo mv /tmp/crypto-trend-bot /opt/
cd /opt/crypto-trend-bot
sudo bash deploy/setup_server.sh
```

## 5. Add your keys and start

```
sudo nano /etc/crypto-bot.env
```
Replace the `your_...` values with the keys you printed in step 2. Save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**. Then start the bot:
```
sudo bash deploy/setup_server.sh
```
It should list four `crypto-...` services as **active (running)**.

You can now close the window and turn off your laptop. The bot keeps running.

## Everyday commands

Log in with `ssh -i "D:\HamzaKhan\AI Agent\oracle.key" ubuntu@SERVER_IP`, then put `sudo` in front of each command:

| What | Command |
|---|---|
| Watch trades live (Ctrl+C stops watching, not the bot) | `journalctl -u crypto-execution_engine -f` |
| See what the decision agent thinks | `journalctl -u crypto-decision_agent -n 20` |
| Check all four are running | `systemctl status 'crypto-*' --no-pager` |
| Trade log | `tail /opt/crypto-trend-bot/data/trade_log.jsonl` |
| Stop everything | `systemctl stop crypto-data_collector crypto-anomaly_detector crypto-decision_agent crypto-execution_engine` |
| Start everything again | `systemctl start crypto-data_collector crypto-anomaly_detector crypto-decision_agent crypto-execution_engine` |

**Updating a file:** upload it from your laptop, for example
`scp -i "D:\HamzaKhan\AI Agent\oracle.key" "D:\HamzaKhan\AI Agent\crypto-trend-bot\decision_agent.py" ubuntu@SERVER_IP:/tmp/`
then on the server run `sudo cp /tmp/decision_agent.py /opt/crypto-trend-bot/` and `sudo bash /opt/crypto-trend-bot/deploy/setup_server.sh` to restart everything.

## If something goes wrong

- `$'\r': command not found` when running the setup: Windows changed the line endings. Fix with `sed -i 's/\r$//' deploy/setup_server.sh`, then run it again.
- `UNPROTECTED PRIVATE KEY FILE` or `Permission denied (publickey)`: rerun the `icacls` line from step 1, and make sure you're logging in as `ubuntu`, not `root`.
- A service keeps restarting: `journalctl -u crypto-execution_engine -n 50` shows the error. Paste it to Claude.
- Moving back to the laptop: stop the services on the server first, then copy `trade_state.json` back.
