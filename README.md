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

## Staff and management logins

- **Owner**: sign in with **admin** and the `ADMIN_PASSWORD` server setting. Always has management access, so you can't lock yourself out.
- **Team** page (management only): create logins with a name, email, access level and a temporary password. People choose their own password the first time they sign in. Managers can change someone's access, reset a password, deactivate a login (it stops working immediately, and can be turned back on), or **remove** it for good. When you remove someone, their packets are never deleted: you choose who gets them, or leave them visible to management only. You can't remove or deactivate your own login.
- **Staff** only see **their own packets**: the ones they created. Opening another person's packet by its address shows "Page not found". Staff can create, send and track packets and use library forms, but can't add, edit or remove forms in the library (the buttons are hidden and the server refuses those actions).
- **Management** (and the owner) see **every packet from every user**, with a **Created by** column and a filter to show one person's packets. They can also manage the form library and the Team page.

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
ADMIN_PASSWORD=choose-a-password BUSINESS_NAME="Your Company" python app.py
```

Open http://localhost:5000 and sign in. On Windows PowerShell set variables with `$env:ADMIN_PASSWORD="..."` first.

Locally, only you can open the signing links. To let clients sign, put it online.

## Put it online (Render, about US$7/month)

1. Create a free GitHub account and a new **private** repository; upload this folder's contents.
2. On [render.com](https://render.com) choose **New → Blueprint** and pick the repository. It reads `render.yaml`, which sets up the web service and a 1 GB persistent disk for your forms and signed documents.
3. When asked, fill in `ADMIN_PASSWORD`, `BUSINESS_NAME`, and `BASE_URL` (the address Render gives you, e.g. `https://formsign-xxxx.onrender.com`, or your own domain).
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
| `ADMIN_EMAIL` | where completion notices go, and the reply-to address |

With email on, you can send packets and reminders from the app, and both you and the client receive the signed PDFs when the packet is completed. Gmail accounts need an app password; for client-facing mail a transactional service (Postmark, SES) gives better delivery.

## All settings

| Variable | Required | Purpose |
| --- | --- | --- |
| `ADMIN_PASSWORD` | yes | Your sign-in password |
| `SECRET_KEY` | yes in production | Long random string that secures sessions |
| `BUSINESS_NAME` | recommended | Shown to clients and on PDFs |
| `BASE_URL` | recommended | Public address, used in emailed links |
| `DATA_DIR` | no | Storage folder (default `./data`) |
| `LINK_EXPIRY_DAYS` | no | Default link lifetime, also used by "Extend link" (default 30) |

## How it works

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
