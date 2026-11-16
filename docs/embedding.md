# Embedding demoreel in the community's site

demoreel is meant to live inside the site the community already has (for Mix da Vila: a "Jogadas" page next
to Ranking and Partidas). The supported way is an `<iframe>` pointing at the demoreel server with `?embed=1`.
Videos then go straight from the demoreel server to the visitor; they never pass through the host site.

## What `?embed=1` changes

- No header and no footer of ours: the host page has its own. The menu becomes one row of links at the top
  of the content (Destaques, Jogadas, Partidas, Jogadores, Zoeira, Chat, Rivalidades). On a phone it is
  one line that slides sideways.
- No side gutters and no minimum height: the frame is exactly as wide as the host gives it and as tall as
  the content.
- Visitor mode, always. Importing, rendering, editing, promoting and deleting are never shown inside the
  frame. The organizer uses the demoreel address directly (see below).
- The tab title, theme colour and icon of the page are left alone.
- A tall (9:16) player is sized against `--viewport-h`, 760 px by default, instead of the height of the
  window (inside an auto-height frame "the window" is the frame itself). The host can send its real height.

## Messages

From the frame to the host page (`window.parent.postMessage`):

| Message | When |
| --- | --- |
| `{type: "demoreel:resize", height}` | the content changed height (px); set the frame's height to it |
| `{type: "demoreel:route", hash}` | the route changed (`#/play/h_…`); mirror it in your URL if you want deep links |

From the host page to the frame:

| Message | Effect |
| --- | --- |
| `{type: "demoreel:viewport", height}` | the height of the visitor's window (px); tall players are sized against it |
| `{type: "demoreel:navigate", hash}` | open a route (`#/match/m_…`) |

If the brand kit lists the allowed hosts (`ui.embed.parents`, e.g. `["https://mixdavila.example"]`) the frame
talks and listens only to those origins (and to its own). Without the list it answers any parent; the
messages carry a height and a route, nothing private.

## Snippet for the host page

```html
<iframe id="demoreel" src="https://lances.example.com/?embed=1" title="Jogadas"
        style="width: 100%; border: 0; display: block" loading="lazy"
        allow="fullscreen; clipboard-write; web-share"></iframe>
<script>
  (function () {
    var ORIGIN = "https://lances.example.com";          // where demoreel runs
    var frame = document.getElementById("demoreel");
    // a link to this page may carry the route: /jogadas#/play/h_123
    if (location.hash.indexOf("#/") === 0) frame.src = ORIGIN + "/?embed=1" + location.hash;
    function send(msg) { frame.contentWindow.postMessage(msg, ORIGIN); }
    window.addEventListener("message", function (e) {
      if (e.origin !== ORIGIN || !e.data) return;
      if (e.data.type === "demoreel:resize") frame.style.height = e.data.height + "px";
      if (e.data.type === "demoreel:route") history.replaceState(null, "", e.data.hash);
    });
    frame.addEventListener("load", function () { send({ type: "demoreel:viewport", height: window.innerHeight }); });
    window.addEventListener("resize", function () { send({ type: "demoreel:viewport", height: window.innerHeight }); });
  })();
</script>
```

`allow` is needed for the three things a visitor does with a play: full screen, "Copiar link" and the
system share sheet on phones.

## Share links

"Copiar link" and "Compartilhar" give `https://<demoreel>/p/<play id>`: a real page with Open Graph tags (a
chat shows the title, the frame and the video) that then opens the app. By default it opens the app on the
demoreel server. Once the host page exists, point it there in the brand kit:

```json
"ui": { "embed": { "share_base": "https://mixdavila.example/jogadas", "parents": ["https://mixdavila.example"] } }
```

The link then lands on `https://mixdavila.example/jogadas#/play/<id>`, and the snippet above passes the route
on to the frame. Do not set `share_base` before that page is live: every shared link would lead to it.

## Visitor and organizer

- With `DEMOREEL_ADMIN_KEY` set on the server, anyone can watch and only requests carrying the key can
  change anything (`GET /api/v1/session` tells the interface which side it is on).
- Visitors see no organizer actions at all. The organizer opens the demoreel address itself (not the
  embedded page), follows "Sou organizador" in the footer and types the key once; it is kept in that
  browser and sent as `Authorization: Bearer <key>`. "Ver como visitante" shows the site the way everyone
  else sees it; "Sair do modo organizador" forgets the key.
- Without an admin key on the server everyone is the organizer (the single-user setup).

## Before going public

- Send `Content-Security-Policy: frame-ancestors https://<host site>` from the demoreel server (or the proxy
  in front of it) so only that site can frame it.
- Behind a proxy, set the public base URL of the server: video and thumbnail addresses are absolute.
- Give the static files (`/js`, `/styles`) a `Cache-Control: no-cache`, so a tab left open picks up a new
  version of the interface instead of mixing old and new modules.
