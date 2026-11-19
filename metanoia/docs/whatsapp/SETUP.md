# WhatsApp: connecting the business number

The LMS side is built and switched off (`WHATSAPP_ENABLED=False`). While it is off nothing is sent, the inbox just stores what staff type, and registration does not ask for a code. These are the steps to switch it on, in order.

## What it does once it's on

| Feature | Where |
|---|---|
| Staff inbox: every chat with the number, replies, files, assigning, closing | Staff desk → **WhatsApp** (`/staff/whatsapp/`), admin and staff |
| Bot quick answers: WEEK, DATES, MODULES, INVOICE, NOTICES, AGENT, SWITCH, STOP | Automatic; anything else goes to the inbox |
| Number verification with a 6-digit code at registration | Student, staff, parent and educator registration |
| Notification copies (per person, opt-in under Settings → Notifications) | Automatic, with a template per category |
| Registration invoice PDF | Automatic after registration |

**Shared numbers.** Several accounts can use one number (for example a parent and a child). The bot asks who is writing and labels every answer with that person's name. Every notification greets the person it is about by full name.

## 1. Before you start

1. **The HTTPS domain must be live.** Meta only sends incoming messages to an `https://` address. Run the domain switch first (`deploy/server/switch-to-domain.sh`, see the brand and domain notes).
2. **Take the number off the WhatsApp app.** If the LMS is to be the only inbox, the business number (`<whatsapp-number>`) must not be registered in the WhatsApp or WhatsApp Business app when you add it to the API. In the app, go to Settings → Account → **Delete account**. Back up any chats you want to keep first. The SIM still has to receive one SMS or call for the code in step 3.

## 2. Meta Business account

1. Go to <https://business.facebook.com>. Create or open the **Metanoia Group** business portfolio.
2. Security Centre → **Start verification**. Upload the company registration (CIPC) documents and a utility bill or bank letter that shows the business name and address. Approval usually takes a few days.

## 3. The app and the number

1. Go to <https://developers.facebook.com> → My Apps → **Create app**. Choose type **Business**, attached to the Metanoia Group portfolio.
2. Add the **WhatsApp** product. Meta creates a WhatsApp Business Account (WABA) and a test number.
3. WhatsApp → API Setup → **Add phone number**. Enter the business number (`<whatsapp-number>`), display name **Metanoia Group**, category **Education**. Verify it with the SMS or voice code.
4. Note two ids from API Setup: the **Phone number ID** and the **WhatsApp Business Account ID**.
5. Display name: Meta reviews it. It must match the business name or website.

## 4. A permanent token

1. Business settings → Users → **System users** → Add. Name it `metanoia-lms` with the **Admin** role.
2. **Add assets** → the app (Full control) and the WhatsApp account (Full control).
3. **Generate token**. Choose the app, set expiry to **Never**, and tick `whatsapp_business_messaging` and `whatsapp_business_management`.
4. Copy the token now, because Meta shows it only once.

## 5. Settings on the server

Edit `metanoia/.env`:

```
WHATSAPP_ENABLED=True
WHATSAPP_PHONE_NUMBER_ID=<phone number id>
WHATSAPP_BUSINESS_ACCOUNT_ID=<whatsapp business account id>
WHATSAPP_ACCESS_TOKEN=<system user token>
WHATSAPP_APP_SECRET=<App settings → Basic → App secret>
WHATSAPP_VERIFY_TOKEN=<any long random string you make up>
WHATSAPP_DISPLAY_NUMBER=<whatsapp-number, e.g. +27 xx xxx xxxx>
```

Restart the site: `sudo systemctl restart metanoia`.

## 6. The webhook (incoming messages)

1. developers.facebook.com → your app → WhatsApp → **Configuration** → Webhook → Edit.
2. Callback URL: `https://learn.metanoiagroup.co.za/communication/whatsapp/webhook/`
3. Verify token: the same `WHATSAPP_VERIFY_TOKEN` as in `.env`. Click **Verify and save**.
4. Under webhook fields, **subscribe to `messages`**. This one subscription brings in both messages and delivery ticks.

## 7. Templates (messages the LMS starts)

WhatsApp only lets a business start a conversation with a pre-approved template. The platform's set is listed in `core/whatsapp_templates.py`. On the server:

```
cd ~/projects/Metanoia/metanoia
.environment/bin/python manage.py whatsapp_templates            # list them
.environment/bin/python manage.py whatsapp_templates --submit   # send them to Meta
.environment/bin/python manage.py whatsapp_templates --status   # check approval
```

`metanoia_invoice` has a PDF header. Meta only accepts a sample file through WhatsApp Manager, so the command prints its wording and you create it by hand: WhatsApp Manager → Message templates → Create. Category **Utility**, name `metanoia_invoice`, language English, header **Document**, then paste the body.

Approval usually takes minutes to a day. Until a template is approved, sends that need it fail. Each failure is logged and shown in the inbox as "Not delivered".

## 8. Check it works

1. From any phone, send "hi" to the business number. You should get the menu, or the welcome message if the number isn't on an account.
2. Type a question. It appears under **Staff desk → WhatsApp → Open**, and admins' bells ring.
3. Reply from the inbox. The reply arrives on the phone, and the tick changes to Delivered and then Read.
4. Register a test student. The contact step now shows **Verify your WhatsApp number**, and the code arrives on WhatsApp.

## 9. Then update the public link

The landing page and footer take the number from `.strings.json` → `brand.contact.whatsapp` and `whatsapp_url`. Set both to the business number (`<whatsapp-number>` and `https://wa.me/<number-in-international-format-without-plus>`) once step 8 works.

## Costs

Meta bills the WABA per template message, at different rates for Authentication, Utility and Marketing. Everything here is Authentication (codes) or Utility (account updates). Replies inside the 24-hour window, which covers the bot and staff replies to someone who just wrote, are free. Check Meta's current South Africa rate card and add a payment method under WhatsApp Manager → Payment settings.

## How the 24-hour window shows in the inbox

- **Green line above the reply box:** they wrote in the last 24 hours, so your reply goes straight away.
- **Amber line:** they didn't. Write your reply as normal. They get the `metanoia_new_message` template ("the team has replied… tap Show message"), and your reply is delivered the moment they tap or answer. The message shows "Waiting for them to reply" until then.
