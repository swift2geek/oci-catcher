# oci-catcher

Ловит бесплатный ARM-сервер в Oracle Cloud (Always Free `VM.Standard.A1.Flex`). Свободных мощностей под A1 почти всегда нет, и консоль отвечает `Out of host capacity`. Скрипт стучится в API раз в 2 минуты, по очереди перебирает availability domains региона и, как только сервер создан, присылает в Telegram готовую команду `ssh`.

- Лимит Always Free с 15.06.2026 — **2 OCPU / 12 GB** на аккаунт (на PAYG бесплатно 4 / 24).
- Если сервер с таким `DISPLAY_NAME` уже работает, скрипт ничего не создаёт и выходит.
- На `429 TooManyRequests` отступает от 5 до 30 минут, на ошибках конфигурации останавливается и пишет в Telegram.
- Поймать сервер может за полчаса, а может за несколько дней — оставь крутиться.

Нужна любая машина с Docker, которая всегда включена: домашний сервер, NAS, Raspberry Pi, другой VPS.

## 1. Аккаунт

Регистрация на https://signup.oraclecloud.com/.

- Почта, телефон и банковская карта — свои. Одна бесплатная учётка на человека, карту, уже привязанную к другому аккаунту Oracle, отклонят.
- **Home Region** выбирается один раз и навсегда, бесплатный A1 дают только в нём. Бери регион с тремя AD, например Germany Central (Frankfurt): скрипту есть что перебирать.
- На карте временно заблокируют около 1 €. На PAYG не переходи, если не решил это осознанно: обратно на Free Tier не откатить.

## 2. Сеть

Консоль → Networking → Virtual Cloud Networks → **Start VCN Wizard** → *Create VCN with Internet Connectivity* → дай имя → Create. Мастер создаст сеть с публичным и приватным сабнетом, нужен публичный (`public subnet-<имя>`).

## 3. API-ключ

Консоль → иконка профиля справа сверху → My profile → **API keys** (в новой консоли — вкладка Tokens and keys) → Add API key → *Generate API key pair* → **Download private key** → Add.

Консоль покажет превью конфига. Положи его в папку `oci/` в корне репо:

```
oci/
├── config
└── oci_api_key.pem   ← скачанный приватный ключ, переименованный
```

В `oci/config` строку `key_file=<path to your private keyfile> # TODO` замени на путь внутри контейнера:

```ini
[DEFAULT]
user=ocid1.user.oc1..…
fingerprint=…
tenancy=ocid1.tenancy.oc1..…
region=eu-frankfurt-1
key_file=/root/.oci/oci_api_key.pem
```

Папка `oci/` и `.env` в `.gitignore`, в git не попадут.

## 4. `.env`

```bash
git clone https://github.com/swift2geek/oci-catcher.git
cd oci-catcher
cp .env.example .env
```

OCID проще всего достать в **Cloud Shell** (иконка `>_` в шапке консоли): там `oci` CLI уже залогинен.

```bash
T=ocid1.tenancy.oc1..…   # tenancy= из oci/config

# OCI_SUBNET_ID — id строки «public subnet-…»
oci network subnet list -c $T --query 'data[].{name:"display-name",id:id}' --output table

# OCI_IMAGE_ID — свежая Ubuntu 24.04 под ARM (в имени должно быть aarch64)
oci compute image list -c $T --operating-system "Canonical Ubuntu" --operating-system-version 24.04 \
  --shape VM.Standard.A1.Flex --sort-by TIMECREATED --sort-order DESC --all \
  --query 'data[0].{name:"display-name",id:id}' --output table
```

Заполни в `.env`:

| Переменная | Что это |
|---|---|
| `OCI_COMPARTMENT_ID` | `tenancy=` из `oci/config` |
| `OCI_SUBNET_ID` | id публичного сабнета |
| `OCI_IMAGE_ID` | id образа |
| `SSH_PUBLIC_KEY` | твой публичный ключ целиком, `cat ~/.ssh/id_ed25519.pub` (нет ключа — `ssh-keygen -t ed25519`) |
| `TELEGRAM_BOT_TOKEN` | токен бота от [@BotFather](https://t.me/BotFather); пусто — без уведомлений, только логи |
| `TELEGRAM_CHAT_ID` | напиши боту (или добавь его в группу), открой `https://api.telegram.org/bot<TOKEN>/getUpdates` и возьми `"chat":{"id":…}` |

Остальное можно не трогать.

## 5. Запуск

```bash
docker compose up -d --build
docker compose logs -f
```

В Telegram придёт `🎯 hunting…`, раз в 6 часов — `⏳ still hunting`. Строки вида `500 InternalError: Out of host capacity` в логах — норма, это и есть ожидание.

На Synology и других системах, где docker только от root: `sudo docker compose …`.

## 6. Поймал

Придёт `✅ free-vps RUNNING` с командой `ssh ubuntu@<ip>`. Контейнер сам завершится с кодом 0 и не перезапустится. Убрать его:

```bash
docker compose down
```

Чтобы открыть сайт на 80/443:

1. Консоль → твоя VCN → Security Lists → Default Security List → Add Ingress Rules: source `0.0.0.0/0`, TCP, отдельно порт `80` и порт `443`.
2. На самом сервере: в образах Ubuntu от Oracle входящие закрыты правилом `REJECT` в iptables. Разрешающие правила надо вставить **перед** ним (на свежем образе `REJECT` стоит пятым — проверь `sudo iptables -L INPUT --line-numbers`):

```bash
sudo iptables -I INPUT 5 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 5 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo netfilter-persistent save
```

## Грабли

- **Простаивающие серверы Oracle забирает.** Always Free инстанс считается простаивающим, если за 7 дней загрузка CPU (95-й перцентиль), сети и памяти ниже 20%. Совсем пустой сервер могут отобрать.
- **Не запускай тестово через `timeout … docker compose run`.** `timeout` убивает только клиент, контейнер продолжает ловить в фоне и может создать второй сервер. Для пробы: `docker compose up`, потом `docker compose down`.
- **`🛑 fatal, stopping`** — ошибка конфигурации, а не мощностей: `NotAuthorizedOrNotFound` — неверный OCID или ключ, `LimitExceeded` — лимит A1 в аккаунте уже занят. Контейнер перезапустится максимум 3 раза и остановится.
- Скрипт видит только серверы в состоянии PROVISIONING / STARTING / RUNNING с тем же `DISPLAY_NAME`. Остановленный (Stopped) или переименованный сервер он не заметит и попробует создать ещё один.
