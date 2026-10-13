# FormSign (general edition)

The general version of FormSign for any business: contracts, agreements, consents and files. It has no fabrication-specific forms (no plumbing & appliance spec form, no layout or drawing approvals).

A small web app for sending forms to clients for e-signature and getting confirmation when they're done.

- **Form library**: upload PDF forms (engagement letters, consents, contracts) or build fillable forms (intake questionnaires, authorizations). PDFs can also carry extra fields for the client to fill in.
- **Signing packets**: bundle several forms for one client, in the order you choose, behind one private link.
- **Send**: copy the link and send it yourself, or have the app email it (optional SMTP).
- **Track and confirm**: each packet shows Sent → Opened → In progress → Signed, with a timestamped activity log. When the client finishes, they see a confirmation number and can download copies; you see "Confirmed" on the packet, and if email is on you both get the signed PDFs.
- **Signed PDFs**: the original pages, stamped with a signing footer, plus a signature certificate page (signature, name, time, IP address, device, SHA-256 fingerprint of the original file). A per-packet audit trail PDF is one click away.

Signatures are "simple electronic signatures" (drawn or typed, plus consent and an audit record). That's generally fine for engagement letters, intake and consent forms. For documents that legally need identity verification or a qualified signature, use a dedicated provider.

## 48-hour signing target and follow-ups

- Clients have **48 hours** from when a packet is sent to complete it (change with the `SIGN_WITHIN_HOURS` setting). The client sees "Please complete by …" on their page. The signing link itself stays valid until it expires (30 days by default), so a late client can still sign.
- Packets not completed in time turn **orange** on the dashboard ("Overdue by 5 h"), get an **Overdue** count card and tab, and an orange banner on the packet page. Others show "Due in 20 h".
- Dashboard tabs: **All · Open · Awaiting client** (sent, not opened yet) **· Overdue · Completed**.
- Each packet has **Follow-ups & notes**: five follow-up checkboxes, each with its own note. Ticking one stamps the date, time and who did it. A general Notes box holds anything else. Changes appear in the packet's activity history.
- The dashboard's **Last follow-up** column shows the latest follow-up number, date and note, or "None yet" in orange when an overdue packet hasn't been followed up.
- Times are shown in Toronto time (change with the `TIMEZONE` setting, e.g. `America/Vancouver`).

## Companies (one server, many businesses)

Each company that uses FormSign gets its **own account**: its own managers and staff, its own form library, its own packets and signed copies. One company can never see another's data, and clients see the company's own name on signing pages, emails and signed PDFs.

- **Self sign-up**: a new company clicks **Create your company account** on the sign-in page, enters its company name, the person's name, email and password, and is ready to go. That person is the company's first manager. Turn this off with `ALLOW_SIGNUP=0`.
- **Platform owner (you)**: sign in with **admin** and the `ADMIN_PASSWORD` setting. You see the **Companies** screen: every company with its logins, packets, storage used and last activity. From there you can:
  - **Add a company** with its first manager and a temporary password (they choose their own at first sign-in).
  - **Open** a company to help it, with management access. An orange bar shows you're inside it; **Back to all companies** takes you out.
  - **Pause** a company (for example, unpaid account): its team can't sign in until you turn it back on. Nothing is deleted, and signing links already sent to its clients keep working.
  - **Change** the storage included for a company (default 25 GB, set with `INCLUDED_STORAGE_GB`).
- **Company page** (managers): change the company name clients see, set a **notification email** that receives completion notices and client replies, and see storage used against the storage included.
- **Sign-in**: everyone signs in with their email and password. The email decides which company they belong to, so each email can only have one login.
- **Upgrading** from the single-company version: on first start, everything already in the database becomes one company named after your old `BUSINESS_NAME` setting. Nothing else changes for those users.

## Email campaigns (mass email to your client list)

The **Email** tab lets a company send one email to its whole client list, or to a group.

