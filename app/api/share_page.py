"""`GET /s#<code>`: the page a share QR code opens.

Phone cameras open only web addresses, so the QR holds this page, which hands the code to the app. The code stays
in the URL fragment: browsers never send it, so it is not logged here. No data is read; the page is public.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

ANDROID_PACKAGE = "com.dev.kamalelmagouri.ebook_ai_pro"

_PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E-Book AI</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;padding:32px 20px;text-align:center;background:#faf5ff;color:#0f172a}
a{display:inline-block;margin:24px 0;padding:16px 28px;border-radius:999px;background:#3a85ed;color:#fff;
  text-decoration:none;font-weight:600}
code{display:block;word-break:break-all;padding:12px;border:1px solid #c4b5fd;border-radius:12px;background:#fff}
p{line-height:1.5}
</style>
</head>
<body>
<h1>E-Book AI</h1>
<div id="ok" hidden>
<p>Un document a &eacute;t&eacute; partag&eacute; avec vous.<br>A document was shared with you.</p>
<a id="open" href="#">Ouvrir dans l'application / Open in the app</a>
<p>L'application ne s'ouvre pas ? Saisissez ce code dans &laquo; Recevoir un PDF &raquo;.<br>
Nothing happens? Enter this code in "Receive a PDF".</p>
<code id="code"></code>
</div>
<p id="bad" hidden>Lien invalide / Invalid link</p>
<noscript><p>JavaScript est n&eacute;cessaire / JavaScript is required.</p></noscript>
<script>
var code = location.hash.slice(1).toLowerCase();
if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(code)) {
  document.getElementById("code").textContent = code;
  document.getElementById("open").href = /Android/i.test(navigator.userAgent)
    ? "intent://share/" + code + "#Intent;scheme=ebookaipro;package=__PACKAGE__;end"
    : "ebookaipro://share/" + code;
  document.getElementById("ok").hidden = false;
} else {
  document.getElementById("bad").hidden = false;
}
</script>
</body>
</html>
""".replace("__PACKAGE__", ANDROID_PACKAGE)

_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                               "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/s", include_in_schema=False)
def share_page():
    return HTMLResponse(_PAGE, headers=_HEADERS)
