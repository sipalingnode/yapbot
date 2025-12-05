# yapbot
## Hal yang perlu di siapkan
1. Mengambil apikey dari OpenAI: https://platform.openai.com/settings/organization/api-keys (buy $5)
2. Mengambil Cookies Twitter (Gunakan Inspect element) lalu edit pada file [cookies.json](https://github.com/sipalingnode/yapbot/blob/main/README.md#edit-file-cookiesjson)

```
sudo apt update && sudo apt upgrade -y
```
```
sudo apt install -y python3 python3-pip
```
```
sudo apt install -y git
```
```
git clone https://github.com/sipalingnode/yapbot.git
```
```
cd yapbot
```
```
python3 -m venv venv
```
```
source venv/bin/activate
```
```
pip3 install -r requirements.txt
```
```
playwright install chromium
```

## Edit File .env
```
nano .env
```
Save dengan CTRL + X → Y → ENTER.

## Edit File Cookies.json
```
nano cookies.json
```
Save dengan CTRL + X → Y → ENTER.

## Run Bot
```
source venv/bin/activate
```
```
python3 main.py
```