- **Client list**: everyone you send a packet to is added automatically. You can also **import** a CSV or Excel (.xlsx) file (first row = headings; it needs an *Email* column and uses *Name* or *First/Last name*, *Company* and *Tags* if present) and **add people by hand**. Duplicates and invalid addresses are skipped, and you can tag everyone in a file (e.g. "Builders"). Download the list as CSV at any time.
- **Write a campaign**: subject, message (plain text; blank lines make paragraphs, web addresses become links, `**text**` is bold), an optional button with a link, and personalization with `{first_name}`, `{name}` or `{organization}`. A live preview shows the email as clients will see it.
- **Send to** everyone, or only clients with a given tag. **Send me a test** first, then **Review and send**. Sending runs in the background (you can close the page), shows progress, and can be stopped. If the server restarts mid-send it picks up where it stopped.
- **Report** per campaign: sent, opened (an estimate, since some mail apps hide opens), failed (with the reason, and a *Retry failed* button) and skipped.
- **Who can send**: managers can email the whole company list and see every campaign. Staff can send too, but only to the clients of their own packets and contacts they added themselves, and they only see their own campaigns.
- **Canada's anti-spam law (CASL)**: every campaign email automatically includes the company name, its **mailing address** (set on the Company page; campaigns can't be sent until it's filled in), a contact email and a working **unsubscribe** link, plus the one-click unsubscribe headers Gmail, Outlook and Yahoo require. Unsubscribed clients are never emailed again, even if they appear in a later import. Before sending, the sender confirms the list is made of clients or people who agreed to receive emails. Only email people you have consent for (current clients, or people who opted in).
- **Limits**: each company can send up to 2,000 campaign emails per 24 hours by default (`CAMPAIGN_DAILY_LIMIT`), at about 120 per minute (`CAMPAIGN_PER_MINUTE`). Match these to your email provider's plan.
- Campaigns go out through the same SMTP settings as packets, with the company's name as the sender and replies going to the company's notification email. For bulk email use a provider built for it (Amazon SES, Postmark broadcast streams, Mailgun, SendGrid), and set up SPF and DKIM for your sending domain so emails land in the inbox.


- Logins, roles and visibility below always apply **within one company**. If a company's only manager is locked out, the platform owner can open the company and reset their password from the Team page.
- **Team** page (management only): create logins with a name, email, access level and a temporary password. People choose their own password the first time they sign in. Managers can change someone's access, reset a password, deactivate a login (it stops working immediately, and can be turned back on), or **remove** it for good. When you remove someone, their packets are never deleted: you choose who gets them, or leave them visible to management only. You can't remove or deactivate your own login.
- **Staff** only see **their own packets**: the ones they created. Opening another person's packet by its address shows "Page not found". Staff can create, send and track packets and use library forms, but can't add, edit or remove forms in the library (the buttons are hidden and the server refuses those actions).
- **Management** sees **every packet from every user**, with a **Created by** column and a filter to show one person's packets. They can also manage the form library and the Team page.

## Dashboard order and missing forms

- The newest packets are always at the top (by the date they were sent).
- For any packet that isn't finished, the Status column lists in orange the forms the client **still needs to sign**, so you can see what's missing without opening the packet. Packets with only files to review show "Receipt not confirmed yet" until the client confirms.
- Each packet records who created it, and voiding, extending and detail changes note who did them.
- Everyone can change their own password by clicking their name at the top right.
- 8 wrong passwords from one address locks sign-in for 15 minutes.

## Documents shown on the signing page

The client reads every document right above where they sign:
- PDFs are shown as page images (works on any phone, nothing to install). Tap a page to zoom.
- Word, Excel, PowerPoint, OpenDocument, RTF, CSV and text files are converted and shown the same way when the app runs with Docker (the Dockerfile installs LibreOffice). Without Docker, Word (.docx), Excel (.xlsx), CSV and text files get a simplified preview.
- Pictures are shown directly. Files that can't be previewed (ZIP, CAD...) are offered as a download.
- When the client signs a Word/Excel file, the signed PDF includes its pages stamped with the signature; the original file is kept too.

## One-off files and any file type

- **Form library** accepts any file type (PDF, Word, Excel, images, CAD, ZIP...), up to 50 MB.
- **New packet → One-off files**: drop in files just for this client. They're sent with that packet only and never saved to your library.
- For each one-off file, choose **Client signs this** or leave it unticked so the file is just for their records (they download it; you see when they did).
- PDFs are shown on screen and stamped with the signature. Other files are downloaded by the client to review; their signed record is a signature page with the file's name and SHA-256 fingerprint (images are shown on that page). The original file is kept with the packet and attached to the completion email.
- A packet with only files to review (nothing to sign) completes when the client clicks **Confirm I've received these files**.
- Only PDFs and common images open in the browser; every other type is always sent as a download, so an uploaded file can never run as a web page.

