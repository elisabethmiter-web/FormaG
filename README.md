# FormSign (general edition)

The general version of FormSign for any business: contracts, agreements, consents and files. It has no fabrication-specific forms (no plumbing & appliance spec form, no layout or drawing approvals).

A small web app for sending forms to clients for e-signature and getting confirmation when they're done.

- **Form library**: upload PDF forms (engagement letters, consents, contracts) or build fillable forms (intake questionnaires, authorizations). PDFs can also carry extra fields for the client to fill in.
- **Signing packets**: bundle several forms for one client, in the order you choose, behind one private link.
- **Send**: copy the link into your own email or message, or have the client sign right away on your device. The app doesn't send emails or texts.
- **Track and confirm**: each packet shows Sent → Opened → In progress → Signed, with a timestamped activity log. When the client finishes, they see a confirmation number and download their signed copies; you see "Confirmed" on the packet and download the signed PDFs from it.
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

Each company that uses FormSign gets its **own account**: its own managers and staff, its own form library, its own packets and signed copies. One company can never see another's data, and clients see the company's own name on signing pages and signed PDFs.

- **Self sign-up**: a new company clicks **Create your company account** on the sign-in page, enters its company name, the person's name, email and password, and is ready to go. That person is the company's first manager. Turn this off with `ALLOW_SIGNUP=0`.
- **Platform owner (you)**: sign in with **admin** and the `ADMIN_PASSWORD` setting. You see the **Companies** screen: every company with its logins, packets, storage used and last activity. From there you can:
  - **Add a company** with its first manager and a temporary password (they choose their own at first sign-in).
  - **Open** a company to help it, with management access. An orange bar shows you're inside it; **Back to all companies** takes you out.
  - **Pause** a company (for example, unpaid account): its team can't sign in until you turn it back on. Nothing is deleted, and signing links already sent to its clients keep working.
  - **Change** the storage included for a company (default 25 GB, set with `INCLUDED_STORAGE_GB`).
- **Company page** (managers): change the company name clients see, set the logo and colour, and see storage used against the storage included.
- **Sign-in**: everyone signs in with their email and password. The email decides which company they belong to, so each email can only have one login.
- **Upgrading** from the single-company version: on first start, everything already in the database becomes one company named after your old `BUSINESS_NAME` setting. Nothing else changes for those users.

## Two owners sign

On the New packet page choose **Two owners must both sign** and add the second owner's name (and email, for your records). Each owner gets their own link, which you copy and send them. The second owner signs each form after the first, seeing exactly what was filled in and chosen. The packet completes only when both have signed everything. Both signatures go on the signed PDF and in the signing record. The packet page shows when the second owner has forms ready to sign, so you can let them know.

## Photos and files from the client

Tick **Ask the client for photos or files** and say what you need (for example "Photos of your sink and faucet model numbers"). The client adds them from their phone or computer while signing (photos, PDF, Word, Excel or drawings, up to 25 MB each). Adding files is optional for the client and never holds up the packet; they can also add files after signing, while their link works. Files appear on the packet page and count toward the company's storage. You can also add or change the request later from the packet page.

## Bundles

Management saves sets of forms you send together (for example "Kitchen job") on the Form library page. On New packet, one click ticks all the forms in the bundle.

## Sign now on this device (in the showroom)

**Sign now on this device**, on New packet or on any packet, lets the client sign on your tablet or computer right away. Email is optional in this case. While they sign, the device is locked to the signing pages; to get back to your screens, you enter your own password. With two owners, the second can sign right after on the same device. The signing record says the document was signed in person and who started it.

## Signature boxes on your PDFs

For a PDF in the library, management clicks **Place signature** and puts Signature, Printed name and Date boxes exactly where they belong on the page (for the client, or for the second owner). New packets using that form show the signatures right on the page. The signing record page is still added at the end.

## Digitally sealed PDFs

Every signed PDF and audit trail is digitally sealed. If anyone changes the file afterwards, Adobe Acrobat Reader's Signatures panel shows that it was changed after signing. By default the app uses its own certificate (created automatically and kept in `DATA_DIR/seal`), so Acrobat says the document is unchanged but that the signer's identity can't be verified. For a green check mark, buy a document-signing certificate from an Adobe-trusted (AATL) provider and set `SEAL_P12_BASE64` and `SEAL_P12_PASSWORD`.

## Export to Excel

