"""Digitally seals finished PDFs so any change made after signing can be detected.

Every signed PDF and audit trail gets a standard PDF digital signature (PKCS#7, SHA-256), the kind Adobe
Acrobat shows in its Signatures panel. If anyone edits the file afterwards, Acrobat reports that the
document was changed since it was sealed.

Certificate:
  * By default the app makes its own certificate the first time it runs (kept in DATA_DIR/seal/).
    Acrobat then says the document "has not been modified" but that the signer's identity can't be
    verified, because it doesn't know that certificate.
  * For a green check mark in Acrobat, buy a document-signing certificate from an Adobe-trusted (AATL)
    provider and set SEAL_P12_BASE64 (the .p12/.pfx file, base64-encoded) and SEAL_P12_PASSWORD.
  * SEAL=0 turns sealing off.
"""
import base64
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12
from cryptography.x509.oid import NameOID
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, ByteStringObject, DictionaryObject, NameObject, NumberObject,
                           TextStringObject)

log = logging.getLogger(__name__)
CONTENTS_BYTES = 12000          # room for the signature and certificate chain
_PLACEHOLDER = 9_999_999_999    # ByteRange numbers, replaced by the real offsets (same width)
_identity = None


def enabled():
    return os.environ.get("SEAL", "1") != "0"


def _load_identity(data_dir, org):
    """(private key, certificate, extra certificates) used to seal."""
    global _identity
    if _identity:
        return _identity
    p12 = os.environ.get("SEAL_P12_BASE64", "").strip()
    if p12:
        key, cert, extra = pkcs12.load_key_and_certificates(
            base64.b64decode(p12), (os.environ.get("SEAL_P12_PASSWORD") or "").encode() or None)
        _identity = (key, cert, list(extra or []))
        return _identity
    folder = os.path.join(data_dir, "seal")
    os.makedirs(folder, exist_ok=True)
    kpath, cpath = os.path.join(folder, "seal-key.pem"), os.path.join(folder, "seal-cert.pem")
    if not (os.path.exists(kpath) and os.path.exists(cpath)):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{org} document seal"[:64]),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, org[:64])])
        start = datetime.now(timezone.utc) - timedelta(days=1)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(start)
                .not_valid_after(start + timedelta(days=365 * 20))
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                             data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                             crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .sign(key, hashes.SHA256()))
        tmp = f"{kpath}.{secrets.token_hex(4)}"
        with open(tmp, "wb") as fh:
            fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        os.chmod(tmp, 0o600)
        with open(cpath + ".tmp", "wb") as fh:
            fh.write(cert.public_bytes(serialization.Encoding.PEM))
        os.replace(tmp, kpath)          # several server workers may start at once: last writer wins, files
        os.replace(cpath + ".tmp", cpath)  # are then re-read below so every worker uses the same pair
    with open(kpath, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), None)
    with open(cpath, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        raise RuntimeError("seal key and certificate don't match")
    _identity = (key, cert, [])
    return _identity


def certificate_summary(data_dir, org):
    key, cert, _ = _load_identity(data_dir, org)
    return {"subject": cert.subject.rfc4514_string(), "self_signed": cert.issuer == cert.subject,
            "expires": cert.not_valid_after_utc.date().isoformat()}


def seal_pdf(path, data_dir, org, reason="Signed electronically", location=""):
    """Seal the PDF at `path` in place. Returns True if sealed; never raises (signing must not fail because
    of the seal)."""
    if not enabled():
        return False
    try:
        _seal(path, data_dir, org, reason, location)
        return True
    except Exception:  # noqa: BLE001
        log.exception("Could not seal %s; the signed PDF is kept without a digital seal", path)
        return False


def _seal(path, data_dir, org, reason, location):
    key, cert, extra = _load_identity(data_dir, org)
    writer = PdfWriter(clone_from=PdfReader(path))
    when = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'")
    sig = DictionaryObject({
        NameObject("/Type"): NameObject("/Sig"),
        NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
        NameObject("/SubFilter"): NameObject("/adbe.pkcs7.detached"),
        NameObject("/ByteRange"): ArrayObject([NumberObject(0)] + [NumberObject(_PLACEHOLDER)] * 3),
        NameObject("/Contents"): ByteStringObject(b"\0" * CONTENTS_BYTES),
        NameObject("/M"): TextStringObject(when),
        NameObject("/Name"): TextStringObject(org),
        NameObject("/Reason"): TextStringObject(reason),
    })
    if location:
        sig[NameObject("/Location")] = TextStringObject(location)
    sig_ref = writer._add_object(sig)
    page = writer.pages[0]
    widget = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Sig"), NameObject("/T"): TextStringObject(f"Seal{secrets.token_hex(3)}"),
        NameObject("/V"): sig_ref, NameObject("/F"): NumberObject(132),   # hidden + locked
        NameObject("/Rect"): ArrayObject([NumberObject(0)] * 4), NameObject("/P"): page.indirect_reference,
    })
    w_ref = writer._add_object(widget)
    annots = page.get("/Annots")
    annots = annots.get_object() if annots is not None else ArrayObject()
    annots.append(w_ref)
    page[NameObject("/Annots")] = annots
    root = writer._root_object
    form = root.get("/AcroForm")
    form = form.get_object() if form is not None else DictionaryObject()
    fields = form.get("/Fields")
    fields = fields.get_object() if fields is not None else ArrayObject()
    fields.append(w_ref)
    form[NameObject("/Fields")] = fields
    form[NameObject("/SigFlags")] = NumberObject(3)
    if "/AcroForm" not in root:
        root[NameObject("/AcroForm")] = form
    tmp = path + ".sealing"
    with open(tmp, "wb") as fh:
        writer.write(fh)
    with open(tmp, "rb") as fh:
        data = bytearray(fh.read())

    hex_zeros = b"0" * (CONTENTS_BYTES * 2)
    start = data.find(b"<" + hex_zeros + b">")
    if start < 0 or data.find(b"<" + hex_zeros + b">", start + 1) >= 0:
        raise RuntimeError("signature placeholder not found exactly once")
    end = start + len(hex_zeros) + 2
    br = re.compile(rb"/ByteRange\s*\[\s*0\s+" + (rb"%d\s+" % _PLACEHOLDER) * 2 + (rb"%d\s*\]" % _PLACEHOLDER))
    m = br.search(data)
    if not m:
        raise RuntimeError("ByteRange placeholder not found")
    filled = b"/ByteRange [0 %d %d %d]" % (start, end, len(data) - end)
    if len(filled) > len(m.group(0)):
        raise RuntimeError("ByteRange too long")
    data[m.start():m.end()] = filled.ljust(len(m.group(0)), b" ")
    signed_part = bytes(data[:start]) + bytes(data[end:])
    builder = pkcs7.PKCS7SignatureBuilder().set_data(signed_part).add_signer(cert, key, hashes.SHA256())
    for c in extra:
        builder = builder.add_certificate(c)
    der = builder.sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary])
    if len(der) > CONTENTS_BYTES:
        raise RuntimeError("signature larger than its placeholder")
    data[start + 1:end - 1] = der.hex().encode().ljust(CONTENTS_BYTES * 2, b"0")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