## Run it on your computer (5 minutes)

Needs Python 3.10 or newer.

```bash
pip install -r requirements.txt
ADMIN_PASSWORD=choose-a-password python app.py
```

Open http://localhost:5000. Sign in as **admin** to add companies, or click **Create your company account**. On Windows PowerShell set variables with `$env:ADMIN_PASSWORD="..."` first.

Locally, only you can open the signing links. To let clients sign, put it online.

## Put it online (Render, about US$7/month)

1. Create a free GitHub account and a new **private** repository; upload this folder's contents.
2. On [render.com](https://render.com) choose **New → Blueprint** and pick the repository. It reads `render.yaml`, which sets up the web service and a persistent disk (raise its size as companies are added; each includes 25 GB) for your forms and signed documents.
3. When asked, fill in `ADMIN_PASSWORD` and `BASE_URL` (the address Render gives you, e.g. `https://formsign-xxxx.onrender.com`, or your own domain).
4. Deploy, open the address, sign in.

Any host that runs Docker works too (Railway, Fly.io, a VPS): build the `Dockerfile` and mount a persistent volume at `/data`.

**Keep the disk backed up.** Everything (database, uploaded forms, signed PDFs) lives in `DATA_DIR`. Render takes daily disk snapshots on paid plans; you can also download the folder periodically.

## Turn on email (optional)

Set these environment variables and restart. Any SMTP service works: Google Workspace, Microsoft 365, Postmark, SendGrid, Mailgun, Amazon SES.

| Variable | Example |
| --- | --- |
| `SMTP_HOST` | `smtp.postmarkapp.com` |
| `SMTP_PORT` | `587` (or `465` for SSL) |
| `SMTP_USER` / `SMTP_PASSWORD` | from your provider |
| `SMTP_FROM` | `Your Company <forms@yourdomain.com>` |
| `ADMIN_EMAIL` | fallback for completion notices and replies when a company has no notification email |

With email on, you can send packets and reminders from the app, and the client, the person who sent the packet and the company's notification email receive the signed PDFs when the packet is completed. Gmail accounts need an app password; for client-facing mail a transactional service (Postmark, SES) gives better delivery.

## All settings

| Variable | Required | Purpose |
| --- | --- | --- |
| `ADMIN_PASSWORD` | yes | Platform owner password (sign in as **admin**) |
| `SECRET_KEY` | yes in production | Long random string that secures sessions |
| `PLATFORM_NAME` | no | Your service's name on the sign-in and sign-up pages (default FormSign) |
| `ALLOW_SIGNUP` | no | `1` lets companies create their own account (default), `0` means only you add them |
| `INCLUDED_STORAGE_GB` | no | Storage included for each new company (default 25) |
| `CAMPAIGN_DAILY_LIMIT` | no | Campaign emails each company may send per 24 hours (default 2000) |
| `CAMPAIGN_PER_MINUTE` | no | Sending speed for campaigns (default 120 per minute) |
| `BUSINESS_NAME` | no | Only used when upgrading: names the first company |
| `BASE_URL` | recommended | Public address, used in emailed links |
| `DATA_DIR` | no | Storage folder (default `./data`) |
| `LINK_EXPIRY_DAYS` | no | Default link lifetime, also used by "Extend link" (default 30) |

## How it works

- `campaigns.py`: client lists, CSV/Excel import, campaign writing, background sending, unsubscribe and open tracking.
- `app.py`: Flask routes for the admin side (`/`, `/library`, `/packets/...`) and the client side (`/s/<token>/...`).
- `pdfgen.py`: builds signed PDFs and audit trails with pypdf and ReportLab.
- `mailer.py`: optional SMTP email.
- SQLite database with four tables: `templates`, `packets`, `packet_items`, `events`.
- When a packet is created, each form is snapshotted into it, so editing or removing a form later never changes what a client already received or signed.
- Signing links use 32-character random tokens, expire, and can be cancelled. Anyone with a link can sign it, so send links only to the client.

## Ideas for later

- Place the signature on a specific spot on the PDF page instead of a certificate page
- Multiple signers per packet (e.g. two company directors), signing order
- Automatic reminder emails after N days
- Client-uploaded attachments (ID, documents)