**Export to Excel** on the Packets page downloads every packet you can see (or only the ones matching your search, optionally by date created): client, email, second owner, SO#, sales rep, dates, status, forms and files from the client.

## 14-day demo (locks on its own)

Companies that sign up themselves get a 14-day demo (`TRIAL_DAYS`). When you add a company yourself, tick **Start as a 14-day demo** to do the same (it's ticked by default). Managers see the days left at the top of every page. When the demo ends, managers see a red banner for 3 days (`DEMO_GRACE_DAYS`); then the company **locks on its own**: its team is signed out and can't sign in, and the sign-in page tells them to contact `SUPPORT_EMAIL`. Nothing is deleted, and signing links already sent to clients keep working. The company shows as "Demo ended" on your Companies screen and in the Admin Console.

**+14 days demo** (on the Companies screen or in the Admin Console) unlocks the company for another 14 days; you can do this as many times as you like. **Mark as paid** keeps it on for good.

## Invoices (for the platform owner)

The Admin Console has a **Billing** page for every company to record the invoices you send and whether each one has been paid. The invoices are stored in this app's database, are visible only through the Admin Console (never to the company), and count nothing toward the company's storage. `INVOICE_DEFAULT_AMOUNT` (default 185.00) and `INVOICE_CURRENCY` (default CAD) set the amount suggested for a new invoice.

## Logo and colour

Managers upload their logo and choose a colour under **Company → Logo and colour**. Clients see them on the signing pages.

## Storage warning

If the app runs on Render without a persistent disk, everything is erased on every restart. The app detects this and shows a red warning to you and to managers, and in the Admin Console. Fix it by adding a disk (mount path `/var/data`) and setting `DATA_DIR` to `/var/data`.

## Admin Console (optional)

If you run more than one FormSign app, the separate **FormSign Admin Console** shows every company from every app in one place and opens any company with one click. To connect this app to it, set `ADMIN_API_TOKEN` to a long random value (different for each app) and enter the same value in the console. Without that setting the admin API is off. See the Admin Console's README.

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
- PDFs are shown on screen and stamped with the signature. Other files are downloaded by the client to review; their signed record is a signature page with the file's name and SHA-256 fingerprint (images are shown on that page). The original file is kept with the packet.
- A packet with only files to review (nothing to sign) completes when the client clicks **Confirm I've received these files**.
- Only PDFs and common images open in the browser; every other type is always sent as a download, so an uploaded file can never run as a web page.

## Everyday tools

- **Send in one click**: on a packet, **Email it** or **Text it** opens your own email or phone with the message, link and your name filled in, so nothing is typed by hand. **Copy message** copies it instead. Each send or reminder is logged on the packet.
- **What's new**: the dashboard shows "New since your last visit" (opened, signed, files added) and the Packets menu shows a count. Click **Turn on alerts** and the browser pops up a notice while FormSign is open in a tab.
- **Follow-up list**: the **Follow-up** page lists every open packet that needs a nudge today (overdue, not opened in 2 days, opened but not finished, waiting on the second owner), each with a ready-made reminder. Once you've reminded someone, they move down to "Already reminded today". The menu shows how many are left.
- **Who it's waiting on**: with two owners, the dashboard and packet show "Waiting on Emma Wong" so you know who to call.
- **Download everything**: one ZIP with all signed PDFs, the files from the client and the audit trail, from the packet page or the client's done page. Clients are told to keep their link.
- **Nightly backups**: every night (1–5 am) the app saves a copy of the database and all files, keeps the last 7 (`BACKUP_KEEP`) and, if you set the `BACKUP_S3_…` settings, copies each one to Backblaze B2 or Amazon S3. The Admin Console shows the last backup, has **Back up now**, lets you download any backup, and has **Restore** (pick a backup on the server or upload one; today's data is saved first so a restore can be undone).
- **Server restarts don't lose a signature**: if the server is restarting when a client presses Finish, the page waits and sends it again by itself as soon as the server is back. `/healthz` tells Render the app is up.
- **Access code (optional)**: tick **Ask for an access code** on New packet. The client needs a 6-digit code to open their link; give it to them by phone or text, separately from the link. After 5 wrong tries the link waits 15 minutes. You can change or remove the code on the packet page. Signing on your own device skips the code.
- **Earlier clients**: typing a client's name on New packet suggests clients you've sent to before and fills in their email.
- **Faster previews**: Word, Excel and photo previews are prepared in the background as soon as a packet is made, so the client's page opens quickly.
- **Time zone per company**: managers pick their time zone on the Company page; every time in the app is shown in it.
- **Check a signed PDF**: anyone can upload a signed PDF at `/verify` to see whether it matches the one signed here, with no certificate to buy. The signing record page explains the "identity unknown" message Adobe shows for the app's own seal, gives the verify address, and `/verify/certificate.cer` lets an office trust the seal in Acrobat if they want.
- **Demo grace days**: when a demo ends, the company gets a red banner and keeps working for 3 more days (`DEMO_GRACE_DAYS`) before it locks, so nobody loses a client mid-signing.
- **Help when stuck**: the sign-in page shows who to call (`SUPPORT_NAME`, `SUPPORT_PHONE`, `SUPPORT_EMAIL`), and the **Help** page has step-by-step answers for the common jobs, plus your video link (`HELP_VIDEO_URL`).

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

## All settings

| Variable | Required | Purpose |
| --- | --- | --- |
| `ADMIN_PASSWORD` | yes | Platform owner password (sign in as **admin**) |
| `SECRET_KEY` | yes in production | Long random string that secures sessions |
| `PLATFORM_NAME` | no | Your service's name on the sign-in and sign-up pages (default FormSign) |
| `ALLOW_SIGNUP` | no | `1` lets companies create their own account (default), `0` means only you add them |
| `INCLUDED_STORAGE_GB` | no | Storage included for each new company (default 25) |
| `ADMIN_API_TOKEN` | no | Turns on the private API used by the Admin Console (long random value) |
| `SEAL_P12_BASE64` / `SEAL_P12_PASSWORD` | no | A purchased document-signing certificate (.p12/.pfx, base64-encoded) for sealing PDFs |
| `SEAL` | no | `0` turns off PDF sealing |
| `TRIAL_DAYS` | no | Length of the demo (default 14) |
| `INVOICE_DEFAULT_AMOUNT` / `INVOICE_CURRENCY` | no | Suggested amount and currency for new invoices in the Admin Console (default 185.00 CAD) |
| `SUPPORT_EMAIL` | recommended | Shown to companies when their demo ends, as who to contact (for example liz@helmmsp.ca) |
| `DEMO_GRACE_DAYS` | no | Days a company keeps working after its demo ends, with a warning, before it locks (default 3) |
| `SUPPORT_NAME` / `SUPPORT_PHONE` | recommended | Who to call, shown on the sign-in and Help pages (for example Helm MSP, 1-555-...) |
| `HELP_VIDEO_URL` | no | Link to a how-to video, shown on the Help page |
| `BACKUP_KEEP` | no | How many nightly backups to keep on the disk (default 7) |
| `BACKUPS` | no | `0` turns off nightly backups |
| `BACKUP_S3_ENDPOINT` | no | Off-site backups: the S3 address, e.g. `https://s3.us-west-004.backblazeb2.com` (Backblaze B2) or `https://s3.ca-central-1.amazonaws.com` |
| `BACKUP_S3_BUCKET` / `BACKUP_S3_REGION` | no | Bucket name and region (e.g. `us-west-004`, `ca-central-1`) |
| `BACKUP_S3_KEY_ID` / `BACKUP_S3_SECRET` | no | The application key ID and secret for that bucket (write access is enough) |
| `STORAGE_CHECK` | no | `0` hides the storage warning; `1` checks even off Render |
| `BUSINESS_NAME` | no | Only used when upgrading: names the first company |
| `BASE_URL` | recommended | Public address, used in signing links |
| `DATA_DIR` | no | Storage folder (default `./data`) |
| `LINK_EXPIRY_DAYS` | no | Default link lifetime, also used by "Extend link" (default 30) |

## How it works

- `admin_api.py`: private API for the Admin Console (company stats, pause, storage, one-time sign-in links).
- `app.py`: Flask routes for the admin side (`/`, `/library`, `/packets/...`) and the client side (`/s/<token>/...`).
- `pdfgen.py`: builds signed PDFs and audit trails with pypdf and ReportLab.
- SQLite database with four tables: `templates`, `packets`, `packet_items`, `events`.
- When a packet is created, each form is snapshotted into it, so editing or removing a form later never changes what a client already received or signed.
- Signing links use 32-character random tokens, expire, and can be cancelled. Anyone with a link can sign it, so send links only to the client.

## Ideas for later

- Multiple signers per packet (e.g. two company directors), signing order
